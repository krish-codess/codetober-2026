from __future__ import annotations

from pathlib import Path

from tydlc.discovery import discover, templates
from tydlc.engine import Engine
from tydlc.properties import Ctx
from tydlc.runner import candidates
from tydlc.schema import Dataset
from tydlc.subjects import Subject


def _ctx(out: Dataset, raw: Dataset | None = None) -> Ctx:
    return Ctx(raw or {}, out, lambda ds: {})


def _named(tables: Dataset, models: list[str]) -> dict[str, object]:
    return {p.name: p for p in templates(tables, models)}


GOOD: Dataset = {
    "raw": [{"id": 1}, {"id": 2}],
    "m": [{"id": 1, "lo": 1, "hi": 5, "tag": "a"}, {"id": 2, "lo": 2, "hi": 2, "tag": None}],
}


def test_templates_are_typed() -> None:
    names = set(_named(GOOD, ["m"]))
    assert {"not_null(m.id)", "unique(m.id)", "non_negative(m.lo)", "le(m.lo,m.hi)",
            "le(m.hi,m.lo)", "subset(m.id,raw.id)", "row_count_eq(m,raw)"} <= names
    assert "non_negative(m.tag)" not in names  # strings are not ordered numerics
    assert "le(m.lo,m.tag)" not in names
    assert not any(n.startswith("not_null(raw.") for n in names)  # inputs are not outputs


def test_discover_keeps_only_what_the_observation_supports() -> None:
    kept = {p.name for p in discover([_ctx(GOOD)], ["m"])}
    assert {"not_null(m.id)", "unique(m.id)", "le(m.lo,m.hi)", "row_count_eq(m,raw)"} <= kept
    assert "not_null(m.tag)" not in kept  # violated
    assert "le(m.hi,m.lo)" not in kept    # violated
    assert "unique(m.tag)" not in kept    # one value: vacuous, no evidence


def test_checks_report_violation_and_vacuity() -> None:
    props = {p.name: p for p in discover([_ctx(GOOD)], ["m"])}
    empty = _ctx({"raw": [], "m": []})
    assert props["not_null(m.id)"].check(empty) is None
    assert props["row_count_eq(m,raw)"].check(empty) is None
    broken = _ctx({"raw": [{"id": 1}],
                   "m": [{"id": None, "lo": 9, "hi": 1, "tag": "a"},
                         {"id": 7, "lo": 1, "hi": 1, "tag": "a"}]})
    assert props["not_null(m.id)"].check(broken) is False
    assert props["le(m.lo,m.hi)"].check(broken) is False
    assert props["subset(m.id,raw.id)"].check(broken) is False
    assert props["row_count_eq(m,raw)"].check(broken) is False


def test_candidates_from_the_real_seed_include_the_declared_contract(
        subject: Subject, duck: Engine, tmp_path: Path) -> None:
    """Discovery, given only the seed data, re-derives the dbt tests jaffle declares,
    plus the conservation law nobody wrote down."""
    found = {p.name for p in candidates(subject, duck, tmp_path)}
    assert {"unique(customers.customer_id)", "not_null(customers.customer_id)",
            "unique(orders.order_id)", "not_null(orders.amount)",
            "subset(orders.customer_id,customers.customer_id)",
            "row_count_eq(orders,raw_orders)",
            "sum_eq(orders.amount,stg_payments.amount)",
            "le(customers.first_order,customers.most_recent_order)"} <= found
    # 38 of the seed's 100 customers never ordered, so this is correctly not a candidate.
    assert "not_null(customers.number_of_orders)" not in found
