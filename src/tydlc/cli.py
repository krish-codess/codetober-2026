"""tydlc command line. Every pipeline stage is a subcommand that can run on its own."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from tydlc.engine import ENGINES, make_engine
from tydlc.ingest import ingest, profile
from tydlc.obs import correlation_id, setup_logging
from tydlc.runner import candidates, run_suite
from tydlc.subjects import get_subject

log = logging.getLogger("tydlc.cli")
_MARK = {"held": "ok  ", "falsified": "FAIL", "vacuous": "n/a "}


def _summary(report: dict[str, Any]) -> str:
    lines = [f"{report['subject']} on {report['engine']}: seed {report['seed']}, "
             f"{report['examples']} datasets, {report['rows_generated']} rows, "
             f"{report['timings_ms']['total'] / 1000:.1f}s", ""]
    for p in report["properties"]:
        if p["source"] == "discovered" and p["status"] != "falsified":
            continue
        note = ""
        if f := p["failure"]:
            freq = f"{p['failure_frequency']:.0%}"
            note = f"  fails {freq} of examples; minimal case {f['minimal_rows']} rows"
            note += "  [known bug]" if p["known_bug"] else ""
        lines.append(f"  {_MARK[p['status']]} {p['source']:<10} {p['name']}{note}")
    d = report["discovery"]
    lines += ["", f"discovery: {d['held']}/{d['candidates']} seed-inferred candidates survived "
                  f"(hit rate {d['hit_rate']})" if d["candidates"] else "discovery: off"]
    for key, label in (("unexpected_failures", "UNEXPECTED FAILURE"),
                       ("stale_known_bugs", "KNOWN BUG NO LONGER FAILS (update known_bugs)")):
        lines += [f"{label}: {name}" for name in report[key]]
    lines.append("gate: " + ("pass" if report["ok"] else "FAIL"))
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> int:
    subject = get_subject(args.subject)
    dsn = os.environ.get("DATABASE_URL")
    key = args.run_key or uuid.uuid4().hex
    correlation_id.set(key)
    conn = run = None
    if args.store:
        from tydlc import store

        # The property gate must not depend on the results database: if it is down the
        # run still happens and still gates, and only the history entry is lost.
        try:
            conn = store.connect(dsn or "")
            run, created = store.create_run(conn, key, subject.name, args.engine, args.seed,
                                            args.max_examples, os.environ.get("GIT_SHA"))
            if not created:
                print(f"run {key} is already recorded (id {run['id']}, {run['status']})")
                return 0
        except Exception as exc:
            log.warning("results database unavailable; run will not be recorded",
                        extra={"error": str(exc).strip()})
            conn = None
    try:
        report = run_suite(subject, args.engine, seed=args.seed, max_examples=args.max_examples,
                           data_dir=args.data_dir, run_key=key, dsn=dsn, workers=args.workers,
                           discover_invariants=not args.no_discover)
    except Exception as exc:
        if conn and run:
            store.fail_run(conn, run["id"], f"{type(exc).__name__}: {exc}")
        raise
    if conn and run:
        store.finish_run(conn, run["id"], report)
    print(_summary(report))
    print(f"report: {args.data_dir / 'runs' / key / 'report.json'}")
    return 0 if report["ok"] else 1


def _discover(args: argparse.Namespace) -> int:
    subject = get_subject(args.subject)
    engine = make_engine(args.engine, subject, os.environ.get("DATABASE_URL"))
    for p in candidates(subject, engine, args.data_dir):
        print(f"{p.name:<60} {p.description}")
    return 0


def _ingest(args: argparse.Namespace) -> int:
    subject = get_subject(args.subject)
    counts = ingest(subject.schema, args.src or subject.seed_dir, args.data_dir / subject.name)
    print(json.dumps(counts, indent=2))
    return 0


def _profile(args: argparse.Namespace) -> int:
    subject = get_subject(args.subject)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(profile(subject.schema, args.src or subject.seed_dir))
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from tydlc import store

    conn = store.connect(os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"])
    print(f"schema at version {store.migrate(conn, args.to)}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("tydlc.api:create_app", factory=True, host=args.host, port=args.port,
                log_config=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tydlc", description=__doc__)
    parser.add_argument("--subject", default="jaffle")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("TYDLC_DATA_DIR", "data")))
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("run", help="sweep all properties, shrink failures, apply the gate")
    p.add_argument("--engine", choices=ENGINES, default="duckdb")
    p.add_argument("--seed", type=int, default=28)
    p.add_argument("--max-examples", type=int, default=200)
    p.add_argument("--workers", type=int, default=None, help="shrink processes (default: CPUs)")
    p.add_argument("--run-key", help="idempotency key; reusing one never records twice")
    p.add_argument("--store", action="store_true", help="record the run in PostgreSQL")
    p.add_argument("--no-discover", action="store_true", help="declared properties only")
    p.set_defaults(fn=_run)

    p = sub.add_parser("discover", help="list invariant candidates inferred from the seed data")
    p.add_argument("--engine", choices=ENGINES, default="duckdb")
    p.set_defaults(fn=_discover)

    p = sub.add_parser("ingest", help="validate raw CSVs into staged and quarantine Parquet")
    p.add_argument("--src", type=Path)
    p.set_defaults(fn=_ingest)

    p = sub.add_parser("profile", help="profile raw CSVs as Markdown")
    p.add_argument("--src", type=Path)
    p.set_defaults(fn=_profile)

    p = sub.add_parser("migrate", help="apply migrations (or roll back with --to)")
    p.add_argument("--to", type=int, help="target version; 0 rolls everything back")
    p.set_defaults(fn=_migrate)

    p = sub.add_parser("serve", help="run the API and viewer")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=_serve)

    args = parser.parse_args(argv)
    setup_logging()
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
