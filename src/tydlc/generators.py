"""Hypothesis strategies built from a Schema: hostile values, valid references."""

from __future__ import annotations

import datetime as dt
from functools import cache
from typing import Any

from hypothesis import strategies as st

from tydlc.schema import Column, Dataset, Schema, Table

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1

# Values that have broken real pipelines: unit boundaries, type limits, lookalike nulls,
# quoting, whitespace, non-BMP and width-variant text.
HOSTILE_INTS = [0, 1, -1, 99, 100, 101, 149, 150, INT32_MAX, INT32_MIN]
HOSTILE_TEXT = ["", " ", "NULL", "null", "N/A", "O'Brien", 'say "hi"', "a,b", "line\nbreak",
                "Robert'); DROP TABLE raw_customers;--", "ｆｕｌｌ", "𝓤𝓷𝓲", "İstanbul", "x" * 300]
HOSTILE_DATES = [dt.date(1, 1, 1), dt.date(9999, 12, 31), dt.date(1970, 1, 1),
                 dt.date(2000, 2, 29), dt.date(1900, 2, 28)]

# The general strategy comes first so shrinking lands on 0 / "" / 2000-01-01.
# NUL and lone surrogates are excluded: PostgreSQL text cannot store them, so they
# would test the driver rather than the pipeline.
_BASE: dict[str, st.SearchStrategy[Any]] = {
    "int": st.integers(INT32_MIN, INT32_MAX) | st.sampled_from(HOSTILE_INTS),
    "str": st.text(st.characters(exclude_characters="\x00", exclude_categories=["Cs"]),
                   max_size=20) | st.sampled_from(HOSTILE_TEXT),
    "date": st.dates() | st.sampled_from(HOSTILE_DATES),
}


MAX_ROWS = 6


def _column(col: Column) -> st.SearchStrategy[Any]:
    if col.references:
        # An index into the parent rows, resolved in `datasets`. Drawing the parent key
        # directly would need a fresh sampled_from per example, and building strategies
        # inside the draw made generation ~6x slower (docs/performance.md).
        base: st.SearchStrategy[Any] = st.integers(0, MAX_ROWS - 1)
    elif col.accepted:
        base = st.sampled_from(col.accepted)
    else:
        base = _BASE[col.type]
    return st.none() | base if col.nullable else base


@cache
def _rows(table: Table) -> st.SearchStrategy[list[dict[str, Any]]]:
    return st.lists(st.fixed_dictionaries({c.name: _column(c) for c in table.columns}),
                    max_size=MAX_ROWS)


@st.composite
def datasets(draw: st.DrawFn, schema: Schema) -> Dataset:
    """A dataset where every FK points at a generated parent and unique columns are unique."""
    ds: Dataset = {}
    for table in schema:
        fks = [c for c in table.columns if c.references]
        if any(not c.nullable and not ds[c.references[0]] for c in fks if c.references):
            ds[table.name] = []  # no parent rows exist, so no valid child row can
            continue
        rows = draw(_rows(table))
        # Uniqueness by dropping later duplicates rather than `unique_by`: the rejection
        # sampling behind unique_by left the shrinker stuck on non-minimal lists.
        for c in table.columns:
            if c.unique:
                rows = list({r[c.name]: r for r in reversed(rows)}.values())[::-1]
        for c in fks:
            assert c.references
            parents = [r[c.references[1]] for r in ds[c.references[0]]]
            for r in rows:
                if r[c.name] is not None:
                    r[c.name] = parents[r[c.name] % len(parents)] if parents else None
        ds[table.name] = rows
    return ds
