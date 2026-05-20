"""End-to-end orchestrator. See plan section "Detalle por fase"."""

from __future__ import annotations

import logging
import time
from typing import Iterable

from . import drive_reader, excel_io
from .config import Settings
from .drive_writer import DriveWriter
from .gcs_uploader import GcsUploader
from .reports_client import ReportsClient
from .state import StateStore
from .webhook_client import WebhookClient, WebhookError, build_payload

log = logging.getLogger("studio_backfill.pipeline")


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

    # ── Fase 1 ─────────────────────────────────────────────────────────────
    def submit_row(self, state: StateStore, excel_row: dict) -> None:
        """Execute Phase 1 for a single row. Idempotent: skips already-submitted."""
        excel_id = int(excel_row["id"])
        event_id = f"studio-{excel_id}"
        state.upsert_pending(excel_id, excel_row)

        if state.is_completed_or_submitted(event_id):
            log.info("skip already-submitted event_id=%s", event_id)
            return

        try:
            utilities = excel_io.parse_utilities(excel_row)
            r = drive_reader.classify(utilities, drive=self.drive)

            if r.case == "E":
                state.update(event_id, state="skipped_empty_link",
                             last_error=r.diagnostic)
                return
            if r.case == "F":
                state.update(
                    event_id, state="skipped_unsupported_format",
                    last_error=f"[{r.sub_variant}] transcription no soportado: {r.transcription_url}",
                )
                return
            if r.case in ("C", "D1") and r.file_id is None:
                state.update(event_id, state="skipped_no_transcript_in_folder",
                             last_error=f"carpeta {r.folder_id} no contiene Doc ni .docx")
                return

            # Case B / D2 — mime unknown up front; resolve now
            mime = r.mime
            if r.case in ("B", "D2") and mime is None:
                mime = drive_reader.probe_mime(self.drive, r.file_id)

            if r.case == "D2" and mime == drive_reader.MIME_MP4:
                state.update(event_id, state="skipped_no_transcript",
                             last_error="file/d único es .mp4, no hay transcript")
                return

            if r.case == "B" and mime == drive_reader.MIME_MP4:
                state.update(event_id, state="skipped_no_transcript",
                             last_error="file/d directo es .mp4, no hay transcript")
                return

            state.update(event_id, drive_case=r.case, transcript_file_id=r.file_id)

            # Download from Drive (returns bytes + content_type + extension)
            try:
                bytes_, content_type, extension = drive_reader.download_transcript(
                    self.drive, r.file_id, mime
                )
            except Exception as e:
                state.update(event_id, state="failed_drive_read", last_error=str(e))
                state.increment_attempts(event_id)
                return

            # Upload to GCS + signed URL
            try:
                gcs_uri, signed_url = self.gcs.upload(
                    bytes_, event_id, content_type=content_type, extension=extension,
                )
            except Exception as e:
                state.update(event_id, state="failed_gcs_upload", last_error=str(e))
                state.increment_attempts(event_id)
                return
            state.update(event_id, transcript_gcs_uri=gcs_uri, state="transcript_uploaded")

            # POST webhook
            payload = self._build_payload(excel_row, signed_url, event_id)
            try:
                resp = self.webhook.post(payload)
            except WebhookError as e:
                state.update(event_id, state="failed_webhook",
                             last_error=f"{e.status}: {e.body[:300]}")
                state.increment_attempts(event_id)
                return
            except Exception as e:
                state.update(event_id, state="failed_webhook", last_error=str(e))
                state.increment_attempts(event_id)
                return

            from datetime import datetime, timezone
            state.update(
                event_id,
                webhook_message_id=str(resp.get("message_id", "")),
                webhook_submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                state="submitted",
            )
        except Exception as e:
            state.update(event_id, state="failed_phase1", last_error=str(e))
            state.increment_attempts(event_id)

    def submit_all(self, state: StateStore, excel_rows: Iterable[dict]) -> None:
        """Run Phase 1 for all rows with throttle = WEBHOOK_RPS."""
        delay = 1.0 / max(self.settings.webhook_rps, 0.0001)
        last = 0.0
        for row in excel_rows:
            # Throttle
            now = time.monotonic()
            wait = delay - (now - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            self.submit_row(state, row)

    def _build_payload(self, excel_row: dict, signed_url: str, event_id: str) -> dict:
        payload = excel_io.parse_payload(excel_row)
        return build_payload(
            event_id=event_id,
            teacher_code=f"studio-teacher-{excel_row['teacherId']}",
            coach_code=f"studio-tutor-{excel_row['tutorId']}",
            school_code=f"studio-school-{excel_row['schoolCode']}",
            grade=self.settings.default_grade,
            subject=payload.get("subject", "No informado"),
            recorded_at=str(excel_row.get("submittedAt", "")),
            signed_url=signed_url,
        )

    # ── Fase 2 ─────────────────────────────────────────────────────────────
    def collect_once(self, state: StateStore) -> int:
        """Single pass of Phase 2. Returns count of rows that became 'completed'."""
        # Mark timeouts before polling
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
                # Still processing — keep as submitted, no attempts++
                state.update(row.event_id, state="submitted")
            elif resp.status == 404:
                state.update(row.event_id, state="failed_pdf_404",
                             last_error="not in BQ yet")
            elif resp.status == 429:
                # Backoff politely, do NOT count as attempt
                log.warning("429 from reports backend for %s — backoff", row.event_id)
                time.sleep(30)
            else:
                # 5xx, etc. — transient, count as attempt
                state.update(row.event_id, state="failed_pdf_fetch",
                             last_error=f"HTTP {resp.status}: {resp.raw_text[:200]}")
                state.increment_attempts(row.event_id)

        return completed

    def collect_until_drained(self, state: StateStore) -> None:
        """Keep running collect_once with sleep until no rows remain pending."""
        while True:
            n = self.collect_once(state)
            log.info("collect pass: %d completed this round", n)
            # Are there still pending rows to poll?
            remaining = (
                len(state.fetch(states=["submitted", "failed_pdf_404"]))
                + len(state.fetch(states=["failed_pdf_fetch"], max_attempts=self.settings.max_attempts))
            )
            if remaining == 0:
                break
            log.info("sleeping %ds before next poll (%d remaining)",
                     self.settings.poll_interval_seconds, remaining)
            time.sleep(self.settings.poll_interval_seconds)
