"""Robust estimator guarantees, proven with property-based tests (Hypothesis)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from goldstandard import estimators
from goldstandard.estimators import MIN_LISTINGS, ask_price, ask_rank

honest = st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
any_price = st.floats(min_value=0.01, max_value=1e13, allow_nan=False, allow_infinity=False)
books = st.lists(honest, min_size=MIN_LISTINGS, max_size=200)


def _bounds(xs: list[float], steps: int) -> tuple[float, float]:
    s = sorted(xs)
    k = ask_rank(len(s))
    return s[max(0, k - steps)], s[min(len(s) - 1, k + steps)]


@given(book=books, troll=any_price)
def test_one_added_listing_cannot_move_estimate_past_neighbouring_order_statistics(book, troll):
    lo, hi = _bounds(book, 1)
    est = ask_price([*book, troll])
    assert est is not None and lo <= est <= hi


@given(book=st.lists(honest, min_size=MIN_LISTINGS + 1, max_size=200), troll=any_price, idx=st.integers(min_value=0))
def test_one_repriced_listing_is_bounded_too(book, troll, idx):
    i = idx % len(book)
    manipulated = [*book[:i], troll, *book[i + 1 :]]
    lo, hi = _bounds(book, 2)
    est = ask_price(manipulated)
    assert est is not None and lo <= est <= hi


@given(book=st.lists(honest, max_size=MIN_LISTINGS - 1))
def test_thin_books_publish_no_price(book):
    assert ask_price(book) is None


@settings(max_examples=60, deadline=None)
@given(
    data=st.lists(
        st.tuples(st.integers(0, 3), st.integers(1, 3), books), min_size=1, max_size=6, unique_by=lambda t: (t[0], t[1])
    )
)
def test_polars_estimator_matches_reference_implementation(data):
    """Differential test: the vectorised estimator == the 10-line reference, for every book."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for snap, item, book in data:
        for k, p in enumerate(book):
            rows.append(
                {
                    "server_id": "s",
                    "item_id": item,
                    "snapshot_ts": t0 + timedelta(hours=6 * snap),
                    "is_buy": False,
                    "price": p,
                    "order_id": k,
                }
            )
    out = estimators.snapshot_prices(pl.DataFrame(rows))
    for snap, item, book in data:
        got = out.filter((pl.col("item_id") == item) & (pl.col("snapshot_ts") == t0 + timedelta(hours=6 * snap)))
        assert got.height == 1
        assert math.isclose(got["price"][0], ask_price(book), rel_tol=1e-12)


def test_buy_orders_never_enter_the_ask_estimate():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    df = pl.DataFrame(
        {
            "server_id": ["s"] * 5,
            "item_id": [1] * 5,
            "snapshot_ts": [t] * 5,
            "is_buy": [False, False, False, True, True],
            "price": [10.0, 11.0, 12.0, 0.01, 0.02],
        }
    )
    assert estimators.snapshot_prices(df)["price"][0] == 11.0


@given(book=books, bait=st.floats(min_value=0.0001, max_value=1.0))
def test_a_single_bait_listing_is_never_the_estimate(book, bait):
    assert ask_price([*book, bait]) != bait or bait in book


@given(honest_book=st.lists(honest, min_size=4, max_size=60), n_trolls_share=st.floats(0, 0.74))
def test_estimate_survives_high_side_trolls_up_to_three_quarters_of_the_book(honest_book, n_trolls_share):
    n_trolls = int(len(honest_book) * n_trolls_share / (1 - n_trolls_share))
    est = ask_price(honest_book + [1e12] * n_trolls)
    assert est is not None and est <= max(honest_book)


def test_extreme_listing_is_flagged_but_does_not_move_price():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [4.0, 4.05, 4.1, 4.08, 4.02, 300_100_000.0]  # the real Jita tritanium troll listing, scaled
    df = pl.DataFrame(
        {"server_id": ["s"] * 6, "item_id": [34] * 6, "snapshot_ts": [t] * 6, "is_buy": [False] * 6, "price": prices}
    )
    row = estimators.snapshot_prices(df).row(0, named=True)
    assert row["n_extreme"] == 1
    assert 4.0 <= row["price"] <= 4.1
    assert row["naive_mean"] > 1e7  # what a mean would have published


def test_daily_status_never_fabricates_a_price():
    t = datetime(2026, 1, 1, 6, tzinfo=UTC)
    snaps = pl.DataFrame(
        {
            "server_id": ["s", "s"],
            "item_id": [1, 2],
            "snapshot_ts": [t, t],
            "n_sell": [5, 2],
            "price": [10.0, None],
            "naive_mean": [10.0, 9.0],
            "n_extreme": [0, 0],
            "max_z": [0.1, 0.1],
            "min_price": [9.0, 8.0],
            "max_price": [11.0, 10.0],
        }
    )
    expected = pl.DataFrame({"server_id": ["s", "s", "s"], "item_id": [1, 2, 3]})
    out = estimators.daily_from_snapshots(snaps, expected, date(2026, 1, 1))
    status = dict(zip(out["item_id"], out["status"], strict=True))
    prices = dict(zip(out["item_id"], out["price"], strict=True))
    assert status == {1: "ok", 2: "thin", 3: "missing"}
    assert prices[2] is None and prices[3] is None


# --------------------------------------------------------------------------------------- Hampel (history)
def _hist(prices: list[float], start: date = date(2026, 1, 1)) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "server_id": "s",
            "item_id": 1,
            "day": [start + timedelta(days=i) for i in range(len(prices))],
            "average": prices,
            "volume": 100,
            "order_count": 50,
        }
    )


def test_hampel_rejects_the_real_001_isk_trade_day():
    prices = [19900.0 * (1 + 0.01 * ((i * 7) % 5 - 2)) for i in range(30)]
    prices[20] = 0.01  # Oxygen Fuel Block, Heimatar, 2026-08-22
    out = estimators.hampel_daily(_hist(prices))
    assert out["status"][20] == "rejected"
    assert (out["status"] == "rejected").sum() == 1  # the rebound day is not rejected


def test_hampel_keeps_a_genuine_patch_crash():
    prices = [100.0] * 20 + [38.0] * 20  # services -62% overnight (synthetic crash patch)
    out = estimators.hampel_daily(_hist(prices))
    assert (out["status"] == "rejected").sum() == 0


@settings(max_examples=40, deadline=None)
@given(
    prices=st.lists(st.floats(min_value=0.01, max_value=1e6), min_size=10, max_size=60),
    extra=st.lists(st.floats(min_value=0.01, max_value=1e6), min_size=1, max_size=20),
)
def test_hampel_is_causal_future_days_never_change_past_decisions(prices, extra):
    before = estimators.hampel_daily(_hist(prices))
    after = estimators.hampel_daily(_hist(prices + extra)).head(len(prices))
    assert before["status"].to_list() == after["status"].to_list()
