"""`bakeoff <stage>`: every stage runs on its own, and running it twice changes nothing."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import bench as bench_mod
from . import data
from .config import ConfigError, Settings, load_dotenv, log, new_correlation_id, setup_logging, timed
from .report import report, write_docs

STAGES = ["fetch", "land", "profile", "ingest", "materialize", "bench", "columns", "report"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bakeoff", description=__doc__)
    p.add_argument("stage", choices=[*STAGES, "all", "serve", "docs", "clean"])
    p.add_argument("--csv", type=Path, help="benchmark your own CSV instead of the taxi dataset")
    p.add_argument("--sort-key", help="with --csv: column to sort variants by (default: first column)")
    p.add_argument("--only", help="regex over variant ids, e.g. 'parquet|orc-zstd'")
    p.add_argument("--publish", action="store_true", help="report: also copy results into LAKE_PUBLISHED_DIR")
    p.add_argument("--out", type=Path, default=Path("docs"), help="docs: output directory")
    args = p.parse_args(argv)

    load_dotenv()
    try:
        s = Settings.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    setup_logging(s.log_level)
    new_correlation_id()

    if args.stage == "serve":
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(s), host="0.0.0.0", port=s.port, log_config=None)  # noqa: S104
        return 0
    if args.stage == "docs":
        from .api import create_app
        write_docs(s, args.out)
        (args.out / "openapi.json").write_text(json.dumps(create_app(s).openapi(), indent=1) + "\n", encoding="utf-8",
                                               newline="\n")
        return 0
    if args.stage == "clean":  # derived data only; raw/ is immutable and stays
        for name in ("landing", "clean", "quarantine", "variants", "results", "tmp", "stages.json"):
            target = s.data_dir / name
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
        return 0

    stages = [args.stage]
    if args.stage == "all":
        skip = {"fetch", "land"} if args.csv else {"fetch"} if s.source != "tlc" else set()
        stages = [st for st in STAGES if st not in skip]
    failed_cells = 0
    try:
        landing, ds = data.generic_dataset(args.csv, args.sort_key) if args.csv else data.taxi_dataset(s)
        for stage in stages:
            with timed(s, stage) as info:
                if stage == "fetch":
                    data.fetch(s, info)
                elif stage == "land":
                    data.land(s, info)
                elif stage == "profile":
                    data.profile(s, landing, info)
                elif stage == "ingest":
                    data.ingest(s, landing, ds, info)
                elif stage == "materialize":
                    bench_mod.materialize(s, info, args.only)
                elif stage == "bench":
                    failed_cells = bench_mod.bench(s, ds, info, args.only)
                elif stage == "columns":
                    bench_mod.column_lab(s, info)
                elif stage == "report":
                    report(s, ds, info, args.publish)
    except data.DataError as e:
        log("failed", logging.ERROR, error=str(e))
        print(f"error: {e}", file=sys.stderr)
        return 2
    if failed_cells:
        print(f"error: {failed_cells} benchmark cell(s) failed; results are partial. Re-run `bakeoff bench` "
              "to retry only those.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
