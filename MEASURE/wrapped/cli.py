"""Command line. Every pipeline stage is its own command, idempotent, and runnable on its own."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from wrapped import config
from wrapped.config import Settings, log

logger = logging.getLogger("wrapped.cli")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _timed(settings: Settings, stage: str, fn: Callable[[], object]) -> object:
    """Run a stage, log its duration, and append it to the run report the benchmarks are read from."""
    started = time.perf_counter()
    result = fn()
    seconds = round(time.perf_counter() - started, 2)
    log(logger, "stage finished", stage=stage, seconds=seconds)
    report = settings.data_dir / "run_report.jsonl"
    with report.open("a") as fh:
        fh.write(json.dumps({"stage": stage, "seconds": seconds, "result": result}, default=str) + "\n")
    return result


def transform(settings: Settings, full_refresh: bool = False) -> dict[str, object]:
    """dbt build: models and tests together, so a model whose tests fail stops everything downstream."""
    dbt_dir = REPO_ROOT / "dbt"
    env = os.environ | {
        "WRAPPED_DATA_DIR": settings.data_dir.as_posix(),
        "WRAPPED_WAREHOUSE_PATH": settings.warehouse_path.as_posix(),
        "WRAPPED_YEAR": str(settings.year),
        "WRAPPED_DUCKDB_MEMORY": settings.duckdb_memory,
        "WRAPPED_DUCKDB_THREADS": str(settings.duckdb_threads),
        "WRAPPED_K_ANONYMITY": str(settings.k_anonymity),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    dbt = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    cmd = [str(dbt), "build", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    # Run artefacts live with the data, not in the source tree (which may be read-only in a container).
    cmd += ["--target-path", str(settings.data_dir / "dbt-target"), "--log-path", str(settings.data_dir / "dbt-logs")]
    if full_refresh:
        cmd.append("--full-refresh")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)  # noqa: S603 - fixed argv, no shell
    tail = "\n".join(proc.stdout.strip().splitlines()[-25:])
    if proc.returncode != 0:
        raise SystemExit(f"dbt build failed:\n{tail}\n{proc.stderr[-2000:]}")
    summary = next((line for line in reversed(proc.stdout.splitlines()) if "PASS=" in line), "")
    return {"summary": summary.split("Done. ")[-1].strip()}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wrapped", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="write a synthetic year of GH Archive-shaped files into the raw directory")
    p.add_argument("--users", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=2025)

    p = sub.add_parser("fetch", help="download real GH Archive hours, e.g. 2025-03-12-15")
    p.add_argument("hours", nargs="+")

    sub.add_parser("ingest", help="validate new raw files into bronze Parquet and the quarantine")

    p = sub.add_parser("transform", help="dbt build: staging, intermediate, marts, and their tests")
    p.add_argument("--full-refresh", action="store_true")

    sub.add_parser("build", help="generate one payload per user from the marts")
    sub.add_parser("audit", help="check every payload against the warehouse; exits 1 on any violation")
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    sub.add_parser("publish", help="load the built run into Postgres and make it the one served")
    sub.add_parser("rollback", help="serve the previous run again")

    p = sub.add_parser("run-all", help="ingest, transform, build, audit, publish: the whole batch, in order")
    p.add_argument("--no-publish", action="store_true")

    p = sub.add_parser("links", help="print personal links for a few users of each tier (what a product would email)")
    p.add_argument("--per-tier", type=int, default=2)
    p.add_argument("--user-id", type=int, help="print the link for one specific user instead")

    p = sub.add_parser("serve", help="run the API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("openapi", help="write the OpenAPI document generated from the code")
    p.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    settings = config.load()
    config.setup_logging(settings.log_level)
    config.correlation_id.set(f"run-{uuid.uuid4().hex[:12]}")
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "generate":
        from wrapped.generate import generate

        _timed(
            settings,
            "generate",
            lambda: generate(
                settings.raw_dir,
                args.users,
                settings.year,
                args.seed,
                settings.data_dir / "tmp",
                settings.duckdb_memory,
            ),
        )
    elif args.command == "fetch":
        from wrapped.ingest import fetch

        _timed(settings, "fetch", lambda: [p.name for p in fetch(settings, args.hours)])
    elif args.command == "ingest":
        from wrapped.ingest import ingest

        _timed(settings, "ingest", lambda: ingest(settings))
    elif args.command == "transform":
        print(_timed(settings, "transform", lambda: transform(settings, args.full_refresh)))
    elif args.command in ("build", "audit", "publish", "run-all"):
        run_batch(settings, args.command, publish_after=not getattr(args, "no_publish", False))
    elif args.command == "migrate":
        os.environ["WRAPPED_MIGRATE_DATABASE_URL"] = settings.migrate_database_url
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head"],
            check=True,
            cwd=REPO_ROOT,
        )
    elif args.command == "rollback":
        from wrapped.publish import rollback

        print(json.dumps(_timed(settings, "rollback", lambda: rollback(settings)), indent=2))
    elif args.command == "links":
        print_links(settings, args.per_tier, args.user_id)
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("wrapped.api:create_app", factory=True, host=args.host, port=args.port, log_config=None)
    elif args.command == "openapi":
        from wrapped.api import create_app

        args.path.write_text(json.dumps(create_app(settings).openapi(), indent=2) + "\n")


def run_batch(settings: Settings, command: str, publish_after: bool = True) -> None:
    """The batch stages after transform. `run-all` is every stage in order; a failed audit stops before publish."""
    from wrapped.build import audit, build
    from wrapped.ingest import ingest
    from wrapped.publish import publish

    everything = command == "run-all"
    if everything:
        _timed(settings, "ingest", lambda: ingest(settings))
        _timed(settings, "transform", lambda: transform(settings))
    if everything or command == "build":
        print(json.dumps(_timed(settings, "build", lambda: build(settings)), indent=2))
    if everything or command == "audit":
        result = _timed(settings, "audit", lambda: audit(settings))
        print(json.dumps(result, indent=2))
        if result["violation_count"]:  # type: ignore[index]
            raise SystemExit("audit failed: refusing to publish a run that says something untrue")
    if (everything and publish_after) or command == "publish":
        print(json.dumps(_timed(settings, "publish", lambda: publish(settings)), indent=2))


def print_links(settings: Settings, per_tier: int, user_id: int | None) -> None:
    from wrapped import auth
    from wrapped.publish import connect

    with connect(settings.api_database_url) as conn:
        if user_id is not None:
            rows = [(user_id, "", "")]
        else:
            rows = conn.execute(
                """SELECT user_id, login, tier FROM (
                       SELECT p.user_id, p.login, p.tier,
                              row_number() OVER (PARTITION BY p.tier ORDER BY p.user_id) AS n
                       FROM active_runs a JOIN wrapped_payloads p ON p.run_id = a.run_id WHERE a.year = %s
                   ) ranked WHERE n <= %s ORDER BY tier, user_id""",
                [settings.year, per_tier],
            ).fetchall()
    for uid, login, tier in rows:
        token = auth.mint(settings.token_secret, uid, settings.year)
        print(f"{tier:<8} {login:<28} {settings.public_base_url}/#t={token}")


if __name__ == "__main__":
    main()
