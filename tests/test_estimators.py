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


# --------------------------------------------------------------------------------------- day-level acceptance
def _raw(prices: dict[str, list[float | None]], start: date = date(2026, 1, 1)) -> pl.DataFrame:
    rows = [(srv, 1, start + timedelta(days=k), p) for srv, ps in prices.items() for k, p in enumerate(ps)]
    return pl.DataFrame(
        rows,
        schema={"server_id": pl.String, "item_id": pl.Int64, "day": pl.Date, "raw_price": pl.Float64},
        orient="row",
    ).with_columns(status=pl.lit("ok"))


def _status(out: pl.DataFrame, server: str) -> list[bool]:
    return out.filter(pl.col("server_id") == server).sort("day")["rejected"].to_list()


def _flat(n: int, level: float = 19900.0) -> list[float]:
    return [level * (1 + 0.01 * ((k * 7) % 5 - 2)) for k in range(n)]


def test_cross_server_consensus_rejects_the_real_001_isk_trade_day():
    heimatar = _flat(30)
    heimatar[20] = 0.01  # Oxygen Fuel Block, Heimatar, 2026-08-22
    out = estimators.robust_series(_raw({"heimatar": heimatar, "forge": _flat(30), "domain": _flat(30, 20500)}))
    assert _status(out, "heimatar") == [k == 20 for k in range(30)]
    assert out.filter((pl.col("server_id") == "heimatar") & pl.col("rejected"))["reject_reason"].to_list() == [
        "cross_server"
    ]


def test_temporal_fallback_rejects_when_no_consensus_exists():
    only = _flat(30)
    only[20] = 0.01
    out = estimators.robust_series(_raw({"lonely": only}))
    assert _status(out, "lonely") == [k == 20 for k in range(30)]


def test_a_genuine_patch_crash_survives_both_checks():
    crash = [100.0] * 20 + [38.0] * 20  # services -62% overnight on every server
    out = estimators.robust_series(_raw({"a": crash, "b": [x * 1.03 for x in crash], "c": [x * 0.97 for x in crash]}))
    assert not out["rejected"].any()
    alone = estimators.robust_series(_raw({"a": crash}))
    assert not alone["rejected"].any()


def test_a_captured_book_cannot_lock_out_honest_prices_later():
    """Trolls capture a thin server's book for 3 weeks (1000x). Consensus rejects every captured day,
    and because the trailing window only holds accepted prices, honest prices are accepted again after."""
    thin = [10.0] * 10 + [10_000.0] * 21 + [10.5] * 10
    out = estimators.robust_series(_raw({"thin": thin, "deep1": [10.0] * 41, "deep2": [10.2] * 41}))
    assert _status(out, "thin") == [10 <= k < 31 for k in range(41)]


@settings(max_examples=40, deadline=None)
@given(
    prices=st.lists(st.floats(min_value=0.01, max_value=1e6), min_size=10, max_size=40),
    extra=st.lists(st.floats(min_value=0.01, max_value=1e6), min_size=1, max_size=15),
)
def test_acceptance_is_causal_future_days_never_change_past_decisions(prices, extra):
    before = estimators.robust_series(_raw({"s": prices}))
    after = estimators.robust_series(_raw({"s": prices + extra})).head(len(prices))
    assert before["rejected"].to_list() == after["rejected"].to_list()
    assert before["price"].to_list() == after["price"].to_list()


def test_an_untraded_corner_is_rejected_but_a_traded_premium_is_kept():
    """A 3x ask sits inside the 4x consensus band. With no trades that day it is a corner (rejected);
    with real volume it is a genuine local premium (kept)."""
    base = {"a": [100.0] * 20, "b": [102.0] * 20, "c": [98.0] * 20}
    raw = _raw({**base, "c": [98.0] * 15 + [300.0] * 5})
    untraded = raw.with_columns(
        volume=pl.when((pl.col("server_id") == "c") & (pl.col("raw_price") == 300.0)).then(0.0).otherwise(50.0)
    )
    out = estimators.robust_series(untraded)
    assert _status(out, "c") == [k >= 15 for k in range(20)]
    assert set(out.filter(pl.col("rejected"))["reject_reason"]) == {"untraded_outlier"}
    traded = raw.with_columns(volume=pl.lit(50.0))
    assert not estimators.robust_series(traded)["rejected"].any()


def test_cornered_books_cannot_outvote_the_one_market_that_traded():
    """Two of three servers cornered at ~3x with no buyers; only the honest one traded. Consensus must not
    be the cornered price: the corners are rejected, the honest market is kept (a real fixture case)."""
    honest, corner = [20000.0] * 20, [17500.0] * 15 + [56600.0] * 5
    raw = _raw({"aurora": honest, "borealis": corner, "cinder": [18000.0] * 15 + [65800.0] * 5})
    raw = raw.with_columns(volume=pl.when(pl.col("raw_price") > 50000).then(0.0).otherwise(40.0))
    out = estimators.robust_series(raw)
    assert _status(out, "aurora") == [False] * 20
    assert _status(out, "borealis") == [k >= 15 for k in range(20)]
    assert _status(out, "cinder") == [k >= 15 for k in range(20)]


def test_a_corner_ending_mid_day_is_not_corroborated_by_the_honest_trades():
    """Volume alone is not evidence: when a corner ends mid-day the day has trades, but at the honest
    price. The 3x ask is rejected because the day's traded VWAP does not corroborate it."""
    raw = _raw({"a": [100.0] * 20, "b": [100.0] * 15 + [300.0] * 5}).with_columns(
        volume=pl.lit(40.0), trade_vwap=pl.lit(100.0)
    )
    out = estimators.robust_series(raw)
    assert _status(out, "b") == [k >= 15 for k in range(20)]
    assert _status(out, "a") == [False] * 20
