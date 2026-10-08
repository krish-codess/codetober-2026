"""Command line: python -m squeeze <command>. Every stage is its own command and is idempotent."""

from __future__ import annotations

import argparse
import json
import os
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
    p.add_argument("--reuse", metavar="RUN_ID", help="copy student-independent steps from this earlier run")

    p = sub.add_parser("package", help="build deployment packages from a pipeline run")
    p.add_argument("--target", action="append", help="target name (default: all)")
    p.add_argument("--out", type=Path, help="output directory (default: <data dir>/packages)")
    p.add_argument("--run", help="run id (default: most recent)")

    p = sub.add_parser("ingest", help="rebuild the database from the raw result files (idempotent)")
    p.add_argument("dir", type=Path, nargs="?", help="default: $SQUEEZE_RAW_DIR")

    p = sub.add_parser("openapi", help="write the API reference generated from the code")
    p.add_argument("out", type=Path)

    p = sub.add_parser("bench", help="unpack a target's package and run the device harness on THIS machine")
    p.add_argument("--target", required=True)
    p.add_argument("--packages", type=Path, help="directory holding the package (default: <data dir>/packages)")
    p.add_argument("--out", type=Path, help="results file to append to (default: <raw dir>/bench-<target>.jsonl)")
    p.add_argument("harness", nargs=argparse.REMAINDER, help="after --, arguments for edge_bench.py")

    p = sub.add_parser("regress", help="CI gate: compare benchmark results with the committed baseline")
    p.add_argument("results", type=Path)
    p.add_argument("--baseline", type=Path, default=Path("release/baseline.json"))
    p.add_argument("--tolerance", type=float, default=float(os.environ.get("SQUEEZE_REGRESS_TOL", "0.3")))
    p.add_argument("--update", action="store_true", help="record these results as the baseline for this CPU")

    p = sub.add_parser("seed-synthetic", help="load generated results (flagged synthetic) for load testing")
    p.add_argument("--n", type=int, default=20000)

    p = sub.add_parser("loadtest", help="measure API latency under load")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--concurrency", type=int, default=16)

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
        chosen = pipeline.SMOKE if args.smoke else pipeline.Config()
        record = pipeline.execute(chosen, ds, cfg.data_dir / "runs", args.reuse)
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
        all_targets = config.targets()
        for name in args.target or list(all_targets):
            record = package.build(
                runs / args.run if args.run else latest,
                ds,
                all_targets[name],
                args.out or cfg.data_dir / "packages",
            )
            mb = record["size_bytes"] / 2**20
            print(f"{record['filename']}  {mb:.1f} MB  sha256 {record['package_id'][:16]}")

    elif args.cmd == "ingest":
        from . import db

        con = db.connect(cfg.db_path)
        db.migrate(con)
        print(json.dumps(db.ingest_dir(con, args.dir or cfg.raw_dir, set(config.targets()))))

    elif args.cmd == "openapi":
        from .api import create_app

        args.out.write_text(json.dumps(create_app(cfg).openapi(), indent=1, sort_keys=True) + "\n")

    elif args.cmd == "bench":
        import subprocess
        import tarfile
        import tempfile

        packages = args.packages or cfg.data_dir / "packages"
        archive = max(packages.glob(f"squeeze-{args.target}-*.tar.gz"), key=lambda p: p.stat().st_mtime)
        out = (args.out or cfg.raw_dir / f"bench-{args.target}.jsonl").resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter="data")
            pkg = next(Path(tmp).iterdir())
            extra = [a for a in args.harness if a != "--"]
            # The harness exactly as it ships in the package, in its own process.
            command = [sys.executable, str(pkg / "edge_bench.py"), "--package", str(pkg), "--out", str(out), *extra]
            return subprocess.run(command, check=False).returncode  # noqa: S603

    elif args.cmd == "regress":
        from . import regress

        return regress.run(args.results, args.baseline, args.tolerance, args.update)

    elif args.cmd in ("seed-synthetic", "loadtest"):
        from . import db, synth

        con = db.connect(cfg.db_path)
        db.migrate(con)
        variants = [
            {"name": r["name"], "sha256": r["model_sha256"]}
            for r in con.execute("SELECT name, model_sha256 FROM variant ORDER BY position")
        ] or [{"name": "student-kd", "sha256": "0" * 64}]
        if args.cmd == "seed-synthetic":
            counts: dict[str, int] = {}
            for payload, _ in synth.bench_results(variants, list(config.targets()), args.n, seed=1):
                status = db.ingest_bench(con, payload, "generator", set(config.targets()))[0]
                counts[status] = counts.get(status, 0) + 1
            print(json.dumps(counts))
        else:
            from . import loadtest

            fresh = [p for p, _ in synth.bench_results(variants, ["rpi5"], 100_000, seed=7, defects=False)]
            report = loadtest.run(args.url, cfg.device_token, args.seconds, args.concurrency, fresh)
            print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
