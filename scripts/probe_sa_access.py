"""
Probe whether the auditoria-clases SA can access the Drive files referenced
in the studio_results Excel. No downloads — only metadata + folder listing.
"""

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

KEY_PATH = "./auditoria-clases-sa-key.json"

# Sampled from the first rows of studio_results_20260518_1308.xlsx
TARGETS = [
    ("row2-transcript (Google Doc)", "1xmSBW2tVR2SiyPtV-_g6gm6ehHtV7FvSiMfXD6zsTLQ", "file"),
    ("row2-video (Drive file)", "1ZCUJOPRwebWu8XB1yt2-oxefGxBg-XeZ", "file"),
    ("row3-video (Drive folder)", "1vY9qENi0qo86XGe8P8LRAhQ72dNnyaz1", "folder"),
    ("row3-transcript (Drive file)", "17rups5xMgGt1UG_Z1IDCa1yMmzGcrUwV", "file"),
    ("row4-both (Drive folder)", "1GQtv8yJs4IgSOqTJumLaL3e-MWr2J0Jq", "folder"),
]

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def make_drive(subject: str | None = None):
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
    if subject:
        creds = creds.with_subject(subject)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def probe(drive, label: str, file_id: str, kind: str) -> None:
    try:
        meta = drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType,owners(emailAddress),driveId,shared,trashed",
            supportsAllDrives=True,
        ).execute()
        print(f"  [OK]  {label}")
        print(
            f"        name={meta.get('name')!r}  mime={meta.get('mimeType')}  "
            f"driveId={meta.get('driveId')}  trashed={meta.get('trashed')}"
        )
        owners = meta.get("owners") or []
        if owners:
            print(f"        owner={owners[0].get('emailAddress')}")
        if kind == "folder":
            children = drive.files().list(
                q=f"'{file_id}' in parents and trashed=false",
                fields="files(id,name,mimeType,size)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute().get("files", [])
            print(f"        children={len(children)}")
            for c in children[:5]:
                print(f"           - {c.get('name')!r} ({c.get('mimeType')})")
    except HttpError as e:
        print(f"  [FAIL] {label}  status={e.resp.status}  reason={e.error_details if hasattr(e,'error_details') else str(e)[:160]}")
    except Exception as e:
        print(f"  [ERR ] {label}  {type(e).__name__}: {e}")


def main() -> None:
    print("=" * 70)
    print("Mode 1: Direct SA auth (no Domain-Wide Delegation)")
    print("=" * 70)
    drive = make_drive()
    for label, fid, kind in TARGETS:
        probe(drive, label, fid, kind)

    print()
    print("About info — what scopes this SA can see for itself:")
    try:
        about = drive.about().get(fields="user(emailAddress),storageQuota").execute()
        print(f"  identity={about.get('user',{}).get('emailAddress')}")
    except Exception as e:
        print(f"  about() failed: {e}")


if __name__ == "__main__":
    main()
