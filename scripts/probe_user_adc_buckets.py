"""Probe user ADC write access to the three video-transcripts buckets.

Uses eduardo.lopez ADC (no SA key) to test storage.objects.create.
"""

import time
import warnings
from google.cloud import storage
from google.api_core.exceptions import GoogleAPIError

warnings.filterwarnings("ignore", category=UserWarning)

BUCKETS = [
    ("DEV", "videos-and-transcripts-bucket", "g-edu-room-mon-dev-prj-65cd"),
    ("QA",  "videos-and-transcripts-bucket-qa", "g-edu-room-mon-qa-prj-65cd"),
    ("PRD", "videos-and-transcripts-bucket-prod", "g-edu-room-mon-prd-prj-65cd"),
]

for label, name, project in BUCKETS:
    print(f"\n=== {label}: gs://{name} (project={project}) ===")
    client = storage.Client(project=project)
    bucket = client.bucket(name)

    # Skip reload — needs storage.buckets.get (broader). objectCreator alone
    # is enough to upload. Go straight to write.
    blob_name = f"studio-backfill/test/user-probe-{int(time.time())}.txt"
    blob = bucket.blob(blob_name)
    try:
        blob.upload_from_string(b"user adc probe", content_type="text/plain")
        print(f"  [OK]   wrote {blob_name}")
    except GoogleAPIError as e:
        print(f"  [FAIL] write: {str(e)[:240]}")
        continue

    # Read back
    try:
        data = blob.download_as_bytes()
        print(f"  [OK]   read back ({len(data)}b)")
    except GoogleAPIError as e:
        print(f"  [WARN] read: {str(e)[:160]}")

    # Delete
    try:
        blob.delete()
        print(f"  [OK]   deleted test blob")
    except Exception as e:
        print(f"  [WARN] delete: {str(e)[:160]}")
