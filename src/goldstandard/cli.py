"""Command line entry point: `goldstandard <command>`. Every command calls library code that
Dagster also calls, so nothing here is logic that only runs on one laptop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from goldstandard.config import Settings, settings
from goldstandard.obs import configure_logging
from goldstandard.raw import RawStore


def _owner_dsn() -> str:
    dsn = os.environ.get("GS_OWNER_DATABASE_URL")
    if not dsn:
        sys.exit("GS_OWNER_DATABASE_URL is required for migrations (schema owner credentials)")
    return dsn


def cmd_migrate(args: argparse.Namespace) -> None:
    from goldstandard import migrate

    if args.direction == "up":
        print(json.dumps({"applied": migrate.upgrade(_owner_dsn(), args.target)}))
    else:
        print(json.dumps({"reverted": migrate.downgrade(_owner_dsn(), args.target or 0)}))


def cmd_profile(args: argparse.Namespace) -> None:
    from pathlib import Path

    from goldstandard.profiling import profile

    cfg = settings()
    print(json.dumps(profile(cfg, Path("docs/DATA_PROFILE.md"), cfg.reference_dir / "synthetic_calibration.json")))


def cmd_synth(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from goldstandard.sources import synthetic

    cfg = settings()
    until = datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else datetime.now(UTC)
    sc = synthetic.SynthConfig(
        start=cfg.synth_start,
        servers=cfg.synth_servers,
        seed=cfg.synth_seed,
        snapshots_per_day=cfg.synth_snapshots_per_day,
    )
    stats = synthetic.generate(
        sc, RawStore(cfg.raw_dir), cfg.reference_dir, until, truth_dir=cfg.lake_dir / "synthetic_truth"
    )
    print(json.dumps(stats))


def cmd_ingest(args: argparse.Namespace) -> None:
    from goldstandard.sources import eve

    cfg = settings()
    store = RawStore(cfg.raw_dir)
    fns: dict[str, Callable[[Settings, RawStore], eve.IngestReport]] = {
        "history": eve.ingest_history,
        "orders": eve.ingest_order_snapshot,
        "meta": eve.ingest_type_metadata,
        "patches": eve.ingest_patch_notes,
    }
    reports = [fns[k](cfg, store).as_dict() for k in (fns if args.what == "all" else [args.what])]
    print(json.dumps(reports))
    if any(r["failed"] == r["requested"] for r in reports):
        sys.exit(2)  # nothing at all came back: fail loudly; partial failure is reported, not fatal


def cmd_run(args: argparse.Namespace) -> None:
    from datetime import date, timedelta

    from goldstandard import db
    from goldstandard.obs import correlation_id, new_correlation_id
    from goldstandard.pipeline import WORLDS, Pipeline

    correlation_id.set(new_correlation_id())
    cfg = settings()
    pipe = Pipeline(cfg)
    days = None
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else start
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    with db.connect(cfg) as conn:
        for world in WORLDS if args.world == "all" else [args.world]:
            print(json.dumps(pipe.run(conn, world, days).as_dict()))


def cmd_seed(args: argparse.Namespace) -> None:
    """Bring an empty deployment to a demo-ready state: real EVE data (live, or the committed snapshot
    when ESI is unreachable), the synthetic feed up to now, then the full pipeline for both worlds."""
    import tarfile
    from datetime import UTC, datetime

    from goldstandard.sources import eve, synthetic

    cfg = settings()
    store = RawStore(cfg.raw_dir)
    if not store.days("eve", "market_history"):
        fixture = cfg.reference_dir / "fixtures" / "eve_raw_2026-10-02.tar"
        with tarfile.open(fixture) as tar:
            tar.extractall(cfg.raw_dir, filter="data")  # 'data' filter: no absolute paths, links or traversal
        print(json.dumps({"seed": "restored real EVE snapshot", "fixture": fixture.name}))
    if not args.offline:
        for fn in (eve.ingest_type_metadata, eve.ingest_history, eve.ingest_patch_notes, eve.ingest_order_snapshot):
            report = fn(cfg, store)
            print(json.dumps({"seed": "live ESI top-up", **report.as_dict(), "failures": report.failures[:3]}))
    sc = synthetic.SynthConfig(
        start=cfg.synth_start,
        servers=cfg.synth_servers,
        seed=cfg.synth_seed,
        snapshots_per_day=cfg.synth_snapshots_per_day,
    )
    until = datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else datetime.now(UTC)
    print(
        json.dumps(
            {
                "seed": "synthetic feed",
                **synthetic.generate(sc, store, cfg.reference_dir, until, truth_dir=cfg.lake_dir / "synthetic_truth"),
            }
        )
    )
    cmd_run(argparse.Namespace(world="all", start=None, end=None))


def cmd_create_api_key(args: argparse.Namespace) -> None:
    """Create (or rotate) an API key as the schema owner. The token comes from the environment
    variable named by --token-env, is stored only as a SHA-256 hash and is never printed."""
    import hashlib

    import psycopg

    token = os.environ.get(args.token_env, "")
    if len(token) < 16:
        sys.exit(f"{args.token_env} must hold a token of at least 16 characters")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with psycopg.connect(_owner_dsn()) as conn:
        conn.execute(
            """INSERT INTO api_key (key_id, key_hash, scopes) VALUES (%s, %s, %s)
                        ON CONFLICT (key_id) DO UPDATE SET key_hash = EXCLUDED.key_hash, scopes = EXCLUDED.scopes,
                        revoked_at = NULL""",
            (args.key_id, digest, args.scopes.split(",")),
        )
    print(json.dumps({"api_key": args.key_id, "scopes": args.scopes.split(","), "stored": "sha256 only"}))


def cmd_openapi(args: argparse.Namespace) -> None:
    """Write the OpenAPI spec generated from the FastAPI code (docs and frontend types derive from it)."""
    from goldstandard.api.app import app
    from goldstandard.ops import render_api_markdown

    spec = app.openapi()
    outputs = {args.out: json.dumps(spec, indent=2, sort_keys=True) + "\n", args.md: render_api_markdown(spec)}
    for path, text in outputs.items():
        if args.check:
            current = Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""
            if current != text:
                sys.exit(f"{path} is out of date: run `goldstandard openapi`")
        else:
            Path(path).write_text(text, encoding="utf-8", newline="\n")


def cmd_explain(args: argparse.Namespace) -> None:
    from goldstandard.ops import explain

    dsn = os.environ.get("GS_OWNER_DATABASE_URL") or settings().database_url.get_secret_value()
    print(json.dumps({"explained": explain(dsn, Path(args.out), args.world)}))


def cmd_bench(args: argparse.Namespace) -> None:
    from goldstandard.ops import bench

    result = bench(settings(), args.days, args.servers, args.api)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2))


def cmd_accuracy(args: argparse.Namespace) -> None:
    from goldstandard.ops import accuracy

    result = accuracy(settings())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str) + chr(10), encoding="utf-8", newline=chr(10))
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    cfg = settings()
    configure_logging(cfg.log_level, cfg.log_json)
    parser = argparse.ArgumentParser(prog="goldstandard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="apply or roll back schema migrations")
    m.add_argument("direction", choices=["up", "down"])
    m.add_argument("--target", type=int, default=None)
    m.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("profile", help="profile real raw data; write docs/DATA_PROFILE.md + calibration")
    p.set_defaults(fn=cmd_profile)

    sy = sub.add_parser("synth", help="advance the synthetic world feed up to --until (default: now)")
    sy.add_argument("--until", default=None, help="ISO datetime (UTC)")
    sy.set_defaults(fn=cmd_synth)

    ing = sub.add_parser("ingest", help="fetch real EVE data into the raw store")
    ing.add_argument("what", choices=["all", "history", "orders", "meta", "patches"])
    ing.set_defaults(fn=cmd_ingest)

    r = sub.add_parser("run", help="process changed partitions (or --start/--end) end to end")
    r.add_argument("--world", choices=["all", "eve", "synthetic"], default="all")
    r.add_argument("--start", help="force-process from this day (YYYY-MM-DD)")
    r.add_argument("--end", help="force-process through this day (default: --start)")
    r.set_defaults(fn=cmd_run)

    sd = sub.add_parser("seed", help="load real + synthetic data and run the pipeline (idempotent)")
    sd.add_argument("--offline", action="store_true", help="do not call ESI; use the committed snapshot only")
    sd.add_argument("--until", default=None, help="advance the synthetic feed to this UTC time (default: now)")
    sd.set_defaults(fn=cmd_seed)

    k = sub.add_parser("create-api-key", help="create/rotate an API key (schema owner credentials)")
    k.add_argument("--key-id", default="admin")
    k.add_argument("--scopes", default="patches:write")
    k.add_argument("--token-env", default="GS_BOOTSTRAP_ADMIN_KEY")
    k.set_defaults(fn=cmd_create_api_key)

    o = sub.add_parser("openapi", help="export the OpenAPI spec generated from code")
    o.add_argument("--out", default="docs/api/openapi.json")
    o.add_argument("--md", default="docs/API.md")
    o.add_argument("--check", action="store_true", help="fail if the committed spec differs from the code")
    o.set_defaults(fn=cmd_openapi)

    e = sub.add_parser("explain", help="capture EXPLAIN ANALYZE of the hot queries into docs/explain/")
    e.add_argument("--out", default="docs/explain")
    e.add_argument("--world", default="synthetic")
    e.set_defaults(fn=cmd_explain)

    b = sub.add_parser("bench", help="measure stage throughput (and API latency with --api)")
    b.add_argument("--days", type=int, default=60)
    b.add_argument("--servers", type=int, default=4)
    b.add_argument("--api", default=None, help="base URL of a running API, e.g. http://localhost:8000")
    b.add_argument("--out", default="docs/perf/bench.json")
    b.set_defaults(fn=cmd_bench)

    acc = sub.add_parser("accuracy", help="score the synthetic world against the generator's ground truth")
    acc.add_argument("--out", default="docs/perf/accuracy.json")
    acc.set_defaults(fn=cmd_accuracy)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
