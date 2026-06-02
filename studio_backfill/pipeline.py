"""End-to-end orchestrator. See plan section "Detalle por fase"."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Iterable

from . import drive_reader
from .config import Settings
from .drive_writer import DriveWriter
from .gcs_uploader import GcsUploader
from .reports_client import ReportsClient
from .state import StateStore
from .webhook_client import WebhookClient, WebhookError, build_payload

log = logging.getLogger("studio_backfill.pipeline")


def slug(name: str | None) -> str:
    """Normalize a free-form name to an ASCII-safe slug.

    "LÓPEZ VELASCO, CARLOS MAURICIO" -> "LOPEZ-VELASCO-CARLOS-MAURICIO"
    Returns "UNKNOWN" if the input is empty.
    """
    if not name:
        return "UNKNOWN"
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").upper()
    return s or "UNKNOWN"


def _clean_str(value) -> str | None:
    """Return a clean string suitable for the webhook display fields,
    or None if the value is empty / "Sin dato"."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "sin dato":
        return None
    return s


class Pipeline:
    """Holds long-lived clients shared across rows."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.drive = drive_reader.make_drive_client(settings.sa_key_path)
        self.gcs = GcsUploader(
            settings.gcs_bucket,
            settings.gcs_project,
            settings.gcs_prefix,
            settings.signed_url_ttl_seconds,
        )
        self.webhook = WebhookClient(settings.webhook_url, settings.webhook_secret)
        self.reports = ReportsClient(settings.reports_url)
        self.drive_writer = DriveWriter(settings.sa_key_path, settings.shared_drive_id)

    def _event_id(self, row_position: int) -> str:
        """Build the cross-system event_id. The prefix is configurable via
        EVENT_ID_PREFIX (default 'studio-row') so a re-run can use fresh ids
        (e.g. 'studio-row-v2') that xAI treats as new sessions."""
        return f"{self.settings.event_id_prefix}-{row_position}"

    # ── Fase 1 ─────────────────────────────────────────────────────────────
    def submit_row(self, state: StateStore, excel_row: dict) -> bool:
        """Execute Phase 1 for a single row. Idempotent: skips already-submitted.

        Returns True if a webhook POST was actually attempted (success or fail),
        False if the row skipped early (E/F/already-submitted/no-transcript).
        submit_all uses this to throttle only on real webhook calls.
        """
        row_position = int(excel_row["_row_position"])
        event_id = self._event_id(row_position)
        state.upsert_pending(row_position, event_id, excel_row)

        if state.is_completed_or_submitted(event_id):
            log.info("skip already-submitted event_id=%s", event_id)
            return False

        try:
            r = drive_reader.classify(
                transcript_link=excel_row.get("transcriptLink"),
                video_link=excel_row.get("videoLink"),
                drive=self.drive,
            )

            if r.case == "E":
                state.update(event_id, state="skipped_no_link",
                             last_error=r.diagnostic)
                return False
            if r.case == "F":
                state.update(
                    event_id, state="skipped_unsupported_format",
                    last_error=f"[{r.sub_variant}] transcript no soportado: {r.transcript_url}",
                )
                return False
            if r.case in ("C", "D1") and r.file_id is None:
                state.update(event_id, state="skipped_no_transcript_in_folder",
                             last_error=f"carpeta {r.folder_id} no contiene Doc/.docx/.txt")
                return False

            # Case B / D2 — mime unknown up front; resolve now
            mime = r.mime
            if r.case in ("B", "D2") and mime is None:
                mime = drive_reader.probe_mime(self.drive, r.file_id)

            if r.case in ("B", "D2") and mime == drive_reader.MIME_MP4:
                state.update(event_id, state="skipped_no_transcript",
                             last_error=f"file/d is video/mp4 (case {r.case}), no transcript")
                return False

            state.update(event_id, drive_case=r.case, transcript_file_id=r.file_id)

            # Download from Drive
            try:
                bytes_, content_type, extension = drive_reader.download_transcript(
                    self.drive, r.file_id, mime
                )
            except Exception as e:
                state.update(event_id, state="failed_drive_read", last_error=str(e))
                state.increment_attempts(event_id)
                return False

            # Upload to GCS + signed URL
            try:
                gcs_uri, signed_url = self.gcs.upload(
                    bytes_, event_id, content_type=content_type, extension=extension,
                )
            except Exception as e:
                state.update(event_id, state="failed_gcs_upload", last_error=str(e))
                state.increment_attempts(event_id)
                return False
            state.update(event_id, transcript_gcs_uri=gcs_uri, state="transcript_uploaded")

            # POST webhook (this is the only point where we hit external rate-limited resources)
            payload = self._build_payload(excel_row, row_position, signed_url)
            try:
                resp = self.webhook.post(payload)
            except WebhookError as e:
                state.update(event_id, state="failed_webhook",
                             last_error=f"{e.status}: {e.body[:300]}")
                state.increment_attempts(event_id)
                return True  # attempted the POST; counts toward throttle
            except Exception as e:
                state.update(event_id, state="failed_webhook", last_error=str(e))
                state.increment_attempts(event_id)
                return True

            state.update(
                event_id,
                webhook_message_id=str(resp.get("message_id", "")),
                webhook_submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                state="submitted",
            )
            return True
        except Exception as e:
            state.update(event_id, state="failed_phase1", last_error=str(e))
            state.increment_attempts(event_id)
            return False  # unknown if webhook was attempted; conservative: don't throttle

    def submit_all(self, state: StateStore, excel_rows: Iterable[dict]) -> None:
        """Run Phase 1 for all rows. Throttle applies ONLY to rows that hit
        the webhook (skipped rows pass through instantly).
        """
        delay = 1.0 / max(self.settings.webhook_rps, 0.0001)
        last_webhook = 0.0
        for row in excel_rows:
            # Wait only if the previous iteration actually hit the webhook
            # and not enough time has passed since.
            if last_webhook > 0:
                wait = delay - (time.monotonic() - last_webhook)
                if wait > 0:
                    time.sleep(wait)

            did_post = self.submit_row(state, row)
            if did_post:
                last_webhook = time.monotonic()

    def _build_payload(self, excel_row: dict, row_position: int, signed_url: str) -> dict:
        """Build the webhook payload from the new flat Excel columns.

        - event_id from row_position (M-1)
        - codes from slugified names + infrastructureCode (M-4)
        - recorded_at = UTC now (M-2)
        - grade/section/shift/subject directly from the Excel (M-6)
        - display names (teacher, coach, school, department, district) sent
          as optional fields; used by xAI feat/webhook-forward-entity-names
          to populate dim_users/dim_schools so the PDF shows names. Silently
          ignored if the deployed webhook is on main.
        """
        return build_payload(
            event_id=self._event_id(row_position),
            teacher_code=f"studio-teacher-{slug(excel_row.get('teacher'))}",
            coach_code=f"studio-tutor-{slug(excel_row.get('coach'))}",
            school_code=f"studio-school-{excel_row.get('infrastructureCode')}",
            grade=str(excel_row.get("grade") or self.settings.default_grade),
            subject=str(excel_row.get("subject") or "No informado"),
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            signed_url=signed_url,
            section=(str(excel_row["section"]) if excel_row.get("section") else None),
            shift=(str(excel_row["shift"]) if excel_row.get("shift") else None),
            teacher_name=_clean_str(excel_row.get("teacher")),
            coach_name=_clean_str(excel_row.get("coach")),
            school_name=_clean_str(excel_row.get("school")),
            school_department=_clean_str(excel_row.get("department")),
            school_district=_clean_str(excel_row.get("district")),
        )

    # ── Fase 2 ─────────────────────────────────────────────────────────────
    def collect_once(self, state: StateStore) -> int:
        """Single pass of Phase 2. Returns count of rows that became 'completed'."""
        moved = state.mark_timeout_failures(
            state_filter="submitted",
            older_than_hours=self.settings.pipeline_timeout_hours,
            new_state="failed_timeout_pending_analysis",
        )
        if moved:
            log.warning("marked %d rows as failed_timeout_pending_analysis", moved)

        pending = (
            state.fetch(states=["submitted", "failed_pdf_404"])
            + state.fetch(states=["failed_pdf_fetch"], max_attempts=self.settings.max_attempts)
        )

        completed = 0
        for row in pending:
            try:
                resp = self.reports.get_pdf(row.event_id)
            except Exception as e:
                state.update(row.event_id, state="failed_pdf_fetch", last_error=str(e))
                state.increment_attempts(row.event_id)
                continue

            if resp.status == 200 and resp.signed_pdf_url:
                try:
                    pdf_bytes = self.reports.download_signed(resp.signed_pdf_url)
                    drive_id, drive_link = self.drive_writer.upload(pdf_bytes, row.event_id)
                except Exception as e:
                    state.update(row.event_id, state="failed_drive_upload", last_error=str(e))
                    state.increment_attempts(row.event_id)
                    continue
                state.update(
                    row.event_id,
                    pdf_drive_id=drive_id,
                    pdf_drive_link=drive_link,
                    state="completed",
                )
                completed += 1
            elif resp.status == 422 and resp.is_terminal_422():
                state.update(row.event_id, state="failed_pdf_analysis",
                             last_error=f"xAI terminal: {resp.json_body}")
            elif resp.status == 422:
                state.update(row.event_id, state="submitted")
            elif resp.status == 404:
                state.update(row.event_id, state="failed_pdf_404",
                             last_error="not in BQ yet")
            elif resp.status == 429:
                log.warning("429 from reports backend for %s — backoff", row.event_id)
                time.sleep(30)
            else:
                state.update(row.event_id, state="failed_pdf_fetch",
                             last_error=f"HTTP {resp.status}: {resp.raw_text[:200]}")
                state.increment_attempts(row.event_id)

        return completed

    def collect_until_drained(self, state: StateStore) -> None:
        """Keep running collect_once with sleep until no rows remain pending."""
        while True:
            n = self.collect_once(state)
            log.info("collect pass: %d completed this round", n)
            remaining = (
                len(state.fetch(states=["submitted", "failed_pdf_404"]))
                + len(state.fetch(states=["failed_pdf_fetch"], max_attempts=self.settings.max_attempts))
            )
            if remaining == 0:
                break
            log.info("sleeping %ds before next poll (%d remaining)",
                     self.settings.poll_interval_seconds, remaining)
            time.sleep(self.settings.poll_interval_seconds)
