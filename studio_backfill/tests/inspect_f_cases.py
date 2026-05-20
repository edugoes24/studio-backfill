"""Lists all F-case rows with their sub-variants to validate the resolver
against the plan's empirical numbers (which expected F=7).
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from studio_backfill import drive_reader, excel_io

EXCEL = r"C:\Users\Eduardo Lopez\Downloads\studio_results_20260518_1308.xlsx"

rows = list(excel_io.read_rows(EXCEL))
sub_counts: Counter[str] = Counter()
print(f"{'id':>6}  {'sub':<12}  url[:100]")
print("-" * 130)
for row in rows:
    u = excel_io.parse_utilities(row)
    r = drive_reader.classify(u)
    if r.case != "F":
        continue
    sub_counts[r.sub_variant or "F_other"] += 1
    print(f"{row['id']:>6}  {r.sub_variant or 'F_other':<12}  {r.transcription_url[:100]}")

print()
print("Sub-variant counts:")
for sv, n in sub_counts.most_common():
    print(f"  {sv:<12} {n}")
