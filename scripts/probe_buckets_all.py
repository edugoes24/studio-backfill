"""Probe which video transcripts buckets the SA can write/read/delete in.

Tests DEV, QA and PRD buckets to determine the cheapest path forward.
"""

import io
import time
from datetime import timedelta

from google.oauth2 import service_account
from google.cloud import storage
from google.api_core.exceptions import GoogleAPIError

KEY_PATH = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"

BUCKETS = [
    ("DEV", "videos-and-transcripts-bucket", "g-edu-room-mon-dev-prj-65cd"),
    ("QA",  "videos-and-transcripts-bucket-qa", "g-edu-room-mon-qa-prj-65cd"),
    ("PRD", "videos-and-transcripts-bucket-prod", "g-edu-room-mon-prd-prj-65cd"),  # `-prod`, not `-prd`
]

PREFIX = "studio-backfill"


def test_bucket(label: str, bucket_name: str, project: str) -> dict:
    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
    client = storage.Client(credentials=creds, project=project)
    bucket = client.bucket(bucket_name)
    result = {"label": label, "name": bucket_name, "project": project,
              "exists": None, "list": None, "write": None, "read": None,
              "signed_url": None, "delete": None, "error": None}

    print(f"\n=== {label}: gs://{bucket_name} (project={project}) ===")

    # 0) Does the bucket exist + are we even authorized to see it?
    try:
        bucket.reload()
        result["exists"] = True
        print(f"  [OK]    bucket metadata fetched (location={bucket.location})")
    except GoogleAPIError as e:
        result["exists"] = False
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"  [FAIL]  bucket reload: {str(e)[:160]}")
        return result

    # 1) List
    try:
        list(bucket.list_blobs(prefix=PREFIX, max_results=1))
        result["list"] = True
        print(f"  [OK]    list (prefix={PREFIX}/)")
    except GoogleAPIError as e:
        result["list"] = False
        print(f"  [FAIL]  list: {str(e)[:120]}")

    # 2) Write
    test_blob_name = f"{PREFIX}/test/probe-{int(time.time())}.txt"
    blob = bucket.blob(test_blob_name)
    try:
        blob.upload_from_string(b"probe ok", content_type="text/plain")
        result["write"] = True
        print(f"  [OK]    wrote {test_blob_name}")
    except GoogleAPIError as e:
        result["write"] = False
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"  [FAIL]  write: {str(e)[:160]}")
        return result

    # 3) Read
    try:
        data = blob.download_as_bytes()
        result["read"] = (data == b"probe ok")
        print(f"  [{'OK' if result['read'] else 'WARN'}]    read back ({len(data)}b)")
    except GoogleAPIError as e:
        result["read"] = False
        print(f"  [FAIL]  read: {str(e)[:120]}")

    # 4) Signed URL — required by Fase 1 (webhook needs this)
    try:
        url = blob.generate_signed_url(
            version="v4", method="GET", expiration=timedelta(seconds=900),
        )
        result["signed_url"] = True
        print(f"  [OK]    signed URL generated ({len(url)} chars)")
    except Exception as e:
        result["signed_url"] = False
        print(f"  [FAIL]  signed URL: {str(e)[:160]}")

    # 5) Delete
    try:
        blob.delete()
        result["delete"] = True
        print(f"  [OK]    deleted test blob")
    except GoogleAPIError as e:
        result["delete"] = False
        print(f"  [FAIL]  delete: {str(e)[:120]}")

    return result


def main() -> None:
    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
    print(f"SA identity = {creds.service_account_email}\n")

    results = [test_bucket(*b) for b in BUCKETS]

    print("\n" + "=" * 70)
    print("Summary:")
    print("=" * 70)
    print(f"{'Env':4} {'Bucket':45} {'list':5} {'write':5} {'sign':5} {'del':5}")
    for r in results:
        def flag(v):
            if v is True: return "OK"
            if v is False: return "FAIL"
            return "-"
        print(f"{r['label']:4} {r['name']:45} {flag(r['list']):5} {flag(r['write']):5} "
              f"{flag(r['signed_url']):5} {flag(r['delete']):5}")


if __name__ == "__main__":
    main()
