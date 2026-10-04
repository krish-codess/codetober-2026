"""Runs a subject's SQL models over a Dataset on DuckDB or PostgreSQL.

Tables and views are created once per connection as TEMP objects; each run only
swaps the rows. That keeps one pipeline execution in the low milliseconds and
means the PostgreSQL role needs nothing beyond TEMP on the database.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb

from tydlc.schema import SQL_TYPES, Dataset

if TYPE_CHECKING:
    from tydlc.subjects import Subject

ENGINES = ("duckdb", "postgres")

# DuckDB's Python client attempts `import pandas` twice per bound parameter. When pandas
# is absent each attempt is an uncached filesystem search (~1.5 ms/parameter measured,
# docs/performance.md). A None entry makes the failed import instant.
if importlib.util.find_spec("pandas") is None:
    sys.modules["pandas"] = None  # type: ignore[assignment]


def _plain(v: Any) -> Any:
    """PostgreSQL returns NUMERIC as Decimal; properties compare ints and floats."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class Engine:
    def __init__(self, name: str, conn: Any, placeholder: str, subject: Subject) -> None:
        self.name, self.conn, self.ph, self.subject = name, conn, placeholder, subject
        self.runs = 0
        self._cache: dict[str, Dataset] = {}
        for t in subject.schema:
            cols = ", ".join(f"{c.name} {SQL_TYPES[c.type]}" for c in t.columns)
            conn.execute(f"CREATE TEMP TABLE {t.name} ({cols})")
        for model, sql in subject.models:
            conn.execute(f"CREATE TEMP VIEW {model} AS {sql}")

    def run(self, ds: Dataset) -> Dataset:
        """Memoised `execute`. Every shrink search replays the same seeded generation
        and converges on the same tiny datasets, so most of its executions are repeats."""
        key = repr(ds)
        if key not in self._cache:
            self._cache[key] = self.execute(ds)
        return self._cache[key]

    def execute(self, ds: Dataset) -> Dataset:
        """Load `ds` into the raw tables and return every model's rows."""
        self.runs += 1
        for t in self.subject.schema:
            self.conn.execute(f"DELETE FROM {t.name}")
            rows = ds[t.name]
            if rows:
                names = [c.name for c in t.columns]
                one = "(" + ", ".join([self.ph] * len(names)) + ")"
                self.conn.execute(
                    f"INSERT INTO {t.name} ({', '.join(names)}) VALUES "
                    + ", ".join([one] * len(rows)),
                    [r[n] for r in rows for n in names],
                )
        out: Dataset = {}
        for model, _ in self.subject.models:
            cur = self.conn.execute(f"SELECT * FROM {model}")
            names = [d[0] for d in cur.description]
            out[model] = [dict(zip(names, map(_plain, r), strict=True)) for r in cur.fetchall()]
        return out

    def close(self) -> None:
        self.conn.close()


def make_engine(name: str, subject: Subject, dsn: str | None = None) -> Engine:
    if name == "duckdb":
        return Engine(name, duckdb.connect(config={"threads": 1}), "?", subject)
    if name == "postgres":
        from tydlc.store import connect  # lazy: psycopg is an optional dependency

        if not dsn:
            raise ValueError("the postgres engine needs DATABASE_URL")
        return Engine(name, connect(dsn), "%s", subject)
    raise ValueError(f"unknown engine {name!r}; expected one of {ENGINES}")
