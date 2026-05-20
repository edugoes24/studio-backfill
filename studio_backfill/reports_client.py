"""Client for the reports backend.

Endpoint: GET /api/reports/session-pdf?event_id=<id>&lang=es

Status handling per plan section "Fase 2":
 - 200 → JSON {"url": "<signed-url>"} (because backend has GCS_BUCKET_NAME)
 - 422 → still processing OR terminal failure; parse body to distinguish
 - 404 → not in BigQuery yet (ETL hasn't run) → keep polling
 - 502 / 5xx → transient
 - 429 → rate limited
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class PdfFetchResult:
    status: int
    json_body: dict | None
    raw_text: str

    def is_terminal_422(self) -> bool:
        """Distinguishes 'xAI marked failed' from 'still processing'."""
        if self.status != 422 or not self.json_body:
            return False
        detail = self.json_body.get("detail") or {}
        if not isinstance(detail, dict):
            return False
        return (
            detail.get("pipeline_status") == "failed"
            or detail.get("session_status") == "failed"
            or detail.get("reason") == "data_incomplete"
        )

    @property
    def signed_pdf_url(self) -> str | None:
        if self.status == 200 and self.json_body:
            return self.json_body.get("url")
        return None


class ReportsClient:
    def __init__(self, base_url: str, timeout_seconds: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def get_pdf(self, event_id: str, lang: str = "es") -> PdfFetchResult:
        url = f"{self.base_url}/api/reports/session-pdf"
        resp = requests.get(url, params={"event_id": event_id, "lang": lang}, timeout=self.timeout)
        body_json: dict | None
        try:
            body_json = resp.json()
        except ValueError:
            body_json = None
        return PdfFetchResult(status=resp.status_code, json_body=body_json, raw_text=resp.text)

    def download_signed(self, signed_url: str) -> bytes:
        resp = requests.get(signed_url, timeout=120)
        resp.raise_for_status()
        return resp.content
