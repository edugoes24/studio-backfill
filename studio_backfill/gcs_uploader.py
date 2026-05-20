"""Upload transcripts to GCS and generate v4 signed URLs.

Runs as ADC (Application Default Credentials). Two scenarios:
  - On a GCE VM with attached SA (the bastion): the credentials come from
    the metadata server and don't include a local private key, so signed
    URLs MUST be generated via IAM signBlob (we pass `service_account_email`
    and `access_token` to `generate_signed_url`).
  - Locally with SA key file: the credentials have a private key and the
    library signs offline. The same code path works (the extra parameters
    are no-ops when a private key is available).

Mirrors the production pattern in
`educacion_observability_backend_function/app/io/gcs.py::obtain_signed_url`.
"""

from __future__ import annotations

from datetime import timedelta

import google.auth
import google.auth.transport.requests
from google.cloud import storage


class GcsUploader:
    def __init__(
        self,
        bucket_name: str,
        project: str,
        prefix: str,
        signed_url_ttl_seconds: int,
    ):
        # Capture credentials once. We pass them to both the storage Client and
        # the generate_signed_url call so that signing on a GCE VM works via
        # IAM signBlob (the metadata-server creds don't carry a private key).
        self._creds, _ = google.auth.default()
        self._client = storage.Client(credentials=self._creds, project=project)
        self.bucket = self._client.bucket(bucket_name)
        self.prefix = prefix.strip("/")
        self.ttl = signed_url_ttl_seconds

    def blob_path(self, event_id: str, ext: str = "docx") -> str:
        return f"{self.prefix}/{event_id}/transcript.{ext}"

    def upload(
        self,
        content: bytes,
        event_id: str,
        content_type: str | None = None,
        extension: str = "docx",
    ) -> tuple[str, str]:
        """Upload `content` and return (gcs_uri, signed_url)."""
        blob_name = self.blob_path(event_id, ext=extension)
        blob = self.bucket.blob(blob_name)
        blob.upload_from_string(
            content,
            content_type=content_type
            or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        gcs_uri = f"gs://{self.bucket.name}/{blob_name}"
        signed_url = self._sign(blob)
        return gcs_uri, signed_url

    def _sign(self, blob) -> str:
        """Generate a v4 signed URL.

        On GCE with attached SA, the credentials don't carry a private key, so
        we have to provide `service_account_email` and `access_token` for the
        library to call IAM signBlob.
        """
        creds = self._creds
        if not getattr(creds, "token", None):
            creds.refresh(google.auth.transport.requests.Request())

        sa_email = getattr(creds, "service_account_email", None)
        access_token = getattr(creds, "token", None)

        kwargs = {
            "version": "v4",
            "method": "GET",
            "expiration": timedelta(seconds=self.ttl),
        }
        if sa_email and access_token:
            # IAM signBlob path (GCE VM / no private key).
            kwargs["service_account_email"] = sa_email
            kwargs["access_token"] = access_token

        return blob.generate_signed_url(**kwargs)

    def delete(self, event_id: str, extension: str = "docx") -> None:
        """Best-effort delete (used by reset)."""
        blob_name = self.blob_path(event_id, ext=extension)
        try:
            self.bucket.blob(blob_name).delete()
        except Exception:
            pass
