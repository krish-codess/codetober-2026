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
    elif args.command == "build":
        from wrapped.build import build

        print(json.dumps(_timed(settings, "build", lambda: build(settings)), indent=2))
    elif args.command == "audit":
        from wrapped.build import audit

        result = _timed(settings, "audit", lambda: audit(settings))
        print(json.dumps(result, indent=2))
        if result["violation_count"]:  # type: ignore[index]
            raise SystemExit(1)


if __name__ == "__main__":
    main()
