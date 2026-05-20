"""
Probe whether the auditoria-clases SA can write/read/delete in
videos-and-transcripts-bucket (DEV) under prefix studio-backfill/.
"""

import io
import time

from google.oauth2 import service_account
from google.cloud import storage
from google.api_core.exceptions import GoogleAPIError

KEY_PATH = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"
BUCKET_NAME = "videos-and-transcripts-bucket"
PREFIX = "studio-backfill"


def main() -> None:
    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
    print(f"SA identity = {creds.service_account_email}")

    client = storage.Client(credentials=creds, project=creds.project_id)
    bucket = client.bucket(BUCKET_NAME)

    test_blob_name = f"{PREFIX}/test/probe-{int(time.time())}.txt"
    blob = bucket.blob(test_blob_name)

    # 1) WRITE
    try:
        blob.upload_from_string(b"probe ok", content_type="text/plain")
        print(f"[OK]   wrote gs://{BUCKET_NAME}/{test_blob_name}")
    except GoogleAPIError as e:
        print(f"[FAIL] write  {type(e).__name__}: {e}")
        return

    # 2) READ
    try:
        data = blob.download_as_bytes()
        print(f"[OK]   read back ({len(data)} bytes): {data!r}")
    except GoogleAPIError as e:
        print(f"[FAIL] read  {type(e).__name__}: {e}")

    # 3) SIGNED URL (v4) — needed for the webhook flow
    try:
        from datetime import timedelta
        url = blob.generate_signed_url(
            version="v4",
            method="GET",
            expiration=timedelta(seconds=900),
        )
        print(f"[OK]   signed URL generated ({len(url)} chars)")
    except Exception as e:
        # Common failure: SA needs roles/iam.serviceAccountTokenCreator on itself
        # or `--iam-account=<sa>` flag. We test this here because Fase 1 needs it.
        print(f"[FAIL] signed URL  {type(e).__name__}: {str(e)[:300]}")

    # 4) DELETE
    try:
        blob.delete()
        print(f"[OK]   deleted test blob")
    except GoogleAPIError as e:
        print(f"[FAIL] delete  {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
