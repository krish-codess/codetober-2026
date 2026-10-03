"""The properties as plain pytest + Hypothesis tests: `pytest` alone runs the suite on a
fixed example stream and prints Hypothesis's own shrunk falsifying example. Known bugs
are strict xfails, so fixing the pipeline turns this file red until the baseline is
updated."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, given, settings

from tydlc.engine import Engine
from tydlc.generators import datasets
from tydlc.properties import Ctx, Property
from tydlc.runner import evaluate
from tydlc.schema import Dataset
from tydlc.subjects.jaffle import METHODS, PROPERTIES, SCHEMA, SUBJECT

KNOWN = SUBJECT.known_bugs["duckdb"]
BY_NAME = {p.name: p for p in PROPERTIES}
Row = dict[str, Any]


@pytest.mark.parametrize("prop", [
    pytest.param(p, id=p.name,
                 marks=[pytest.mark.xfail(strict=True, reason=KNOWN[p.name])]
                 if p.name in KNOWN else [])
    for p in PROPERTIES])
def test_property_holds_on_generated_data(prop: Property, duck: Engine) -> None:
    # A known bug stops at its first counterexample, so a large budget is cheap there
    # and keeps the strict xfail from flaking on the rarer bugs.
    budget = 500 if prop.name in KNOWN else 60

    @settings(max_examples=budget, derandomize=True, database=None, deadline=None,
              suppress_health_check=list(HealthCheck), phases=[Phase.generate, Phase.shrink])
    @given(datasets(SCHEMA))
    def check(ds: Dataset) -> None:
        assert evaluate(duck, [prop], ds)[prop.name] is not False

    check()


# --- the property logic itself, against hand-built pipeline outputs ---------------------

def _order(order_id: int, amount: float | None, customer_id: int = 1, **methods: float) -> Row:
    row: Row = {"order_id": order_id, "customer_id": customer_id, "order_date": None,
                "status": "placed", "amount": amount}
    row.update({f"{m}_amount": methods.get(m, None if amount is None else 0) for m in METHODS})
    return row


def _customer(customer_id: int, **kw: object) -> Row:
    return {"customer_id": customer_id, "first_name": None, "last_name": None,
            "first_order": None, "most_recent_order": None, "number_of_orders": None,
            "customer_lifetime_value": None, **kw}


def _ctx(raw: Dataset, orders: list[Row], customers: list[Row]) -> Ctx:
    full = {"raw_customers": [], "raw_orders": [], "raw_payments": [], **raw}
    return Ctx(full, {"orders": orders, "customers": customers}, lambda ds: {})


def _check(name: str, ctx: Ctx) -> bool | None:
    return BY_NAME[name].check(ctx)


def test_cents_conserved_catches_truncation() -> None:
    raw = {"raw_payments": [{"id": 1, "order_id": 1, "payment_method": "coupon", "amount": 150}]}
    assert _check("cents_conserved", _ctx(raw, [_order(1, 1.5)], [])) is True
    assert _check("cents_conserved", _ctx(raw, [_order(1, 1)], [])) is False  # 150 // 100
    assert _check("cents_conserved", _ctx({}, [_order(1, None)], [])) is None  # nothing paid


def test_amount_not_null() -> None:
    assert _check("orders_amount_not_null", _ctx({}, [_order(1, 2.0)], [])) is True
    assert _check("orders_amount_not_null", _ctx({}, [_order(1, None)], [])) is False
    assert _check("orders_amount_not_null", _ctx({}, [], [])) is None


def test_row_preservation_catches_fanout_and_drops() -> None:
    raw = {"raw_orders": [{"id": 1, "user_id": 1}, {"id": 2, "user_id": 1}]}
    name = "orders_one_row_per_raw_order"
    assert _check(name, _ctx(raw, [_order(1, 1.0), _order(2, 1.0)], [])) is True
    assert _check(name, _ctx(raw, [_order(1, 1.0), _order(1, 1.0), _order(2, 1.0)], [])) is False
    assert _check(name, _ctx(raw, [_order(1, 1.0)], [])) is False


def test_methods_sum_to_amount() -> None:
    name = "methods_sum_to_amount"
    assert _check(name, _ctx({}, [_order(1, 3.0, coupon=1.0, gift_card=2.0)], [])) is True
    assert _check(name, _ctx({}, [_order(1, 3.0, coupon=1.0)], [])) is False


def test_ltv_and_order_count() -> None:
    raw = {"raw_orders": [{"id": 1, "user_id": 7}, {"id": 2, "user_id": 7}]}
    orders = [_order(1, 2.0, customer_id=7), _order(2, 3.5, customer_id=7)]
    good = [_customer(7, customer_lifetime_value=5.5, number_of_orders=2), _customer(8)]
    assert _check("ltv_is_sum_of_orders", _ctx(raw, orders, good)) is True
    assert _check("order_count_matches_raw", _ctx(raw, orders, good)) is True
    bad = [_customer(7, customer_lifetime_value=2.0, number_of_orders=1)]
    assert _check("ltv_is_sum_of_orders", _ctx(raw, orders, bad)) is False
    assert _check("order_count_matches_raw", _ctx(raw, orders, bad)) is False


def test_first_order_before_last() -> None:
    early, late = dt.date(2020, 1, 1), dt.date(2021, 1, 1)
    name = "first_order_before_last"
    assert _check(name, _ctx({}, [], [_customer(1, first_order=early, most_recent_order=late)]))
    assert _check(name, _ctx({}, [], [
        _customer(1, first_order=late, most_recent_order=early)])) is False
    assert _check(name, _ctx({}, [], [_customer(1)])) is None
