"""SQLite checkpoint for the backfill. Idempotent, crash-safe.

Schema and state machine match the plan's "state.sqlite schema" + "Manejo de
fallas y reintentos" sections.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
    excel_id            INTEGER PRIMARY KEY,
    event_id            TEXT UNIQUE NOT NULL,
    drive_case          TEXT,
    transcript_file_id  TEXT,
    transcript_gcs_uri  TEXT,
    webhook_message_id  TEXT,
    webhook_submitted_at TIMESTAMP,
    pdf_drive_id        TEXT,
    pdf_drive_link      TEXT,
    state               TEXT NOT NULL,
    last_error          TEXT,
    last_attempt_at     TIMESTAMP,
    attempts            INTEGER NOT NULL DEFAULT 0,
    excel_row_json      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rows_state ON rows(state);
CREATE INDEX IF NOT EXISTS idx_rows_submitted_at ON rows(webhook_submitted_at);

CREATE TABLE IF NOT EXISTS source_metadata (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    source_excel_path     TEXT NOT NULL,
    source_excel_sha256   TEXT NOT NULL,
    source_excel_rows     INTEGER NOT NULL,
    phase_1_started_at    TIMESTAMP,
    phase_1_finished_at   TIMESTAMP,
    webhook_url_used      TEXT,
    notes                 TEXT
);
"""


@dataclass
class Row:
    excel_id: int
    event_id: str
    drive_case: str | None
    transcript_file_id: str | None
    transcript_gcs_uri: str | None
    webhook_message_id: str | None
    webhook_submitted_at: str | None
    pdf_drive_id: str | None
    pdf_drive_link: str | None
    state: str
    last_error: str | None
    last_attempt_at: str | None
    attempts: int
    excel_row_json: str

    @property
    def excel_row(self) -> dict:
        return json.loads(self.excel_row_json) if self.excel_row_json else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self):
        try:
            self._conn.execute("BEGIN")
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── Source metadata ────────────────────────────────────────────────────
    def register_source(self, excel_path: str, excel_rows: int, webhook_url: str = "") -> None:
        sha = _file_sha256(excel_path)
        with self._tx():
            existing = self._conn.execute(
                "SELECT source_excel_sha256 FROM source_metadata WHERE id=1"
            ).fetchone()
            if existing:
                if existing["source_excel_sha256"] != sha:
                    raise RuntimeError(
                        f"source_metadata already registered with a different SHA-256 "
                        f"({existing['source_excel_sha256']}) — refusing to overwrite. "
                        f"Run `cli reset --confirm` first if you really want a fresh "
                        f"state.sqlite."
                    )
                return  # idempotent, same file
            self._conn.execute(
                "INSERT INTO source_metadata (id, source_excel_path, source_excel_sha256, "
                "source_excel_rows, phase_1_started_at, webhook_url_used) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (excel_path, sha, excel_rows, _now(), webhook_url),
            )

    def get_source_metadata(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM source_metadata WHERE id=1").fetchone()
        return dict(row) if row else None

    def mark_phase_1_finished(self) -> None:
        with self._tx():
            self._conn.execute(
                "UPDATE source_metadata SET phase_1_finished_at=? WHERE id=1",
                (_now(),),
            )

    # ── Row CRUD ───────────────────────────────────────────────────────────
    def upsert_pending(self, excel_id: int, excel_row: dict) -> None:
        """Insert a row in 'pending' state if not present. Idempotent."""
        event_id = f"studio-{excel_id}"
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO rows (excel_id, event_id, state, excel_row_json) "
                "VALUES (?, ?, 'pending', ?)",
                (excel_id, event_id, json.dumps(excel_row, default=str)),
            )

    def update(self, event_id: str, **fields) -> None:
        if not fields:
            return
        # Auto-stamp last_attempt_at
        fields.setdefault("last_attempt_at", _now())
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [event_id]
        with self._tx():
            self._conn.execute(f"UPDATE rows SET {cols} WHERE event_id=?", vals)

    def increment_attempts(self, event_id: str) -> None:
        with self._tx():
            self._conn.execute(
                "UPDATE rows SET attempts=attempts+1, last_attempt_at=? WHERE event_id=?",
                (_now(), event_id),
            )

    def get(self, event_id: str) -> Row | None:
        row = self._conn.execute(
            "SELECT * FROM rows WHERE event_id=?", (event_id,)
        ).fetchone()
        return Row(**dict(row)) if row else None

    def is_completed_or_submitted(self, event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT state FROM rows WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            return False
        return row["state"] in ("completed", "submitted", "pdf_in_drive", "analyzed")

    def fetch(self, states: Iterable[str], max_attempts: int | None = None) -> list[Row]:
        """Fetch rows in given states. If max_attempts set, excludes rows over it."""
        states = list(states)
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        sql = f"SELECT * FROM rows WHERE state IN ({placeholders})"
        params: list = list(states)
        if max_attempts is not None:
            sql += " AND attempts < ?"
            params.append(max_attempts)
        sql += " ORDER BY excel_id"
        return [Row(**dict(r)) for r in self._conn.execute(sql, params).fetchall()]

    def mark_timeout_failures(
        self,
        state_filter: str,
        older_than_hours: int,
        new_state: str,
    ) -> int:
        """Mark rows in `state_filter` whose webhook_submitted_at is older than the cutoff."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        with self._tx():
            cur = self._conn.execute(
                "UPDATE rows SET state=?, last_error=?, last_attempt_at=? "
                "WHERE state=? AND webhook_submitted_at IS NOT NULL AND webhook_submitted_at < ?",
                (new_state, f"timeout after {older_than_hours}h in {state_filter}", _now(), state_filter, cutoff),
            )
            return cur.rowcount

    # ── Reporting ──────────────────────────────────────────────────────────
    def status_summary(self) -> dict[str, int]:
        cur = self._conn.execute("SELECT state, COUNT(*) AS n FROM rows GROUP BY state ORDER BY n DESC")
        return {r["state"]: r["n"] for r in cur.fetchall()}

    def failures_listing(self) -> list[Row]:
        rows = self._conn.execute(
            "SELECT * FROM rows WHERE state LIKE 'failed_%' ORDER BY excel_id"
        ).fetchall()
        return [Row(**dict(r)) for r in rows]

    def reset(self) -> None:
        """Drop and recreate tables. Idempotency of the webhook prevents duplicate
        xAI work, but any local checkpoint will be lost."""
        with self._tx():
            self._conn.executescript(
                "DROP TABLE IF EXISTS rows; DROP TABLE IF EXISTS source_metadata;"
            )
            self._conn.executescript(SCHEMA)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
