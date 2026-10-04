"""Boundary for real data: raw CSV -> validated Parquet + quarantine Parquet.

Raw files are only ever read. Every run rebuilds its outputs from them, so the stage
is idempotent and a quarantined row (say a payment that arrived before its order)
is picked up by simply re-running once the parent has landed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

from tydlc.schema import SQL_TYPES, Dataset, Schema, Table


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _reason_sql(t: Table) -> str:
    """One CASE naming the first rule a row breaks; NULL means the row is valid."""
    whens = []
    for c in t.columns:
        typed = f"TRY_CAST({c.name} AS {SQL_TYPES[c.type]})"
        if not c.nullable:
            whens.append(f"WHEN {c.name} IS NULL THEN '{c.name}: missing'")
        whens.append(f"WHEN {c.name} IS NOT NULL AND {typed} IS NULL "
                     f"THEN '{c.name}: not a {c.type}'")
        if c.accepted:
            allowed = ", ".join(_lit(v) for v in c.accepted)
            whens.append(f"WHEN {c.name} NOT IN ({allowed}) THEN '{c.name}: not an accepted value'")
        if c.unique:  # first occurrence wins, later ones are quarantined
            whens.append(f"WHEN row_number() OVER (PARTITION BY {c.name} ORDER BY _line) > 1 "
                         f"THEN '{c.name}: duplicate'")
        if c.references:
            parent, pcol = c.references
            whens.append(f"WHEN {typed} NOT IN (SELECT {pcol} FROM {parent}) "
                         f"THEN '{c.name}: no such {parent}.{pcol}'")
    return "CASE " + " ".join(whens) + " END"


def ingest(schema: Schema, src: Path, out: Path) -> dict[str, dict[str, int]]:
    """Validate `<src>/<table>.csv` for each table. Returns per-table staged/quarantined counts."""
    con = duckdb.connect()
    counts: dict[str, dict[str, int]] = {}
    manifest = {}
    for sub in ("staged", "quarantine"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for t in schema:
        path = src / f"{t.name}.csv"
        manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        # all_varchar: nothing is coerced before we have looked at it.
        # null_padding: a short row becomes NULLs (then fails NOT NULL) instead of aborting.
        con.execute("CREATE OR REPLACE TABLE _src AS SELECT * FROM "
                    "read_csv(?, all_varchar=true, header=true, null_padding=true)", [str(path)])
        present = {r[0] for r in con.execute("DESCRIBE _src").fetchall()}
        cols = ", ".join(c.name if c.name in present else f"NULL AS {c.name}" for c in t.columns)
        con.execute(f"CREATE OR REPLACE TABLE _lined AS "
                    f"SELECT {cols}, row_number() OVER () AS _line FROM _src")
        con.execute(f"CREATE OR REPLACE TABLE _checked AS "
                    f"SELECT *, {_reason_sql(t)} AS _reason FROM _lined")
        typed = ", ".join(f"CAST({c.name} AS {SQL_TYPES[c.type]}) AS {c.name}" for c in t.columns)
        con.execute(f"CREATE TABLE {t.name} AS SELECT {typed} FROM _checked "
                    f"WHERE _reason IS NULL ORDER BY _line")
        # Tables here are far below one row group; 122,880 rows is DuckDB's default and
        # the right size for min/max pruning once a source grows past it.
        con.execute(f"COPY {t.name} TO {_lit(str(out / 'staged' / f'{t.name}.parquet'))} "
                    f"(FORMAT parquet, ROW_GROUP_SIZE 122880)")
        con.execute(f"COPY (SELECT * FROM _checked WHERE _reason IS NOT NULL ORDER BY _line) "
                    f"TO {_lit(str(out / 'quarantine' / f'{t.name}.parquet'))} (FORMAT parquet)")
        staged = con.execute(f"SELECT count(*) FROM {t.name}").fetchall()[0][0]
        total = con.execute("SELECT count(*) FROM _checked").fetchall()[0][0]
        counts[t.name] = {"staged": staged, "quarantined": total - staged}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return counts


def _read(path: Path, columns: str = "*") -> list[dict[str, Any]]:
    cur = duckdb.connect().execute(f"SELECT {columns} FROM read_parquet(?)", [str(path)])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def load_staged(schema: Schema, out: Path) -> Dataset:
    return {t.name: _read(out / "staged" / f"{t.name}.parquet",
                          ", ".join(c.name for c in t.columns)) for t in schema}


def load_quarantine(table: str, out: Path) -> list[dict[str, Any]]:
    return _read(out / "quarantine" / f"{table}.parquet")


def profile(schema: Schema, src: Path) -> str:
    """Markdown profile of the raw CSVs: inferred type, nulls, cardinality, range."""
    con = duckdb.connect()
    lines = []
    for t in schema:
        rows = con.execute("SUMMARIZE SELECT * FROM read_csv(?, header=true)",
                           [str(src / f"{t.name}.csv")]).fetchall()
        lines += [f"### {t.name} ({rows[0][10]} rows)", "",
                  "| column | inferred type | null % | approx distinct | min | max |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[11]} | {r[4]} | {r[2]} | {r[3]} |" for r in rows]
        lines.append("")
    return "\n".join(lines)
