"""Command line: python -m squeeze <command>."""

from __future__ import annotations

import argparse

from . import config, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="squeeze")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    cfg = config.load()
    log.setup(cfg.log_level)
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
