"""Versioned SQL migrations: migrations/NNNN_name.up.sql + NNNN_name.down.sql.

Every migration ships with a rollback. Applied migrations are checksummed; editing one after
it was applied is an error (ship a new migration instead). Each migration runs in its own
transaction, so a failure leaves the schema at the previous version.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from goldstandard.config import settings

NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.up\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up.encode()).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    directory = directory or settings().migrations_dir
    found = []
    for up in sorted(directory.glob("*.up.sql")):
        m = NAME.match(up.name)
        if not m:
            raise ValueError(f"bad migration filename: {up.name}")
        down = up.with_name(up.name.replace(".up.sql", ".down.sql"))
        if not down.exists():
            raise ValueError(f"migration {up.name} has no rollback ({down.name})")
        found.append(Migration(int(m[1]), m[2], up.read_text(), down.read_text()))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migration versions must be contiguous from 1: {versions}")
    return found


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    integer PRIMARY KEY,
            name       text NOT NULL,
            checksum   char(64) NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )""")


def current_version(conn: psycopg.Connection) -> int:
    _ensure_table(conn)
    row = conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row else 0


def upgrade(dsn: str, target: int | None = None) -> list[int]:
    migrations = discover()
    applied: list[int] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        done = {int(v): c for v, c in rows}
        for m in migrations:
            if m.version in done:
                if done[m.version] != m.checksum:
                    raise RuntimeError(f"migration {m.version}_{m.name} was edited after being applied")
                continue
            if target is not None and m.version > target:
                break
            with conn.transaction():
                conn.execute(m.up)  # trusted file from the repo
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            applied.append(m.version)
    return applied


def downgrade(dsn: str, target: int) -> list[int]:
    migrations = {m.version: m for m in discover()}
    reverted: list[int] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        version = current_version(conn)
        while version > target:
            m = migrations[version]
            with conn.transaction():
                conn.execute(m.down)
                conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            reverted.append(version)
            version -= 1
    return reverted
