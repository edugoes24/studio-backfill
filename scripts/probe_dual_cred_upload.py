"""Test the GCS upload + signed URL flow end-to-end.

Identity model: a single ADC identity does upload AND signs the URL.
  - On the bastion: ADC = `edu-svc-observability-dev@...` (attached SA).
  - Locally: ADC = `eduardo.lopez@goes.gob.sv` (must have objectAdmin +
    iam.serviceAccountTokenCreator on some SA with bucket access for signing).

Steps:
  1. Confirm ADC identity.
  2. Upload a small probe file.
  3. Generate a v4 signed URL.
  4. Fetch the signed URL with no auth -- should return our bytes.
  5. Delete the probe file.

On bastion this should pass cleanly. Locally, signing usually fails because
the user has no SA to delegate signing to (signBlob requires SA impersonation).
"""

import sys
import time
from pathlib import Path

import requests
import google.auth
import google.auth.transport.requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_backfill.gcs_uploader import GcsUploader

BUCKET = "videos-and-transcripts-bucket"
PROJECT = "g-edu-room-mon-dev-prj-65cd"


def main() -> int:
    # 1) Confirm ADC identity
    try:
        creds, _ = google.auth.default()
        creds.refresh(google.auth.transport.requests.Request())
        identity = (
            getattr(creds, "service_account_email", None)
            or getattr(creds, "_service_account_email", None)
            or "<user ADC -- need to impersonate a SA for signing>"
        )
        print(f"[OK] ADC identity = {identity}")
    except Exception as e:
        print(f"[FAIL] ADC not configured: {e}")
        print("Run: gcloud auth application-default login")
        return 2

    uploader = GcsUploader(
        bucket_name=BUCKET,
        project=PROJECT,
        prefix="studio-backfill/test",
        signed_url_ttl_seconds=600,
    )
    print(f"[OK] uploader configured: bucket=gs://{BUCKET}")

    test_event_id = f"probe-{int(time.time())}"
    content = b"hola desde el probe"

    # 2) Upload
    try:
        gcs_uri, signed_url = uploader.upload(
            content, test_event_id, content_type="text/plain", extension="txt"
        )
        print(f"[OK] uploaded {gcs_uri}")
        print(f"     signed_url[:120] = {signed_url[:120]}...")
    except Exception as e:
        print(f"[FAIL] upload: {type(e).__name__}: {str(e)[:300]}")
        return 1

    # 3) Fetch the signed URL with no auth
    try:
        resp = requests.get(signed_url, timeout=30)
        if resp.status_code == 200 and resp.content == content:
            print(f"[OK] signed URL works -- bytes match ({len(resp.content)} bytes)")
        else:
            print(f"[FAIL] signed URL fetch: status={resp.status_code}")
            print(f"       body[:300]: {resp.text[:300]}")
    except Exception as e:
        print(f"[FAIL] signed URL fetch: {type(e).__name__}: {str(e)[:200]}")

    # 4) Cleanup
    try:
        uploader.delete(test_event_id, extension="txt")
        print(f"[OK] cleaned up probe blob")
    except Exception as e:
        print(f"[WARN] delete failed (non-critical): {e}")

    print()
    print("=" * 60)
    print("If [OK] on upload AND signed URL fetch: ready for pilot.")
    print("If signed URL fetched 403 'service account does not have objects.get':")
    print("  → run this probe from the bastion (its attached SA has bucket access).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
