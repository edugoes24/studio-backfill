"""CLI for studio_backfill.

Subcommands:
  find-pilot-rows                scan Excel and sample candidates per case
  pilot --rows <ids> | --all     phase 1 (encolar)
  collect [--once]               phase 2 (recoger PDFs)
  write-excel [--out PATH]       phase 3 (Excel de salida)
  status                         resumen por estado
  failures                       listado de filas en failed_*
  inspect <event_id>             detalle de una fila
  retry --events <ids>           resetear filas puntuales a pending
  reset --confirm                limpiar el state.sqlite
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from . import drive_reader, excel_io
from .config import Settings
from .pipeline import Pipeline
from .state import StateStore

log = logging.getLogger("studio_backfill.cli")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt)


def _open_state(settings: Settings) -> StateStore:
    return StateStore(settings.state_db_path)


def _ensure_source_registered(state: StateStore, settings: Settings) -> int:
    """Idempotent — registers the Excel SHA + count if not yet done.

    Returns the row count.
    """
    rows = list(excel_io.read_rows(settings.excel_path))
    state.register_source(settings.excel_path, len(rows), settings.webhook_url)
    return len(rows)


# ─── find-pilot-rows ──────────────────────────────────────────────────────
def cmd_find_pilot_rows(args, settings: Settings, state: StateStore) -> int:
    """Print case distribution + sample of row_positions per case.

    For C/D1 also peeks into the Drive folders to validate content uniformity.
    """
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict]] = {c: [] for c in ("A", "B", "C", "D1", "D2", "D3", "E", "F")}
    max_per_case = args.sample_size

    drive = drive_reader.make_drive_client(settings.sa_key_path)
    for row in excel_io.read_rows(settings.excel_path):
        r = drive_reader.classify(
            transcript_link=row.get("transcriptLink"),
            video_link=row.get("videoLink"),
            drive=None,  # no folder peek for speed
        )
        counts[r.case] += 1
        if r.case in samples and len(samples[r.case]) < max_per_case:
            samples[r.case].append({
                "row_position": row["_row_position"],
                "url": r.transcript_url,
                "sub_variant": r.sub_variant,
            })

    print("=" * 70)
    print("Counts per case:")
    for case, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {case:5}  {n}")
    print()

    for case in ("A", "B", "C", "D1", "D2", "D3", "F"):
        if not samples[case]:
            continue
        print(f"\n-- Samples for case {case} (max {max_per_case}) --")
        for s in samples[case]:
            print(f"  row_position={s['row_position']}  url={s['url'][:80]}  sub_variant={s['sub_variant']}")
            if case in ("C", "D1"):
                folder_match = drive_reader._RE_FOLDER.match(s["url"])
                if folder_match:
                    folder_id = folder_match.group(1)
                    try:
                        children = drive.files().list(
                            q=f"'{folder_id}' in parents and trashed=false",
                            fields="files(id,name,mimeType)",
                            supportsAllDrives=True,
                            includeItemsFromAllDrives=True,
                            pageSize=20,
                        ).execute().get("files", [])
                        for c in children[:10]:
                            print(f"      - {c.get('name')!r}  ({c.get('mimeType')})")
                    except Exception as e:
                        print(f"      [could not list folder: {e}]")

    # Build a suggested pilot command
    suggestion = []
    for case in ("A", "B", "C", "D1", "D2", "D3", "F", "E"):
        if samples[case]:
            suggestion.append(str(samples[case][0]["row_position"]))
    if suggestion:
        print()
        print("Suggested pilot command:")
        print(f"  python -m studio_backfill.cli pilot --rows {','.join(suggestion)}")
    return 0


# ─── pilot ────────────────────────────────────────────────────────────────
def cmd_pilot(args, settings: Settings, state: StateStore) -> int:
    _ensure_source_registered(state, settings)

    if args.retry_failed:
        rows_for_retry = state.fetch(
            states=[
                "failed_drive_read", "failed_gcs_upload", "failed_webhook",
                "failed_phase1",
            ],
        )
        for r in rows_for_retry:
            state.update(r.event_id, state="pending", last_error=None)
        excel_rows = [r.excel_row for r in rows_for_retry]
    elif args.all:
        excel_rows = list(excel_io.read_rows(settings.excel_path))
    elif args.rows:
        wanted_positions = {int(x.strip()) for x in args.rows.split(",") if x.strip()}
        excel_rows = [
            r for r in excel_io.read_rows(settings.excel_path)
            if int(r["_row_position"]) in wanted_positions
        ]
        seen = {int(r["_row_position"]) for r in excel_rows}
        for missing in wanted_positions - seen:
            log.warning("row_position %d not in Excel — skipping", missing)
    else:
        log.error("specify --rows ROW_POSITIONS, --all, or --retry-failed")
        return 2

    log.info("phase 1: %d rows to process at %.2f RPS", len(excel_rows), settings.webhook_rps)
    pipeline = Pipeline(settings)
    pipeline.submit_all(state, excel_rows)
    state.mark_phase_1_finished()
    log.info("phase 1 done. summary: %s", state.status_summary())
    return 0


# ─── collect ──────────────────────────────────────────────────────────────
def cmd_collect(args, settings: Settings, state: StateStore) -> int:
    pipeline = Pipeline(settings)
    if args.once:
        n = pipeline.collect_once(state)
        log.info("collected %d in this pass. summary: %s", n, state.status_summary())
    else:
        pipeline.collect_until_drained(state)
        log.info("collect done. summary: %s", state.status_summary())
    return 0


# ─── write-excel ──────────────────────────────────────────────────────────
def cmd_write_excel(args, settings: Settings, state: StateStore) -> int:
    src_meta = state.get_source_metadata() or {}
    out_path = args.out or _default_output_path(settings.excel_path)

    rows_with_links: dict[int, dict] = {}

    # Fetch ALL rows (not filtered by state)
    all_rows = state._conn.execute("SELECT * FROM rows").fetchall()
    for r in all_rows:
        position = r["row_position"]
        status = r["state"]
        if status == "completed":
            rows_with_links[position] = {
                "event_id_xai": r["event_id"],
                "pdf_drive_link": r["pdf_drive_link"] or "",
                "backfill_status": "completed",
            }
        elif status.startswith("skipped_"):
            rows_with_links[position] = {
                "event_id_xai": r["event_id"],
                "pdf_drive_link": "",
                "backfill_status": status,
            }
        else:
            rows_with_links[position] = {
                "event_id_xai": r["event_id"],
                "pdf_drive_link": "",
                "backfill_status": f"{status}: {(r['last_error'] or '')[:200]}",
            }

    excel_io.write_output_excel(
        settings.excel_path, out_path, rows_with_links,
        source_sha256=src_meta.get("source_excel_sha256"),
    )
    log.info("wrote %s (%d rows annotated)", out_path, len(rows_with_links))
    return 0


def _default_output_path(src: str) -> str:
    p = Path(src)
    return str(p.with_name(f"{p.stem}_with_reports{p.suffix}"))


# ─── status / failures / inspect ──────────────────────────────────────────
def cmd_status(args, settings: Settings, state: StateStore) -> int:
    summary = state.status_summary()
    total = sum(summary.values())
    print(f"Total rows in state.sqlite: {total}")
    for state_name, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        pct = (count / total * 100) if total else 0
        print(f"  {state_name:40} {count:6} ({pct:5.1f}%)")
    return 0


def cmd_failures(args, settings: Settings, state: StateStore) -> int:
    failures = state.failures_listing()
    print(f"{'event_id':22} {'state':40} {'attempts':>8}  last_error")
    for r in failures:
        print(f"{r.event_id:22} {r.state:40} {r.attempts:>8}  {(r.last_error or '')[:80]}")
    return 0


def cmd_inspect(args, settings: Settings, state: StateStore) -> int:
    row = state.get(args.event_id)
    if not row:
        print(f"event_id {args.event_id} not found")
        return 1
    print(json.dumps({
        "row_position": row.row_position,
        "event_id": row.event_id,
        "drive_case": row.drive_case,
        "transcript_file_id": row.transcript_file_id,
        "transcript_gcs_uri": row.transcript_gcs_uri,
        "webhook_message_id": row.webhook_message_id,
        "webhook_submitted_at": row.webhook_submitted_at,
        "pdf_drive_id": row.pdf_drive_id,
        "pdf_drive_link": row.pdf_drive_link,
        "state": row.state,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "last_attempt_at": row.last_attempt_at,
        "excel_row": row.excel_row,
    }, indent=2, default=str))
    return 0


# ─── retry / reset ────────────────────────────────────────────────────────
def cmd_retry(args, settings: Settings, state: StateStore) -> int:
    ids = [s.strip() for s in args.events.split(",") if s.strip()]
    for event_id in ids:
        state.update(event_id, state="pending", attempts=0, last_error=None)
        log.info("reset %s to pending", event_id)
    return 0


def cmd_reset(args, settings: Settings, state: StateStore) -> int:
    if not args.confirm:
        print("reset requires --confirm")
        return 2
    state.reset()
    log.info("state.sqlite reset")
    return 0


# ─── entry point ──────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="studio_backfill")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_find = sub.add_parser("find-pilot-rows")
    sp_find.add_argument("--sample-size", type=int, default=5)
    sp_find.set_defaults(func=cmd_find_pilot_rows)

    sp_pilot = sub.add_parser("pilot")
    g = sp_pilot.add_mutually_exclusive_group()
    g.add_argument("--rows", type=str, help="comma-separated row positions (1-indexed)")
    g.add_argument("--all", action="store_true")
    g.add_argument("--retry-failed", action="store_true")
    sp_pilot.set_defaults(func=cmd_pilot)

    sp_collect = sub.add_parser("collect")
    sp_collect.add_argument("--once", action="store_true")
    sp_collect.set_defaults(func=cmd_collect)

    sp_write = sub.add_parser("write-excel")
    sp_write.add_argument("--out", type=str, help="output path")
    sp_write.set_defaults(func=cmd_write_excel)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("failures").set_defaults(func=cmd_failures)

    sp_inspect = sub.add_parser("inspect")
    sp_inspect.add_argument("event_id")
    sp_inspect.set_defaults(func=cmd_inspect)

    sp_retry = sub.add_parser("retry")
    sp_retry.add_argument("--events", required=True, help="comma-separated event_ids")
    sp_retry.set_defaults(func=cmd_retry)

    sp_reset = sub.add_parser("reset")
    sp_reset.add_argument("--confirm", action="store_true")
    sp_reset.set_defaults(func=cmd_reset)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    settings = Settings.load()
    state = _open_state(settings)
    try:
        return args.func(args, settings, state)
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
