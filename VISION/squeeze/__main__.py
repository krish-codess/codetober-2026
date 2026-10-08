"""Command line: python -m squeeze <command>. Every stage is its own command and is idempotent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="squeeze", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the API (and the built web UI if present)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("data", help="validate a raw image folder, quarantine what fails, cache the rest")
    p.add_argument("raw", type=Path)

    p = sub.add_parser("pipeline", help="run or resume the optimisation pipeline")
    p.add_argument("--smoke", action="store_true", help="tiny configuration, for checking the plumbing")

    p = sub.add_parser("package", help="build deployment packages from a pipeline run")
    p.add_argument("--target", action="append", help="target name (default: all)")
    p.add_argument("--out", type=Path, help="output directory (default: <data dir>/packages)")
    p.add_argument("--run", help="run id (default: most recent)")

    args = parser.parse_args(argv)
    cfg = config.load()
    log.setup(cfg.log_level)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_config=None, access_log=False)

    elif args.cmd == "data":
        from . import data

        ds = data.build(args.raw, cfg.data_dir / "dataset")
        print(json.dumps({"version": ds.version, **ds.profile}, indent=1))

    elif args.cmd == "pipeline":
        from . import data, pipeline

        ds = data.load(cfg.data_dir / "dataset")
        record = pipeline.execute(
            pipeline.SMOKE if args.smoke else pipeline.Config(), ds, cfg.data_dir / "runs"
        )
        if not args.smoke:
            cfg.raw_dir.mkdir(parents=True, exist_ok=True)
            (cfg.raw_dir / f"pipeline-{record['run_id']}.json").write_text(json.dumps(record, indent=1))
        for v in record["variants"]:
            print(f"{v['name']:26} {v['precision']:9} top1 {v['eval']['top1']:.3f}  gate {v['gate']}")

    elif args.cmd == "package":
        from . import data, package

        runs = cfg.data_dir / "runs"
        latest = max(runs.glob("*/run.json"), key=lambda p: p.stat().st_mtime).parent
        ds = data.load(cfg.data_dir / "dataset")
        all_targets = package.targets()
        for name in args.target or list(all_targets):
            record = package.build(
                runs / args.run if args.run else latest,
                ds,
                all_targets[name],
                args.out or cfg.data_dir / "packages",
            )
            mb = record["size_bytes"] / 2**20
            print(f"{record['filename']}  {mb:.1f} MB  sha256 {record['package_id'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
