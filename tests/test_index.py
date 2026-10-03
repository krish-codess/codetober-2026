"""Index construction: differential test against a naive reference, closed-form cases, chain linking."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from goldstandard import index
from goldstandard.index import ALL, MIN_COVERAGE, cap_weights

D = date(2026, 1, 5)


def _basket(rows):
    return pl.DataFrame(
        rows,
        schema={
            "server_id": pl.String,
            "item_id": pl.Int64,
            "division_id": pl.String,
            "weight": pl.Float64,
            "base_price": pl.Float64,
        },
        orient="row",
    )


def _prices(rows, day=D):
    return pl.DataFrame(
        [(s, i, day, p) for s, i, p in rows],
        schema={"server_id": pl.String, "item_id": pl.Int64, "day": pl.Date, "price": pl.Float64},
        orient="row",
    )


def reference_index(basket, prices, link, scope, division):
    """Deliberately naive loops: the definition from docs/METHODOLOGY.md, nothing clever."""
    groups = {}
    for s, i, d, w, p0 in basket:
        if (scope != ALL and s != scope) or (division != ALL and d != division):
            continue
        g = groups.setdefault((s, d), {"W": 0.0, "O": 0.0, "WR": 0.0})
        g["W"] += w
        if (s, i) in prices:
            g["O"] += w
            g["WR"] += w * prices[(s, i)] / p0
    if not groups:
        return None, 0.0
    num = sum(g["W"] * g["WR"] / g["O"] for g in groups.values() if g["O"] > 0)
    den = sum(g["W"] for g in groups.values() if g["O"] > 0)
    cov = sum(g["O"] for g in groups.values()) / sum(g["W"] for g in groups.values())
    if den == 0 or cov < MIN_COVERAGE:
        return None, cov
    return link * num / den, cov


cells = st.lists(
    st.tuples(
        st.sampled_from(["a", "b"]),
        st.integers(1, 12),
        st.sampled_from(["x", "y", "z"]),
        st.floats(0.001, 1.0),
        st.floats(0.01, 1e5),
        st.one_of(st.none(), st.floats(0.5, 2.0)),
    ),
    min_size=1,
    max_size=25,
    unique_by=lambda c: (c[0], c[1]),
)


@settings(max_examples=150, deadline=None)
@given(cells=cells, link=st.floats(1.0, 500.0))
def test_index_matches_naive_reference_implementation(cells, link):
    basket = [(s, i, d, w, p0) for s, i, d, w, p0, _ in cells]
    prices = {(s, i): p0 * r for s, i, d, w, p0, r in cells if r is not None}
    servers, divisions = ["a", "b"], ["x", "y", "z"]
    links = {(sc, dv): link for sc in [*servers, ALL] for dv in [*divisions, ALL]}
    got = index.index_values(
        _basket(basket), _prices([(s, i, p) for (s, i), p in prices.items()]), links, servers, divisions
    )
    for sc in [*servers, ALL]:
        for dv in [*divisions, ALL]:
            want, cov = reference_index(basket, prices, link, sc, dv)
            row = got.filter((pl.col("scope") == sc) & (pl.col("division") == dv))
            if row.is_empty():
                assert want is None and cov == 0.0
                continue
            v = row["value"][0]
            if want is None:
                assert v is None
            else:
                assert math.isclose(v, round(want, 6), rel_tol=1e-9, abs_tol=1e-6)
                assert math.isclose(row["coverage"][0], cov, rel_tol=1e-9, abs_tol=1e-12)


def test_unchanged_prices_give_exactly_the_link_value():
    b = _basket([("a", 1, "x", 0.6, 10.0), ("a", 2, "y", 0.4, 5.0)])
    out = index.index_values(
        b, _prices([("a", 1, 10.0), ("a", 2, 5.0)]), {("a", ALL): 100.0, (ALL, ALL): 100.0}, ["a"], ["x", "y"]
    )
    assert out.filter(pl.col("division") == ALL)["value"].to_list() == [100.0, 100.0]


def test_one_item_doubling_moves_index_by_its_weight():
    b = _basket([("a", 1, "x", 0.25, 10.0), ("a", 2, "x", 0.75, 5.0)])
    out = index.index_values(b, _prices([("a", 1, 20.0), ("a", 2, 5.0)]), {("a", ALL): 100.0}, ["a"], ["x"])
    assert out.filter((pl.col("scope") == "a") & (pl.col("division") == ALL))["value"][0] == 125.0


def test_missing_item_is_imputed_from_its_own_group_not_given_a_price():
    # item 2 (group y) is unobserved; group x doubled. A fabricated "last price" for item 2 would give 150;
    # carrying group y's weight on its own (absent) relative is impossible, so y drops out and x speaks.
    b = _basket([("a", 1, "x", 0.5, 10.0), ("a", 2, "y", 0.5, 5.0)])
    out = index.index_values(b, _prices([("a", 1, 20.0)]), {("a", ALL): 100.0}, ["a"], ["x", "y"])
    row = out.filter((pl.col("scope") == "a") & (pl.col("division") == ALL)).row(0, named=True)
    assert row["coverage"] == 0.5 and row["status"] == "partial" and row["value"] == 200.0


def test_low_coverage_publishes_no_value():
    b = _basket([("a", 1, "x", 0.3, 10.0), ("a", 2, "x", 0.7, 5.0)])
    out = index.index_values(b, _prices([("a", 1, 11.0)]), {("a", ALL): 100.0}, ["a"], ["x"])
    row = out.filter((pl.col("scope") == "a") & (pl.col("division") == ALL)).row(0, named=True)
    assert row["status"] == "insufficient" and row["value"] is None


def test_missing_link_publishes_no_value():
    b = _basket([("a", 1, "x", 1.0, 10.0)])
    out = index.index_values(b, _prices([("a", 1, 11.0)]), {}, ["a"], ["x"])
    assert out["value"].null_count() == out.height


def test_chain_link_is_continuous_at_the_link_point():
    old = _basket([("a", 1, "x", 0.5, 10.0), ("a", 2, "x", 0.5, 20.0)])
    link_prices = pl.DataFrame({"server_id": ["a", "a"], "item_id": [1, 2], "price": [15.0, 20.0]})
    j = index.link_factors(old, link_prices, ["a"], ["x"])
    l_new = 100.0 * j[("a", ALL)]
    assert l_new == 125.0
    # new basket re-weighted, base prices = link prices: at the link prices it must read exactly L_new
    new = _basket([("a", 1, "x", 0.8, 15.0), ("a", 2, "x", 0.2, 20.0)])
    out = index.index_values(new, _prices([("a", 1, 15.0), ("a", 2, 20.0)]), {("a", ALL): l_new}, ["a"], ["x"])
    assert out.filter((pl.col("scope") == "a") & (pl.col("division") == ALL))["value"][0] == 125.0


@given(w=st.lists(st.floats(0.001, 1e9), min_size=1, max_size=40))
def test_weight_cap_properties(w):
    c = cap_weights(np.array(w))
    assert math.isclose(c.sum(), 1.0, rel_tol=1e-9)
    if len(w) * index.WEIGHT_CAP > 1:
        assert c.max() <= index.WEIGHT_CAP + 1e-9
    # capping never reverses the order of two weights
    order = np.argsort(w, kind="stable")
    assert all(c[order[k]] <= c[order[k + 1]] + 1e-12 for k in range(len(w) - 1))


def test_basket_eligibility_weights_and_base_price():
    p = index.Period(
        "w",
        "2026Q1",
        date(2026, 1, 1),
        date(2026, 3, 31),
        date(2025, 10, 1),
        date(2025, 12, 31),
        date(2025, 12, 25),
        date(2025, 12, 31),
    )
    days = [p.ref_from + timedelta(days=k) for k in range(92)]
    rows = []
    for k, d in enumerate(days):
        rows.append(("a", 1, d, 10.0 + (k == 91) * 1000, 100.0))  # last day spike must not set the base price
        rows.append(("a", 2, d, 5.0, 100.0))
        if k % 3 == 0:
            rows.append(("a", 3, d, 7.0, 100.0))  # traded on 1/3 of days: ineligible (< 50%)
    daily = pl.DataFrame(
        rows,
        schema={"server_id": pl.String, "item_id": pl.Int64, "day": pl.Date, "price": pl.Float64, "volume": pl.Float64},
        orient="row",
    )
    b = index.build_basket(p, daily, {1: "x", 2: "x", 3: "y"})
    assert sorted(b["item_id"].to_list()) == [1, 2]
    assert b.filter(pl.col("item_id") == 1)["base_price"][0] == 10.0  # median of the link window
    assert math.isclose(b["weight"].sum(), 1.0)


def test_quarter_periods_require_a_full_enough_reference_period():
    ps = index.quarter_periods("w", date(2025, 9, 15), date(2026, 4, 2))
    assert [p.period_id for p in ps] == ["2026Q1", "2026Q2"]  # Q4 2025 would have only 16 reference days
    assert ps[0].ref_from == date(2025, 10, 1) and ps[0].link_from == date(2025, 12, 25)
