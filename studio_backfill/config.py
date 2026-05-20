"""Centralized config loaded from .env_backfill (or process env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _ROOT / ".env_backfill"

if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing env var {name}. Set it in .env_backfill or the environment."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _int(name: str, default: int) -> int:
    raw = _optional(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _optional(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Drive
    sa_key_path: str
    shared_drive_id: str

    # GCS for transcripts
    gcs_bucket: str
    gcs_project: str
    gcs_prefix: str
    signed_url_ttl_seconds: int

    # Webhook xAI
    webhook_url: str
    webhook_secret: str
    webhook_rps: float

    # Reports backend
    reports_url: str

    # Pipeline tuning
    pipeline_timeout_hours: int
    poll_interval_seconds: int
    max_attempts: int

    # Run config
    excel_path: str
    state_db_path: str
    default_grade: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            sa_key_path=_require("SA_KEY_PATH"),
            shared_drive_id=_require("SHARED_DRIVE_ID"),
            gcs_bucket=_require("GCS_BUCKET_TRANSCRIPTS"),
            gcs_project=_require("GCS_BUCKET_PROJECT"),
            gcs_prefix=_optional("GCS_BUCKET_PREFIX", "studio-backfill"),
            signed_url_ttl_seconds=_int("GCS_SIGNED_URL_TTL_SECONDS", 21600),
            webhook_url=_require("WEBHOOK_URL"),
            webhook_secret=_require("WEBHOOK_SECRET"),
            webhook_rps=_float("WEBHOOK_RPS", 0.2),
            reports_url=_require("REPORTS_URL"),
            pipeline_timeout_hours=_int("PIPELINE_TIMEOUT_HOURS", 48),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 900),
            max_attempts=_int("MAX_ATTEMPTS", 5),
            excel_path=_require("EXCEL_PATH"),
            state_db_path=_require("STATE_DB_PATH"),
            default_grade=_optional("DEFAULT_GRADE", "No informado"),
        )

    @classmethod
    def load_partial(cls) -> "Settings | None":
        """Best-effort load that returns None if required vars are missing.

        Useful for offline tests of the resolver / state layer that don't need
        webhook / reports credentials.
        """
        try:
            return cls.load()
        except RuntimeError:
            return None
