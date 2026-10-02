"""Command line entry point: `goldstandard <command>`. Every command calls library code that
Dagster also calls, so nothing here is logic that only runs on one laptop."""

from __future__ import annotations

import argparse
import json
import os
import sys

from goldstandard.config import settings
from goldstandard.obs import configure_logging


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


def main(argv: list[str] | None = None) -> None:
    cfg = settings()
    configure_logging(cfg.log_level, cfg.log_json)
    parser = argparse.ArgumentParser(prog="goldstandard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="apply or roll back schema migrations")
    m.add_argument("direction", choices=["up", "down"])
    m.add_argument("--target", type=int, default=None)
    m.set_defaults(fn=cmd_migrate)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
