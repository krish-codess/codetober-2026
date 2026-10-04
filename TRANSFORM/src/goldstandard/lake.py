"""Columnar intermediate store: Parquet partitions per (dataset, world, day), scanned with DuckDB.

Layout: <lake>/<dataset>/w=<world>/dt=<YYYY-MM-DD>/part-0.parquet
  * one file per partition, rewritten atomically (tmp + rename) -> a partition is either the old
    or the new version, never half-written; rewriting a partition is how a backfill lands.
  * the partition keys live in the path (hive style, named w/dt so they never clash with data
    columns); DuckDB prunes files on them before reading (predicate pushdown) and reads only the
    requested columns (projection pushdown) - see docs/PERFORMANCE.md for the measured effect.
  * zstd compression, row groups of ROW_GROUP rows: a day partition is 10^4-10^5 rows, so most
    files are one or two row groups; big enough for good compression, small enough that min/max
    statistics still prune within a file when filtering by server or item.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta
from pathlib import Path

import duckdb
import polars as pl

ROW_GROUP = 64_000


class Lake:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, dataset: str, world: str, day: date) -> Path:
        return self.root / dataset / f"w={world}" / f"dt={day.isoformat()}"

    def write(self, dataset: str, world: str, day: date, df: pl.DataFrame) -> str:
        """Atomically replace one partition. Returns a content fingerprint of the frame."""
        d = self._dir(dataset, world, day)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".part-0.{os.getpid()}.tmp"
        df.write_parquet(tmp, compression="zstd", row_group_size=ROW_GROUP, statistics=True)
        os.replace(tmp, d / "part-0.parquet")
        return fingerprint(df)

    def exists(self, dataset: str, world: str, day: date) -> bool:
        return (self._dir(dataset, world, day) / "part-0.parquet").exists()

    def read_day(self, dataset: str, world: str, day: date) -> pl.DataFrame | None:
        p = self._dir(dataset, world, day) / "part-0.parquet"
        return pl.read_parquet(p) if p.exists() else None

    def days(self, dataset: str, world: str) -> list[date]:
        d = self.root / dataset / f"w={world}"
        if not d.exists():
            return []
        return sorted(date.fromisoformat(p.name[3:]) for p in d.glob("dt=*") if (p / "part-0.parquet").exists())

    def scan(
        self,
        dataset: str,
        world: str,
        start: date | None = None,
        end: date | None = None,
        columns: list[str] | None = None,
        where: str = "",
        params: list[object] | None = None,
    ) -> pl.DataFrame:
        """DuckDB scan with partition pruning on dt and projection pushdown on `columns`.
        `where` is a trusted, code-defined SQL fragment; every value goes through `params`.

        Pruning happens before DuckDB sees anything: only the partition files inside [start, end] are
        resolved (O(range) existence checks, not a glob over the whole history), then DuckDB reads only
        the requested columns from those files."""
        if start is not None and end is not None:
            span = (end - start).days + 1
            candidates = [self._dir(dataset, world, start + timedelta(days=i)) / "part-0.parquet" for i in range(span)]
            files = [f.as_posix() for f in candidates if f.exists()]
        else:
            files = [
                (self._dir(dataset, world, d) / "part-0.parquet").as_posix()
                for d in self.days(dataset, world)
                if (start is None or d >= start) and (end is None or d <= end)
            ]
        if not files:
            return pl.DataFrame()
        cols = ", ".join(f'"{c}"' for c in columns) if columns else "* EXCLUDE (w, dt)"
        sql = f"SELECT {cols} FROM read_parquet(?, hive_partitioning = true, union_by_name = true) WHERE true"  # noqa: S608
        args: list[object] = [files]
        if where:
            sql += f" AND ({where})"
            args.extend(params or [])
        with duckdb.connect() as con:
            return con.execute(sql, args).pl()


def fingerprint(df: pl.DataFrame) -> str:
    """Order-independent content hash of a frame (64 hex)."""
    if df.is_empty():
        return hashlib.sha256(b"empty:" + ",".join(df.columns).encode()).hexdigest()
    h = df.hash_rows(seed=1, seed_1=2, seed_2=3, seed_3=4).sort()
    return hashlib.sha256(h.to_numpy().tobytes() + ",".join(df.columns).encode()).hexdigest()
