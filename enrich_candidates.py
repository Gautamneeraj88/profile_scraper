#!/usr/bin/env python3
"""
LinkedIn Enricher -- command line.
=================================

The same engine the desktop app uses, for scripted or headless runs.

    python3 enrich_candidates.py                       # use the saved settings
    python3 enrich_candidates.py "https://docs.google.com/spreadsheets/d/.../edit"
    python3 enrich_candidates.py candidates.xlsx --limit 5 --show-browser
    python3 enrich_candidates.py --fields full_name,current_company,location
    python3 enrich_candidates.py --list-fields
    python3 enrich_candidates.py --self-test

Most people should use the app instead:  python3 li_app.py
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

import li_engine as E
import li_fields as LF
from li_fields import FIELDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fill in missing candidate details from LinkedIn.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("=\n")[-1])
    parser.add_argument("input", nargs="?",
                        help="Google Sheet link, .xlsx or .csv file")
    parser.add_argument("--fields", help="comma-separated field keys to collect "
                                         "(see --list-fields)")
    parser.add_argument("--out", help="folder to write the results into")
    parser.add_argument("--limit", type=int, help="only do the first N candidates")
    parser.add_argument("--show-browser", action="store_true",
                        help="watch the browser while it works")
    parser.add_argument("--no-search", action="store_true",
                        help="do not look for missing profiles by name")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore profiles collected earlier")
    parser.add_argument("--min-delay", type=float)
    parser.add_argument("--max-delay", type=float)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the plan without opening LinkedIn")
    parser.add_argument("--list-fields", action="store_true",
                        help="list everything that can be collected")
    parser.add_argument("--self-test", action="store_true",
                        help="check the engine's own logic, offline")
    parser.add_argument("--verbose", action="store_true")
    return parser


def list_fields() -> int:
    for group, specs in LF.fields_by_group().items():
        if not specs:
            continue
        print(f"\n{group}")
        print("-" * len(group))
        for spec in specs:
            extra = sorted(v for v in spec.needs if v != LF.VISIT_PROFILE)
            cost = ("free" if not extra
                    else "+" + ",".join(LF.VISIT_LABELS.get(v, v) for v in extra))
            flag = "  [unverified]" if spec.experimental else ""
            print(f"  {spec.key:<26} {spec.label:<30} {cost}{flag}")
    print("\nUse them with --fields key1,key2,...")
    return 0


def config_from_args(args) -> E.AppConfig:
    try:
        config = E.AppConfig.load()
    except E.EngineError as exc:
        print(f"Note: {exc}\nUsing the standard settings instead.\n")
        config = E.AppConfig()

    if args.input:
        target = args.input.strip().strip('"').strip("'").replace("\\ ", " ")
        if target.startswith("http"):
            config.input_mode, config.sheet_url = "sheet", target
        else:
            config.input_mode, config.file_path = "file", str(Path(target).expanduser())
    if args.fields:
        keys = [k.strip() for k in args.fields.split(",") if k.strip()]
        unknown = [k for k in keys if k not in FIELDS]
        if unknown:
            raise SystemExit(f"Unknown field(s): {', '.join(unknown)}\n"
                             f"Run --list-fields to see what is available.")
        config.columns = [E.ColumnMap(FIELDS[k].label, k) for k in keys]
    if args.out:
        config.output_folder = str(Path(args.out).expanduser())
    if args.limit is not None:
        config.limit = args.limit
    if args.show_browser:
        config.headless = False
    if args.no_search:
        config.name_search = False
    if args.fresh:
        config.use_cache = False
    if args.min_delay is not None:
        config.min_delay = args.min_delay
    if args.max_delay is not None:
        config.max_delay = args.max_delay
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_fields:
        return list_fields()
    if args.self_test:
        import tests_engine
        return tests_engine.main()

    log_path = E.setup_logging(args.verbose)
    config = config_from_args(args)

    print("LinkedIn Enricher")
    print("=" * 17)

    if not args.input and config.input_mode == "sheet" and not config.sheet_url:
        try:
            target = input("\nPaste your Google Sheet link, or drag your Excel/CSV file "
                           "here,\nthen press Enter:\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if not target:
            print("Nothing to do.")
            return 1
        args.input = target
        config = config_from_args(args)

    control = E.Control()
    signal.signal(signal.SIGINT, lambda *_: control.stop())

    creds = None
    if config.username:
        store = E.SecretStore()
        password = store.load(config.username)
        if password:
            creds = E.Credentials(config.username, password)

    runner = E.EnrichRunner(config, control, creds, log_path)
    try:
        plan = runner.load()
    except E.EngineError as exc:
        print(f"\n{exc}")
        return 1

    print(f"\nReading your list ... {plan.total} candidates.")
    print(f"  {plan.with_url} with a LinkedIn link, {plan.need_search} to look up by "
          f"name, {plan.already_complete} already complete.")
    print(f"  Collecting {len(config.enabled_fields)} field(s), which needs "
          f"{len(plan.needs)} LinkedIn page(s) per candidate "
          f"({LF.describe_visits(plan.needs)}).")
    if plan.unreadable_links:
        print(f"  {len(plan.unreadable_links)} LinkedIn value(s) could not be read.")
    for warning in config.warnings():
        print(f"  Note: {warning}")

    if args.dry_run:
        print("\nDry run -- nothing will be collected.\n")
        name_header = runner._input_header_for("full_name")
        for index, row in enumerate(runner.rows, start=1):
            name = str(row.get(name_header, "") or "").strip() or "(no name)"
            slug = row.get("_slug")
            action = ("skip" if not row["_missing"]
                      else "scrape" if slug else "search by name")
            print(f"  {index:>3}  {name[:28]:<30} {action:<15} "
                  f"{('/in/' + slug) if slug else ''}")
        return 0

    print("\nStarting. Press Ctrl-C to stop; everything collected is kept.\n")
    try:
        runner.open_browser()
        if runner.session_state() != "live":
            print("  You need to sign in to LinkedIn.")
            print("  A browser window is opening -- please sign in there.")
            print("  (Security checks and codes are fine, take your time.)\n")
            if not runner.login(lambda ev: print(f"  {ev.message}")):
                print("\n  The sign-in did not complete. Please try again.")
                return 1

        total = plan.total
        for event in runner.iter_rows():
            if isinstance(event, E.RowResult):
                marks = {"ok": "ok", "nothing_new": "nothing new", "skipped": "skipped",
                         "not_found": "not found", "error": "FAILED",
                         "blocked": "BLOCKED"}
                cached = " (already had it)" if event.from_cache else ""
                print(f"[{event.index + 1:>3}/{total}] {event.name[:26]:<28} "
                      f"{marks.get(event.status, event.status):<12} "
                      f"{len(event.filled)} filled{cached}")
                if event.notes:
                    print(f"          {event.notes[:96]}")
    except E.Cancelled:
        print("\nStopping at your request.")
    except E.EngineError as exc:
        print(f"\n{exc}")
        return 1
    finally:
        runner.close()

    summary = runner.finish()
    counters = summary.counters
    print()
    if summary.reason.startswith("blocked"):
        print("LinkedIn paused this session. Your progress is saved -- wait a while, "
              "then run it again.")
    elif summary.reason == "stopped":
        print("Stopped early. Re-run to carry on where it left off.")
    print(f"Done. {counters.get('filled', 0)} filled in, "
          f"{counters.get('skipped', 0)} skipped, "
          f"{counters.get('not_found', 0)} not found, "
          f"{counters.get('errors', 0)} problems, "
          f"{counters.get('pageviews', 0)} LinkedIn pages opened.")
    for path in summary.files:
        print(f"  Saved: {path}")
    review = sum(1 for r in runner.results.values() if r.needs_review)
    if review:
        print(f"  {review} row(s) need checking -- see the 'Needs Review' column.")
    print(f"  Log: {log_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
