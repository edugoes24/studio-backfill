"""Upload PDFs to the destination Shared Drive."""

from __future__ import annotations

import io

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload


class DriveWriter:
    def __init__(self, sa_key_path: str, shared_drive_id: str):
        creds = service_account.Credentials.from_service_account_file(
            sa_key_path,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.shared_drive_id = shared_drive_id

    def upload(self, pdf_bytes: bytes, event_id: str) -> tuple[str, str]:
        """Upload `pdf_bytes` as `reporte_<event_id>.pdf` to the Shared Drive.

        If a file with the same name exists, the new upload creates a sibling.
        Returns (drive_file_id, webViewLink).
        """
        name = f"reporte_{event_id}.pdf"
        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
        body = {"name": name, "parents": [self.shared_drive_id]}
        created = self._drive.files().create(
            body=body,
            media_body=media,
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        ).execute()
        return created["id"], created["webViewLink"]

    def find_existing(self, event_id: str) -> tuple[str, str] | None:
        """Return (id, webViewLink) of an already-uploaded report, or None."""
        name = f"reporte_{event_id}.pdf"
        try:
            res = self._drive.files().list(
                q=f"name='{name}' and '{self.shared_drive_id}' in parents and trashed=false",
                fields="files(id,name,webViewLink)",
                corpora="drive",
                driveId=self.shared_drive_id,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=1,
            ).execute()
        except HttpError:
            return None
        files = res.get("files", [])
        if files:
            return files[0]["id"], files[0]["webViewLink"]
        return None
