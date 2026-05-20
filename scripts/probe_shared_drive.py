"""
Probe whether the auditoria-clases SA can read AND write the target
Shared Drive. Does a real (small) round-trip: create file, then delete it.
"""

import io
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

KEY_PATH = "./auditoria-clases-sa-key.json"
SHARED_DRIVE_ID = "0AO6wfjPBgovOUk9PVA"  # from the URL the user gave

SCOPES = ["https://www.googleapis.com/auth/drive"]  # full scope for write test


def main() -> None:
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    print("=" * 70)
    print("1) Identify the Shared Drive")
    print("=" * 70)
    try:
        d = drive.drives().get(driveId=SHARED_DRIVE_ID, fields="id,name,createdTime,capabilities").execute()
        print(f"  name        = {d.get('name')!r}")
        print(f"  id          = {d.get('id')}")
        print(f"  created     = {d.get('createdTime')}")
        caps = d.get("capabilities", {})
        print(f"  canAddChildren            = {caps.get('canAddChildren')}")
        print(f"  canEdit                   = {caps.get('canEdit')}")
        print(f"  canDeleteChildren         = {caps.get('canDeleteChildren')}")
        print(f"  canListChildren           = {caps.get('canListChildren')}")
        print(f"  canManageMembers          = {caps.get('canManageMembers')}")
        print(f"  canDownload               = {caps.get('canDownload')}")
    except HttpError as e:
        print(f"  [FAIL] drives().get  status={e.resp.status}  reason={str(e)[:200]}")
        return

    print()
    print("=" * 70)
    print("2) List files at the root of the Shared Drive")
    print("=" * 70)
    try:
        res = drive.files().list(
            corpora="drive",
            driveId=SHARED_DRIVE_ID,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=10,
            fields="files(id,name,mimeType)",
        ).execute()
        files = res.get("files", [])
        print(f"  files at root = {len(files)}")
        for f in files[:5]:
            print(f"    - {f.get('name')!r} ({f.get('mimeType')})")
    except HttpError as e:
        print(f"  [FAIL] files().list  status={e.resp.status}  reason={str(e)[:200]}")

    print()
    print("=" * 70)
    print("3) Write test: upload a tiny file, then delete it")
    print("=" * 70)
    test_name = f"sa-probe-{int(time.time())}.txt"
    created_id = None
    try:
        media = MediaIoBaseUpload(io.BytesIO(b"probe ok"), mimetype="text/plain")
        body = {"name": test_name, "parents": [SHARED_DRIVE_ID]}
        created = drive.files().create(
            body=body, media_body=media, supportsAllDrives=True, fields="id,name"
        ).execute()
        created_id = created["id"]
        print(f"  [OK]   created {test_name!r}  id={created_id}")
    except HttpError as e:
        print(f"  [FAIL] create  status={e.resp.status}  reason={str(e)[:200]}")

    if created_id:
        try:
            drive.files().delete(fileId=created_id, supportsAllDrives=True).execute()
            print(f"  [OK]   deleted test file")
        except HttpError as e:
            print(f"  [WARN] could not delete test file (id={created_id})  status={e.resp.status}")

    print()
    print("=" * 70)
    print("4) Identity check")
    print("=" * 70)
    try:
        about = drive.about().get(fields="user(emailAddress,displayName)").execute()
        print(f"  identity = {about.get('user',{}).get('emailAddress')}")
    except Exception as e:
        print(f"  about() failed: {e}")


if __name__ == "__main__":
    main()
