"""Step-by-step walkthrough of what the pipeline does for ONE Excel row.

Picks id=6919 (Case A, Google Doc) and prints inputs/outputs at each stage.
Stops gracefully at GCS upload (blocked on IAM binding) and prints what would
happen for webhook + reports + drive write.
"""

import json
import sys
import time
import hmac
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader, excel_io, webhook_client

EXCEL = r"C:\Users\Eduardo Lopez\Downloads\studio_results_20260518_1308.xlsx"
SA_KEY = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"
TARGET_ID = 6919


def divider(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def truncate(s: str, n: int = 100) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# ─── STEP 0: read the row from the Excel ────────────────────────────────────
divider(f"STEP 0 -- Read row id={TARGET_ID} from Excel")

row = None
for r in excel_io.read_rows(EXCEL):
    if int(r["id"]) == TARGET_ID:
        row = r
        break

if row is None:
    print(f"id={TARGET_ID} not found")
    sys.exit(1)

print("Excel columns:")
for k, v in row.items():
    sv = str(v)
    print(f"  {k:14} = {truncate(sv, 90)}")


# ─── STEP 1: parse utilitiesLink + payload ─────────────────────────────────
divider("STEP 1 -- Parse utilitiesLink JSON and payload JSON")
utilities = excel_io.parse_utilities(row)
payload = excel_io.parse_payload(row)
print("utilitiesLink parsed:")
print(f"  id            = {utilities.get('id')}")
print(f"  video         = {truncate(utilities.get('video', ''), 90)}")
print(f"  transcription = {truncate(utilities.get('transcription', ''), 90)}")
print()
print("payload parsed:")
print(f"  subject = {payload.get('subject')}")
print(f"  score   = {payload.get('score')}")
print(f"  answers = {len(payload.get('answers', {}))} respuestas")


# ─── STEP 2: classify (resolver) ───────────────────────────────────────────
divider("STEP 2 -- drive_reader.classify(utilitiesLink)")
result = drive_reader.classify(utilities, drive=None)
print("ResolveResult:")
print(f"  case              = {result.case}")
print(f"  file_id           = {result.file_id}")
print(f"  mime              = {result.mime}")
print(f"  transcription_url = {truncate(result.transcription_url, 90)}")
print(f"  folder_id         = {result.folder_id}")
print(f"  sub_variant       = {result.sub_variant}")
print(f"  diagnostic        = {result.diagnostic}")


# ─── STEP 3: branch -- check skip cases ──────────────────────────────────────
divider("STEP 3 -- Pipeline branches: would we skip?")
if result.case == "E":
    print("Would skip: skipped_empty_link")
    sys.exit(0)
if result.case == "F":
    print(f"Would skip: skipped_unsupported_format ({result.sub_variant})")
    sys.exit(0)
print(f"case={result.case} -> NOT a skip. Proceed to Drive download.")


# ─── STEP 4: Drive download ────────────────────────────────────────────────
divider("STEP 4 -- drive_reader.download_transcript(drive, file_id, mime)")
drive = drive_reader.make_drive_client(SA_KEY)
t0 = time.monotonic()
bytes_, content_type, ext = drive_reader.download_transcript(
    drive, result.file_id, result.mime
)
dt = time.monotonic() - t0
print(f"Downloaded:")
print(f"  size         = {len(bytes_)} bytes")
print(f"  content_type = {content_type}")
print(f"  extension    = .{ext}")
print(f"  elapsed      = {dt:.2f}s")
print(f"  first bytes  = {bytes_[:32]!r} ...")


# ─── STEP 5: build event_id + GCS path ─────────────────────────────────────
divider("STEP 5 -- Compute event_id and GCS path")
event_id = f"studio-{row['id']}"
bucket = "videos-and-transcripts-bucket"  # DEV
prefix = "studio-backfill"
blob_name = f"{prefix}/{event_id}/transcript.{ext}"
gcs_uri = f"gs://{bucket}/{blob_name}"
print(f"event_id   = {event_id}")
print(f"GCS bucket = {bucket}")
print(f"blob path  = {blob_name}")
print(f"gcs_uri    = {gcs_uri}")


# ─── STEP 6: GCS upload + signed URL ───────────────────────────────────────
divider("STEP 6 -- gcs_uploader.upload(bytes_, event_id, ...)")
print(f"WOULD execute (currently BLOCKED on IAM binding for the SA):")
print()
print(f"  bucket.blob('{blob_name}').upload_from_string(")
print(f"      content={len(bytes_)}b,  content_type='{content_type}'")
print(f"  )")
print()
print(f"  signed_url = blob.generate_signed_url(")
print(f"      version='v4', method='GET',")
print(f"      expiration=timedelta(seconds=21600)   # 6h")
print(f"  )")
print()
print(f"Expected signed URL shape:")
print(f"  https://storage.googleapis.com/{bucket}/{blob_name}?")
print(f"    X-Goog-Algorithm=GOOG4-RSA-SHA256")
print(f"    &X-Goog-Credential=auditoria-clases%40g-edu-room-mon-prd-prj-65cd...")
print(f"    &X-Goog-Date=<timestamp>")
print(f"    &X-Goog-Expires=21600")
print(f"    &X-Goog-Signature=<...>")

# Use a fake signed URL for the next steps' demo
signed_url = f"https://storage.googleapis.com/{bucket}/{blob_name}?X-Goog-Algorithm=GOOG4-RSA-SHA256&...&X-Goog-Expires=21600&X-Goog-Signature=DEMO"


# ─── STEP 7: build webhook payload ─────────────────────────────────────────
divider("STEP 7 -- webhook_client.build_payload(...)")
body = webhook_client.build_payload(
    event_id=event_id,
    teacher_code=f"studio-teacher-{row['teacherId']}",
    coach_code=f"studio-tutor-{row['tutorId']}",
    school_code=f"studio-school-{row['schoolCode']}",
    grade="No informado",
    subject=payload.get("subject", "No informado"),
    recorded_at=str(row.get("submittedAt", "")),
    signed_url=signed_url,
)
print("Body to POST:")
print(json.dumps(body, indent=2, default=str)[:1500])


# ─── STEP 8: HMAC signature ────────────────────────────────────────────────
divider("STEP 8 -- HMAC-SHA256 signature for the webhook POST")
demo_secret = "DEMO_WEBHOOK_SECRET_xxxxxxxxxxxxxxxx"  # real one from Secret Manager
body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
ts = str(int(time.time()))
signing_string = f"{ts}.".encode() + body_bytes
sig = hmac.new(demo_secret.encode(), signing_string, hashlib.sha256).hexdigest()

print(f"Algorithm:        HMAC-SHA256")
print(f"Secret:           {demo_secret[:25]}...  (real value en .env_backfill)")
print(f"Body bytes:       {len(body_bytes)} bytes JSON canónico")
print(f"Timestamp:        {ts}  (unix seconds)")
print(f"Signing string:   '{ts}.' + body_bytes  ({len(signing_string)} bytes)")
print(f"Signature:        sha256={sig}")
print()
print("Headers que enviaríamos:")
print(f"  Content-Type:       application/json")
print(f"  X-Webhook-Signature: sha256={sig[:32]}...")
print(f"  X-Webhook-Timestamp: {ts}")


# ─── STEP 9: POST webhook ──────────────────────────────────────────────────
divider("STEP 9 -- POST to webhook xAI")
print("WOULD execute (currently BLOCKED on WEBHOOK_URL secret):")
print()
print(f"  requests.post(")
print(f"      url=<WEBHOOK_URL>/webhook,")
print(f"      data=<body_bytes {len(body_bytes)}b>,")
print(f"      headers=<above>,")
print(f"      timeout=30,")
print(f"  )")
print()
print(f"Expected response: HTTP 202 Accepted")
print(f"  {{")
print(f'    "status": "accepted",')
print(f'    "message_id": "<pubsub-message-id>",')
print(f'    "event_id": "{event_id}",')
print(f'    "message": "Transcript processing request queued successfully"')
print(f"  }}")


# ─── STEP 10: state.sqlite checkpoint ──────────────────────────────────────
divider("STEP 10 -- state.sqlite checkpoint after Phase 1")
print(f"After successful POST, we run:")
print(f"  state.update('{event_id}',")
print(f"      webhook_message_id='<msg-id>',")
print(f"      webhook_submitted_at='2026-05-20T...',")
print(f"      state='submitted',")
print(f"  )")
print()
print(f"Row in state.sqlite:")
print(f"  excel_id            = {row['id']}")
print(f"  event_id            = {event_id}")
print(f"  drive_case          = {result.case}")
print(f"  transcript_file_id  = {result.file_id}")
print(f"  transcript_gcs_uri  = {gcs_uri}")
print(f"  webhook_message_id  = <msg-id>")
print(f"  webhook_submitted_at = 2026-05-20T...")
print(f"  state               = submitted")
print(f"  attempts            = 0")


# ─── STEP 11: PHASE 2 -- wait + poll reports backend ────────────────────────
divider("STEP 11 -- PHASE 2 (after xAI finishes the batch, horas-days later)")
print(f"Para cada fila en estado 'submitted', cada 15min:")
print()
print(f"  GET https://educacion-observabilidad-tutorias-reportes-407756486122.us-east1.run.app")
print(f"      /api/reports/session-pdf?event_id={event_id}&lang=es")
print()
print(f"Posibles respuestas:")
print(f"  HTTP 422 + body {{detail:{{pipeline_status:'submitted'}}}}")
print(f"     -> still processing en xAI, poll otra vez (NO attempts++)")
print(f"  HTTP 422 + body {{detail:{{pipeline_status:'failed'}}}}")
print(f"     -> terminal, mark state='failed_pdf_analysis'")
print(f"  HTTP 404")
print(f"     -> ETL no ha movido data a BigQuery, poll otra vez (NO attempts++)")
print(f"  HTTP 200 + body {{url:'<signed-pdf-url>'}}")
print(f"     -> reporte listo! Procedo a STEP 12")
print(f"  Cualquiera > 48h en 'submitted'")
print(f"     -> mark state='failed_timeout_pending_analysis'")


# ─── STEP 12: Download PDF + upload to Drive ───────────────────────────────
divider("STEP 12 -- Download PDF + upload to Shared Drive")
print(f"Cuando reports backend devuelve 200 con signed URL del PDF:")
print()
print(f"  pdf_bytes = requests.get('<signed-pdf-url>').content   # ~2 MB típico")
print()
print(f"  drive_id, drive_link = drive_writer.upload(pdf_bytes, '{event_id}')")
print(f"  -> crea archivo 'reporte_{event_id}.pdf' en Shared Drive")
print(f"     0AO6wfjPBgovOUk9PVA")
print()
print(f"Returns:")
print(f"  drive_id   = <Drive fileId>")
print(f"  drive_link = https://drive.google.com/file/d/<fileId>/view?usp=drivesdk")


# ─── STEP 13: final state ──────────────────────────────────────────────────
divider("STEP 13 -- Final state.sqlite")
print(f"  state.update('{event_id}',")
print(f"      pdf_drive_id='<fileId>',")
print(f"      pdf_drive_link='https://drive.google.com/file/d/<fileId>/view...',")
print(f"      state='completed',")
print(f"  )")


# ─── STEP 14: Excel de salida ─────────────────────────────────────────────
divider("STEP 14 -- At the very end: write Excel with the new column")
print(f"Fase 3 (`cli write-excel`) recorre state.sqlite y genera:")
print(f"  studio_results_20260518_1308_with_reports.xlsx")
print(f"")
print(f"Columnas nuevas para id={row['id']}:")
print(f"  event_id_xai     = {event_id}")
print(f"  pdf_drive_link   = https://drive.google.com/file/d/<fileId>/view...")
print(f"  backfill_status  = completed")
