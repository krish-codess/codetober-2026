"""Connection pool and a deliberately small migration runner: numbered .up.sql / .down.sql pairs."""

from __future__ import annotations

import logging
import os
from importlib import resources

import asyncpg

from .validate import mid_table

log = logging.getLogger(__name__)
ROLES = ("ghost_ingest", "ghost_detect", "ghost_api")


async def pool(dsn: str, size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=size, command_timeout=60)


def _migrations() -> dict[int, tuple[str, str, str]]:
    """version -> (name, up sql, down sql). A migration without a rollback is refused outright."""
    files = {
        p.name: p.read_text(encoding="utf-8") for p in resources.files("ghostfleet").joinpath("migrations").iterdir()
    }
    out = {}
    for name, sql in files.items():
        if name.endswith(".up.sql"):
            stem = name.removesuffix(".up.sql")
            out[int(stem.split("_")[0])] = (stem, sql, files[f"{stem}.down.sql"])
    return out


async def migrate(dsn: str, target: int | None = None) -> int:
    """Move the schema up or down to `target` (default: latest). Each step is one transaction."""
    migrations = _migrations()
    target = max(migrations) if target is None else target
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        current = await conn.fetchval("SELECT coalesce(max(version), 0) FROM schema_migrations")
        for v in range(current + 1, target + 1):
            async with conn.transaction():
                await conn.execute(migrations[v][1])
                await conn.execute("INSERT INTO schema_migrations (version, name) VALUES ($1, $2)", v, migrations[v][0])
            log.info("migrated up", extra={"version": v, "migration": migrations[v][0]})
        for v in range(current, target, -1):
            async with conn.transaction():
                await conn.execute(migrations[v][2])
                await conn.execute("DELETE FROM schema_migrations WHERE version = $1", v)
            log.info("migrated down", extra={"version": v, "migration": migrations[v][0]})
        if target >= 1:
            await conn.executemany(
                "INSERT INTO mid_countries (mid, iso2, country) VALUES ($1, $2, $3) "
                "ON CONFLICT (mid) DO UPDATE SET iso2 = excluded.iso2, country = excluded.country",
                [(mid, iso2, country) for mid, (iso2, country) in mid_table().items()],
            )
        if target >= 2:
            for role in ROLES:
                password = os.environ.get(f"{role.upper()}_PASSWORD")
                if password:
                    # ALTER ROLE takes no bind parameters; format(%L) does the quoting server-side.
                    await conn.execute(
                        await conn.fetchval(
                            "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, password
                        )
                    )
        return target
    finally:
        await conn.close()
