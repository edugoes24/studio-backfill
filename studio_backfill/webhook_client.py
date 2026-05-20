"""HMAC-signed POST to the xAI webhook.

Mirrors the algorithm in goes/apps/video-engine/webhook/src/security/hmac.py:45-58:
  signing_string = f"{timestamp}.".encode() + body_bytes
  signature      = HMAC-SHA256(secret, signing_string).hexdigest()
  header         = f"sha256={signature}"

The server tolerates ±300s on the timestamp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import requests


class WebhookError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"webhook returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class WebhookClient:
    def __init__(self, url: str, secret: str, timeout_seconds: int = 30):
        # The secret WEBHOOK_URL already includes the /webhook path
        # (e.g. https://webhook.video-engine.staging.dev-goes.com/webhook).
        # Use as-is; only normalize trailing slash.
        self.url = url.rstrip("/")
        self._secret = secret.encode("utf-8")
        self.timeout = timeout_seconds

    def _sign(self, timestamp: str, body: bytes) -> str:
        signing_string = f"{timestamp}.".encode() + body
        sig = hmac.new(self._secret, signing_string, hashlib.sha256).hexdigest()
        return f"sha256={sig}"

    def post(self, payload: dict) -> dict:
        """Send the payload, return parsed JSON body of the 202 response.

        Raises WebhookError on non-2xx.
        """
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": self._sign(ts, body),
            "X-Webhook-Timestamp": ts,
        }
        resp = requests.post(self.url, data=body, headers=headers, timeout=self.timeout)
        if not resp.ok:
            raise WebhookError(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError:
            return {"status": "accepted", "raw_body": resp.text}


def build_payload(
    *,
    event_id: str,
    teacher_code: str,
    coach_code: str,
    school_code: str,
    grade: str,
    subject: str,
    recorded_at: str,
    signed_url: str,
    section: str | None = None,
    shift: str | None = None,
    lesson_number: int | None = None,
) -> dict:
    """Build the webhook payload per `webhook/src/schemas.py:17-100`."""
    params: dict = {
        "event_id": event_id,
        "teacher_code": teacher_code,
        "coach_code": coach_code,
        "school_code": school_code,
        "grade": grade,
        "subject": subject,
        "recorded_at": recorded_at,
    }
    if section is not None:
        params["section"] = section
    if shift is not None:
        params["shift"] = shift
    if lesson_number is not None:
        params["lesson_number"] = lesson_number
    return {"kwargs": {"signed_url": signed_url, "params": params}}
