"""Invariant discovery, Daikon style.

Instantiate a fixed set of templates over every model column, keep the ones that
hold on observed pipeline behaviour (the real seed data), and hand them to the
runner as ordinary properties. Adversarial generation then either falsifies a
candidate (with a shrunk counterexample) or raises its confidence.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from itertools import permutations
from typing import Any

from tydlc.properties import Check, Ctx, Property, close
from tydlc.schema import Dataset

Rows = list[dict[str, Any]]


def _tables(c: Ctx) -> Dataset:
    return {**c.raw, **c.out}


def _vals(rows: Rows, col: str) -> list[Any]:
    return [r[col] for r in rows if r[col] is not None]


def _kind(values: Iterable[Any]) -> str | None:
    v = next((x for x in values if x is not None), None)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "num"
    if isinstance(v, dt.date):
        return "date"
    return "str" if isinstance(v, str) else None


def _not_null(t: str, col: str) -> Check:
    def check(c: Ctx) -> bool | None:
        rows = _tables(c)[t]
        return all(r[col] is not None for r in rows) if rows else None
    return check


def _unique(t: str, col: str) -> Check:
    def check(c: Ctx) -> bool | None:
        vals = _vals(_tables(c)[t], col)
        return len(set(vals)) == len(vals) if len(vals) > 1 else None
    return check


def _non_negative(t: str, col: str) -> Check:
    def check(c: Ctx) -> bool | None:
        vals = _vals(_tables(c)[t], col)
        return all(v >= 0 for v in vals) if vals else None
    return check


def _le(t: str, a: str, b: str) -> Check:
    def check(c: Ctx) -> bool | None:
        pairs = [(r[a], r[b]) for r in _tables(c)[t] if r[a] is not None and r[b] is not None]
        return all(x <= y for x, y in pairs) if pairs else None
    return check


def _row_count_eq(t: str, u: str) -> Check:
    def check(c: Ctx) -> bool | None:
        tables = _tables(c)
        return len(tables[t]) == len(tables[u]) if tables[t] or tables[u] else None
    return check


def _subset(t: str, a: str, u: str, b: str) -> Check:
    def check(c: Ctx) -> bool | None:
        tables = _tables(c)
        vals = set(_vals(tables[t], a))
        return vals <= set(_vals(tables[u], b)) if vals else None
    return check


def _sum_eq(t: str, a: str, u: str, b: str) -> Check:
    def check(c: Ctx) -> bool | None:
        tables = _tables(c)
        x, y = _vals(tables[t], a), _vals(tables[u], b)
        return close(sum(x), sum(y)) if x or y else None
    return check


def templates(tables: Dataset, models: list[str]) -> Iterator[Property]:
    """Every template instance that is well-typed for these tables."""
    kinds = {(t, col): _kind(r[col] for r in rows)
             for t, rows in tables.items() if rows for col in rows[0]}

    def prop(name: str, description: str, check: Check) -> Property:
        return Property(name, description, check, source="discovered")

    for (t, col), kind in kinds.items():
        if t not in models:
            continue
        yield prop(f"not_null({t}.{col})", f"{t}.{col} is never NULL", _not_null(t, col))
        if kind is None:
            continue
        yield prop(f"unique({t}.{col})", f"{t}.{col} has no duplicates", _unique(t, col))
        if kind == "num":
            yield prop(f"non_negative({t}.{col})", f"{t}.{col} >= 0", _non_negative(t, col))
        # Same-named columns in other tables: candidate foreign keys and conserved totals.
        for (u, other), other_kind in kinds.items():
            if u != t and other == col and other_kind == kind:
                yield prop(f"subset({t}.{col},{u}.{col})",
                           f"every {t}.{col} appears in {u}.{col}", _subset(t, col, u, col))
                if kind == "num" and t < u:
                    yield prop(f"sum_eq({t}.{col},{u}.{col})",
                               f"sum of {t}.{col} equals sum of {u}.{col}",
                               _sum_eq(t, col, u, col))
    for m in models:
        cols = [(col, k) for (t, col), k in kinds.items() if t == m and k in ("num", "date")]
        for (a, ka), (b, kb) in permutations(cols, 2):
            if ka == kb:
                yield prop(f"le({m}.{a},{m}.{b})", f"{m}.{a} <= {m}.{b}", _le(m, a, b))
        for u in tables:
            if u != m and (u not in models or m < u):
                yield prop(f"row_count_eq({m},{u})",
                           f"{m} and {u} have the same number of rows", _row_count_eq(m, u))


def discover(observations: list[Ctx], models: list[str]) -> list[Property]:
    """Candidates: template instances never violated and at least once non-vacuously true."""
    kept = []
    for p in templates(_tables(observations[0]), models):
        results = [p.check(c) for c in observations]
        if False not in results and True in results:
            kept.append(p)
    return kept
