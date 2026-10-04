"""Engine factory and bulk writes. All SQL in this codebase goes through `sqlalchemy.text` with
bound parameters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from .config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping: a restarted database costs one failed ping, not a failed request.
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def bulk_insert(
    conn: Connection,
    table: str,
    cols: dict[str, str],
    rows: Sequence[Mapping[str, Any]],
    tail: str = "",
    chunk: int = 5000,
) -> int:
    """INSERT ... SELECT FROM unnest(arrays): one round trip per `chunk` rows instead of one per row.
    `table`, `cols` and `tail` are code constants, never user input; every value is a bound parameter."""
    arrays = ", ".join(f"CAST(:{name} AS {pg_type}[])" for name, pg_type in cols.items())
    sql = text(f"INSERT INTO {table} ({', '.join(cols)}) SELECT * FROM unnest({arrays}) {tail}")  # noqa: S608
    total = 0
    for start in range(0, len(rows), chunk):
        part = rows[start : start + chunk]
        total += conn.execute(sql, {name: [r[name] for r in part] for name in cols}).rowcount
    return total
