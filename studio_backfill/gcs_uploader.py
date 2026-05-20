"""Upload transcripts to GCS and generate v4 signed URLs.

Runs as ADC (Application Default Credentials):
  - On the bastion VM, ADC = the attached SA `edu-svc-observability-dev@...`,
    which already has full GCS access on `videos-and-transcripts-bucket`
    (the same SA used by the Cloud Run service and the recording-watcher CF
    in production).
  - Locally, ADC = the developer's user account (must have storage.objectAdmin
    on the bucket + permission to sign URLs via IAM signBlob).

The same identity handles upload AND signs the URL, mirroring the production
pattern in `educacion_observability_backend_function/app/io/gcs.py::obtain_signed_url`.
"""

from __future__ import annotations

from datetime import timedelta

from google.cloud import storage


class GcsUploader:
    def __init__(
        self,
        bucket_name: str,
        project: str,
        prefix: str,
        signed_url_ttl_seconds: int,
    ):
        # Uses ADC. On bastion: the attached SA. Locally: developer's user account.
        self._client = storage.Client(project=project)
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

        # v4 signed URL. ADC handles signing via IAM signBlob (no private key needed
        # — works on a GCE VM with an attached SA, or with user creds that have
        # iam.serviceAccountTokenCreator on a SA).
        signed_url = blob.generate_signed_url(
            version="v4",
            method="GET",
            expiration=timedelta(seconds=self.ttl),
        )
        return gcs_uri, signed_url

    def delete(self, event_id: str, extension: str = "docx") -> None:
        """Best-effort delete (used by reset)."""
        blob_name = self.blob_path(event_id, ext=extension)
        try:
            self.bucket.blob(blob_name).delete()
        except Exception:
            pass
