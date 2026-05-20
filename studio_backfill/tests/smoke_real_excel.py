"""Smoke test: run resolver + Drive download against real Excel rows.

Skips GCS upload and webhook POST (those need network access we may not have
from laptop). Just validates classification + Drive read for the 10 pilot ids
plus a sample of A-case rows.

Run:
    python studio_backfill/tests/smoke_real_excel.py
"""

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader, excel_io


EXCEL_PATH = r"C:\Users\Eduardo Lopez\Downloads\studio_results_20260518_1308.xlsx"
SA_KEY = r"C:\Users\Eduardo Lopez\Documents\GitHub\studio_backfill\auditoria-clases-sa-key.json"

PILOT_IDS = {
    6919: "A",     # Google Doc directo
    2774: "B",     # Drive file directo (text/plain, real working B)
    2250: "C",     # Carpeta como transcription (real Case C, not Doc)
    2626: "D1",    # Carpeta donde video == transcription (multi-session)
    4216: "D2",    # File único donde video == transcription (mp4 → skip)
    2944: "D3",    # Doc donde video == transcription
    2054: "F",     # spreadsheet
    2341: "F",     # redirect google.com/url
    2413: "F",     # /videos/d/ u otro
}


def main() -> int:
    drive = drive_reader.make_drive_client(SA_KEY)

    print(f"Reading Excel: {EXCEL_PATH}")
    all_rows = list(excel_io.read_rows(EXCEL_PATH))
    print(f"  total rows: {len(all_rows)}\n")

    # ── 1) Distribution check (no Drive calls, just classify) ────────────
    print("=" * 70)
    print("STEP 1 — Empirical case distribution over all rows")
    print("=" * 70)
    counts: Counter[str] = Counter()
    for row in all_rows:
        utilities = excel_io.parse_utilities(row)
        r = drive_reader.classify(utilities, drive=None)
        counts[r.case] += 1
    print(f"  {'case':6}  {'count':>6}  {'%':>7}")
    for case, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        pct = n / len(all_rows) * 100
        print(f"  {case:6}  {n:>6}  {pct:>6.2f}%")

    # ── 2) Drive smoke for the pilot rows ────────────────────────────────
    print()
    print("=" * 70)
    print("STEP 2 — Drive smoke on pilot ids")
    print("=" * 70)
    by_id = {int(r["id"]): r for r in all_rows}

    for excel_id, expected in PILOT_IDS.items():
        row = by_id.get(excel_id)
        if not row:
            print(f"  [SKIP] id={excel_id} not in Excel")
            continue
        utilities = excel_io.parse_utilities(row)
        r = drive_reader.classify(utilities, drive=drive)
        match = "[OK]" if r.case == expected else "[MISMATCH]"
        print(f"\n  id={excel_id}  expected={expected:3}  got={r.case:3}  {match}")
        print(f"    transcription = {r.transcription_url[:90]}")
        if r.case in ("E", "F"):
            print(f"    sub_variant={r.sub_variant}  diagnostic={r.diagnostic}")
            continue
        if r.file_id is None:
            print(f"    no file_id  diagnostic={r.diagnostic}")
            continue
        if r.case in ("B", "D2") and r.mime is None:
            mime = drive_reader.probe_mime(drive, r.file_id)
            print(f"    probed mime: {mime}")
            if mime == drive_reader.MIME_MP4:
                print(f"    [SKIP] would be skipped_no_transcript (mp4 only)")
                continue
        else:
            mime = r.mime
        # Try to download
        try:
            t0 = time.monotonic()
            bytes_, ct, ext = drive_reader.download_transcript(drive, r.file_id, mime)
            dt = time.monotonic() - t0
            print(f"    downloaded {len(bytes_)} bytes as .{ext} in {dt:.2f}s  (src mime={mime})")
        except Exception as e:
            print(f"    [DOWNLOAD FAILED] {type(e).__name__}: {str(e)[:200]}")

    # ── 3) Sample 5 A-case rows (97.75% of total — sanity check) ─────────
    print()
    print("=" * 70)
    print("STEP 3 — Sample 5 random A-case rows")
    print("=" * 70)
    a_rows = [r for r in all_rows if drive_reader.classify(excel_io.parse_utilities(r)).case == "A"]
    import random
    random.seed(42)
    sample = random.sample(a_rows, min(5, len(a_rows)))
    for row in sample:
        utilities = excel_io.parse_utilities(row)
        r = drive_reader.classify(utilities, drive=drive)
        print(f"\n  id={row['id']}  transcription={r.transcription_url[:90]}")
        try:
            t0 = time.monotonic()
            bytes_, ct, ext = drive_reader.download_transcript(drive, r.file_id, r.mime)
            print(f"    downloaded {len(bytes_)} bytes as .{ext} in {time.monotonic()-t0:.2f}s")
        except Exception as e:
            print(f"    [FAILED] {type(e).__name__}: {str(e)[:200]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
