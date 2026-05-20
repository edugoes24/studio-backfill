"""List all real case-C and case-D1 rows in the Excel (rare cases)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader, excel_io

EXCEL = r"C:\Users\Eduardo Lopez\Downloads\studio_results_20260518_1308.xlsx"
SA_KEY = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"

drive = drive_reader.make_drive_client(SA_KEY)

for case in ("C", "D1"):
    print(f"\n=== Case {case} ===")
    for row in excel_io.read_rows(EXCEL):
        u = excel_io.parse_utilities(row)
        r = drive_reader.classify(u, drive=None)
        if r.case != case:
            continue
        print(f"\nid={row['id']}")
        print(f"  video         = {u.get('video','')[:90]}")
        print(f"  transcription = {u.get('transcription','')[:90]}")
        # Peek the folder
        folder_id = drive_reader._RE_FOLDER.match(u.get('transcription',''))
        if folder_id:
            fid = folder_id.group(1)
            try:
                children = drive.files().list(
                    q=f"'{fid}' in parents and trashed=false",
                    fields="files(id,name,mimeType)",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                    pageSize=20,
                ).execute().get("files", [])
                for c in children[:6]:
                    print(f"      - {c.get('name')[:70]!r} ({c.get('mimeType')})")
            except Exception as e:
                print(f"      [cannot list: {str(e)[:120]}]")
