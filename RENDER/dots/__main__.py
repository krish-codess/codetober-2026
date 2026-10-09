"""`python -m dots <stage>`: every stage is independently runnable and idempotent."""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import fetch
from .config import REPO, Settings, event, read_json, settings, write_json


def report(s: Settings, source: str) -> None:
    """Copy each stage's measurements into results/<source>/ so the numbers quoted in the docs are committed."""
    work, out = s.work(source), REPO / "results" / source
    out.mkdir(parents=True, exist_ok=True)
    for f in [
        work / n for n in ("ingest.json", "profile.json", "cluster.json", "eval.json", "experiments.jsonl")
    ] + sorted((work / "proj").glob("*.json")):
        if f.exists():
            shutil.copyfile(f, out / (f.name if f.parent == work else f"proj-{f.name}"))
    m = read_json(s.site_dir / "data" / "manifest.json")
    write_json(
        out / "tiles.json",
        {
            "build_id": m["build_id"],
            "tile_capacity": m["tile_capacity"],
            "meta_bytes": m["meta"]["bytes"],
            "variants": {
                v["name"]: {
                    "tiles": len(v["tiles"]),
                    "bytes": v["bytes"],
                    "max_depth": max((t[0] for t in v["tiles"]), default=0),
                    "n_points": v["n_points"],
                    "dropped_non_finite": v["dropped_non_finite"],
                }
                for v in m["variants"]
            },
        },
    )
    event("report written", dir=str(out))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dots", description=__doc__)
    p.add_argument(
        "stage",
        choices=["fetch", "ingest", "project", "evaluate", "tile", "report", "all", "serve", "openapi"],
    )
    p.add_argument("out", nargs="?", help="openapi: output path")
    p.add_argument("--force", action="store_true", help="redo the stage even if its outputs are current")
    a = p.parse_args(argv)
    s = settings()

    if a.stage == "openapi":
        from .api import create_app

        text = json.dumps(create_app(s).openapi(), indent=1, sort_keys=True) + "\n"
        (open(a.out, "w", encoding="utf-8") if a.out else sys.stdout).write(text)
        return 0
    if a.stage == "serve":
        import os

        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(s),
            host=os.environ.get("DOTS_HOST", "127.0.0.1"),
            port=int(os.environ.get("DOTS_PORT", "8015")),
            log_config=None,
        )
        return 0

    stages = ["fetch", "ingest", "project", "evaluate", "tile", "report"] if a.stage == "all" else [a.stage]
    for stage in stages:
        if stage == "fetch":
            fetch.run(s)
            continue
        source = fetch.current_source(s)
        if stage == "ingest":
            from . import ingest

            ingest.run(s, source, a.force)
        elif stage == "project":
            from . import project

            project.run(s, source, a.force)
        elif stage == "evaluate":
            from . import evaluate

            evaluate.run(s, source)
        elif stage == "tile":
            from . import tiles

            tiles.run(s, source)
        elif stage == "report":
            report(s, source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
