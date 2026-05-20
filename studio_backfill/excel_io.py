"""Read the studio_results Excel and write the augmented output Excel.

Post-migration: flat 12-column format (studio_results_final.xlsx).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import openpyxl


# The columns we care about (case-sensitive header names in the source Excel).
REQUIRED_COLUMNS = [
    "infrastructureCode",
    "school",
    "department",
    "district",
    "teacher",
    "coach",
    "subject",
    "grade",
    "section",
    "shift",
    "videoLink",
    "transcriptLink",
]


def read_rows(excel_path: str) -> Iterator[dict]:
    """Yield each data row as a dict keyed by header, with synthetic `_row_position`.

    `_row_position` is 1-indexed and excludes the header row. The row position
    becomes the de-facto primary key since the new Excel has no `id` column.
    """
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    rows_iter = ws.iter_rows(values_only=True)
    header = list(next(rows_iter))
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise RuntimeError(f"Excel missing required columns: {missing}")

    position = 0
    for raw in rows_iter:
        if raw is None or all(v is None for v in raw):
            continue
        position += 1
        row = dict(zip(header, raw))
        row["_row_position"] = position
        yield row


def write_output_excel(
    source_excel_path: str,
    output_path: str,
    rows_with_links: dict[int, dict],
    source_sha256: str | None = None,
) -> None:
    """Generate output Excel: source columns + event_id_xai + pdf_drive_link + backfill_status.

    `rows_with_links` maps row_position (1-indexed) → {event_id_xai, pdf_drive_link, backfill_status}.
    """
    src = Path(source_excel_path)
    if not src.exists():
        raise FileNotFoundError(source_excel_path)

    wb = openpyxl.load_workbook(source_excel_path)
    ws = wb[wb.sheetnames[0]]

    # Add the 3 new columns at the right.
    header = [cell.value for cell in ws[1]]
    new_cols = ["event_id_xai", "pdf_drive_link", "backfill_status"]
    start_col = len(header) + 1
    for i, col_name in enumerate(new_cols):
        ws.cell(row=1, column=start_col + i, value=col_name)

    # row_position is 1-indexed and excludes the header. The Excel row index
    # (1-based, header on row 1) is row_position + 1.
    for position in range(1, ws.max_row):
        sheet_row = position + 1
        info = rows_with_links.get(position, {})
        ws.cell(row=sheet_row, column=start_col + 0, value=info.get("event_id_xai", ""))
        ws.cell(row=sheet_row, column=start_col + 1, value=info.get("pdf_drive_link", ""))
        ws.cell(row=sheet_row, column=start_col + 2, value=info.get("backfill_status", ""))

    # SHA-256 trazability cell: write to a separate "_backfill_meta" sheet.
    if source_sha256:
        meta_sheet = wb.create_sheet("_backfill_meta")
        meta_sheet["A1"] = "source_excel_path"
        meta_sheet["B1"] = source_excel_path
        meta_sheet["A2"] = "source_excel_sha256"
        meta_sheet["B2"] = source_sha256
        meta_sheet["A3"] = "generated_by"
        meta_sheet["B3"] = "studio_backfill"

    wb.save(output_path)
