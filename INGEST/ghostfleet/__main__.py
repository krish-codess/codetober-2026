"""Command line: every stage of the pipeline is one independently runnable subcommand."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import obs
from .config import Settings

log = logging.getLogger("ghostfleet")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ghostfleet", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply schema migrations (or roll back with --to)")
    m.add_argument("--to", type=int, default=None, help="target version; lower than current rolls back")
    sub.add_parser("topics", help="create Kafka topics")
    sub.add_parser("init", help="migrate + topics")
    sub.add_parser("collect", help="hold provider connections, archive raw input, produce to Kafka")
    sub.add_parser("sink", help="consumer group: Kafka -> PostGIS")
    d = sub.add_parser("detect", help="gap detector loop")
    d.add_argument("--once", action="store_true", help="refresh coverage, run one pass, print counts, exit")
    a = sub.add_parser("api", help="serve the HTTP API (and the built UI if web/dist exists)")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("sim", help="simulated AIS world: serve it live over TCP, or write a history file")
    s.add_argument("--ships", type=int, default=500)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--step", type=int, default=60, help="seconds between reports of a moving vessel")
    s.add_argument("--clean", action="store_true", help="no injected defects")
    s.add_argument("--serve", metavar="HOST:PORT", help="run as a live NMEA relay")
    s.add_argument("--drop-every", type=int, default=0, help="live: drop all clients every N seconds")
    s.add_argument("--out", type=Path, help="history: write NMEA lines here")
    s.add_argument("--truth", type=Path, help="history: write ground truth JSON here")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--end", type=int, default=None, help="history: end time, unix seconds (default: now)")
    r = sub.add_parser("replay", help="push an archived NMEA file or directory through decode -> Kafka again")
    r.add_argument("path", type=Path)
    r.add_argument("--source", default="replay")
    q = sub.add_parser("replay-dlq", help="re-submit quarantined inputs after a fix")
    q.add_argument("--reason", required=True)
    e = sub.add_parser("evaluate", help="score dark_events against simulator ground truth")
    e.add_argument("truth", type=Path)
    pr = sub.add_parser("profile", help="profile a raw NMEA capture, print markdown")
    pr.add_argument("path", type=Path)
    o = sub.add_parser("openapi", help="write the OpenAPI document generated from the code")
    o.add_argument("out", type=Path)
    h = sub.add_parser("health", help="exit 0 if a worker's metrics show a recent dependency round trip")
    h.add_argument("url")
    h.add_argument("--max-age", type=float, default=60)
    lt = sub.add_parser("loadtest", help="measure end-to-end ingest latency under load")
    lt.add_argument("--rate", type=int, default=2000, help="position reports per second")
    lt.add_argument("--seconds", type=int, default=30)
    sd = sub.add_parser("seed-db", help="simulated history straight into PostGIS plus one detector pass (no Kafka)")
    sd.add_argument("--ships", type=int, default=1200)
    sd.add_argument("--hours", type=float, default=6)
    args = p.parse_args(argv)

    cfg = Settings()
    obs.setup_logging(cfg.log_level)

    if args.cmd in ("migrate", "init"):
        from .db import migrate

        version = asyncio.run(migrate(cfg.dsn, getattr(args, "to", None)))
        print(f"schema at version {version}")
    if args.cmd in ("topics", "init"):
        from .bus import ensure_topics

        asyncio.run(ensure_topics(cfg))
        print(f"topics ready on {cfg.kafka}")
    elif args.cmd == "collect":
        from . import collectors

        asyncio.run(collectors.run(cfg))
    elif args.cmd == "sink":
        from . import sink

        asyncio.run(sink.run(cfg))
    elif args.cmd == "detect":
        from . import detect

        if args.once:
            print(json.dumps(asyncio.run(_detect_once(cfg))))
        else:
            asyncio.run(detect.run(cfg))
    elif args.cmd == "api":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_config=None, access_log=False)
    elif args.cmd == "sim":
        _sim(args)
    elif args.cmd == "replay":
        from .collectors import replay

        stats = asyncio.run(replay(args.path, args.source, cfg))
        print(f"replayed {stats.lines} lines -> {stats.records} records")
    elif args.cmd == "replay-dlq":
        from .evaluate import replay_dlq

        print(f"re-submitted {asyncio.run(replay_dlq(cfg, args.reason))} quarantined inputs")
    elif args.cmd == "evaluate":
        from .evaluate import evaluate

        print(asyncio.run(evaluate(cfg, json.loads(args.truth.read_text()))))
    elif args.cmd == "profile":
        from .profile import profile

        sys.stdout.write(profile(args.path))
    elif args.cmd == "openapi":
        from .api import create_app

        args.out.write_text(
            json.dumps(create_app(cfg).openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    elif args.cmd == "health":
        try:
            ok = obs.health(args.url, args.max_age)
        except OSError:
            ok = False
        sys.exit(0 if ok else 1)
    elif args.cmd == "seed-db":
        from .evaluate import seed_db

        print(asyncio.run(seed_db(cfg, args.ships, args.hours)))
    elif args.cmd == "loadtest":
        from .evaluate import loadtest

        print(asyncio.run(loadtest(cfg, args.rate, args.seconds)))


async def _detect_once(cfg: Settings) -> dict[str, int]:
    import asyncpg

    from .detect import detect, refresh_coverage

    conn = await asyncpg.connect(cfg.dsn)
    try:
        cells = await refresh_coverage(conn, cfg)
        return {"coverage_cells": cells, **await detect(conn, cfg)}
    finally:
        await conn.close()


def _sim(args: argparse.Namespace) -> None:
    import time

    from .simulate import World, serve

    world = World(seed=args.seed, n_ships=args.ships, step_s=args.step, defects=not args.clean)
    if args.serve:
        host, _, port = args.serve.rpartition(":")
        asyncio.run(serve(world, host or "0.0.0.0", int(port), drop_every_s=args.drop_every))  # noqa: S104 - a relay listens
        return
    if not args.out:
        raise SystemExit("sim needs --serve HOST:PORT or --out FILE")
    end = args.end if args.end is not None else int(time.time())
    start = end - int(args.hours * 3600)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with args.out.open("w", encoding="ascii", newline="\n") as f:
        for _, line in world.sentences(start, end):
            f.write(line + "\n")
            n += 1
    if args.truth:
        args.truth.write_text(json.dumps([t.as_dict() for t in world.truth], indent=1), encoding="utf-8")
    print(f"wrote {n} sentences for {args.ships} ships over {args.hours} h; {len(world.truth)} ground-truth silences")


if __name__ == "__main__":
    main()
