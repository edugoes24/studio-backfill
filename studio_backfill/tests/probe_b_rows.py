"""Find a working Case-B row (Drive file/d/, not mp4, SA can read)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader, excel_io

EXCEL = r"C:\Users\Eduardo Lopez\Downloads\studio_results_20260518_1308.xlsx"
SA_KEY = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"

drive = drive_reader.make_drive_client(SA_KEY)

candidates = []
for row in excel_io.read_rows(EXCEL):
    u = excel_io.parse_utilities(row)
    r = drive_reader.classify(u, drive=None)
    if r.case == "B":
        candidates.append((row["id"], r.file_id, r.transcription_url))

print(f"Total Case-B candidates: {len(candidates)}")
print()

for excel_id, file_id, url in candidates:
    mime = drive_reader.probe_mime(drive, file_id)
    marker = "[OK]" if mime and mime != drive_reader.MIME_MP4 else "[skip]"
    print(f"  {marker}  id={excel_id}  mime={mime}  url={url[:80]}")
