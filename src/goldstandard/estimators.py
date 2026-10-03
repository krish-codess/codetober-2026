"""Robust price estimation. The guarantees here are what the whole index rests on.

Snapshot path (auction listings):
    per (server, item, snapshot): the order statistic of rank k = max(1, floor((n-1)/4)) of the
    sell-listing unit prices (an *interior* lower quartile), one listing = one vote, published only
    if at least MIN_LISTINGS listings exist. Per day: the median of that day's snapshot estimates.

    Why a lower quartile and not the median: manipulation is asymmetric. Absurd asks above the
    market cost nothing to leave listed and accumulate (the real Jita book carries a 300M ISK
    tritanium ask); asks far below the market are bought within minutes. In thin books the
    persistent high-side trolls can become half the book and break a median; this rank needs
    more than ~75% of the book to be high-side trolls. Because k >= 1, the cheapest listing is
    never the estimate, so a single low bait listing cannot set the price either. It is also
    closer to the price a buyer actually pays than the median ask (better labour-hour levels).

    Guarantee (bounded influence, proven by tests/test_estimators.py with Hypothesis):
    adding ONE listing at any price to a book of n >= MIN_LISTINGS moves the estimate no further
    than the neighbouring order statistics of the original book: it stays inside [x_(k-1), x_(k+1)].
    Re-pricing one listing stays inside [x_(k-2), x_(k+2)]. Quantity is deliberately ignored: a
    single actor's volume must not buy a larger vote.

    Second layer: the daily series passes through the same causal Hampel filter as the history
    path, so a book captured wholesale by trolls (more than one listing) still cannot publish.

History path (daily trade aggregates, no listing detail):
    the daily volume-weighted average, passed through a CAUSAL Hampel filter: rejected when
    its log-distance from the median of the previous HAMPEL_WINDOW observations exceeds
    max(HAMPEL_K * scale, HAMPEL_FLOOR), scale = 1.4826 * median |daily log change| over the same
    window. Causal (trailing) on purpose: a centred window
    would let tomorrow's data rewrite today's decision, i.e. silently revise history.

Neither path ever fills a gap: no listings / no trades -> no price (status thin / missing).
"""

from __future__ import annotations

import math
from datetime import date

import polars as pl

MIN_LISTINGS = 3
EXTREME_Z = 5.0  # robust z-score (log space) above which a listing is flagged as extreme
MAD_FLOOR = 0.02  # log-units; stops a perfectly flat book from flagging every tiny deviation
HAMPEL_WINDOW = 14
HAMPEL_K = 5.0
HAMPEL_FLOOR = math.log(4.0)  # never reject a move smaller than 4x: real patch shocks must survive
HAMPEL_MIN_HISTORY = 5


def ask_rank(n: int) -> int:
    """0-based rank of the published order statistic in a book of n listings (interior: never 0)."""
    return max(1, (n - 1) // 4)


def ask_price(prices: list[float]) -> float | None:
    """Reference implementation of the snapshot estimator (used by the differential tests)."""
    if len(prices) < MIN_LISTINGS:
        return None
    return sorted(prices)[ask_rank(len(prices))]


def snapshot_prices(listings: pl.DataFrame) -> pl.DataFrame:
    """listings -> one row per (server, item, snapshot) with the robust price and diagnostics.

    Columns: server_id, item_id, snapshot_ts, n_sell, price (null if thin), naive_mean,
    n_extreme, max_z (robust z of the most extreme listing), min_price, max_price.
    """
    sells = listings.filter(~pl.col("is_buy")).with_columns(lp=pl.col("price").log())
    keys = ["server_id", "item_id", "snapshot_ts"]
    stats = (
        sells.with_columns(
            med_lp=pl.col("lp").median().over(keys),
        )
        .with_columns(
            mad=pl.max_horizontal(
                (pl.col("lp") - pl.col("med_lp")).abs().median().over(keys) * 1.4826, pl.lit(MAD_FLOOR)
            )
        )
        .with_columns(z=((pl.col("lp") - pl.col("med_lp")).abs() / pl.col("mad")))
    )
    out = stats.group_by(keys).agg(
        n_sell=pl.len().cast(pl.Int32),
        price=pl.col("price").sort().get(pl.max_horizontal(pl.lit(1), (pl.len() - 1) // 4).clip(0, pl.len() - 1)),
        naive_mean=pl.col("price").mean(),
        n_extreme=(pl.col("z") > EXTREME_Z).sum().cast(pl.Int32),
        max_z=pl.col("z").max(),
        min_price=pl.col("price").min(),
        max_price=pl.col("price").max(),
    )
    return out.with_columns(price=pl.when(pl.col("n_sell") >= MIN_LISTINGS).then(pl.col("price")).otherwise(None)).sort(
        keys
    )


def daily_from_snapshots(snaps: pl.DataFrame, expected_items: pl.DataFrame, day: date) -> pl.DataFrame:
    """Snapshot estimates for one day -> one row per expected (server, item).

    status: ok      at least one snapshot had a robust price; price = median over snapshots
            thin    listings were seen but never MIN_LISTINGS at once; price null
            missing nothing observed (no listings, or every fetch for it failed); price null
    `expected_items` (server_id, item_id) makes absence explicit instead of silently missing rows.
    """
    agg = snaps.group_by("server_id", "item_id").agg(
        price=pl.col("price").drop_nulls().median(),
        n_obs=pl.col("price").is_not_null().sum().cast(pl.Int32),
        n_snapshots=pl.len().cast(pl.Int32),
        max_listings=pl.col("n_sell").max(),
        n_extreme=pl.col("n_extreme").sum(),
        max_z=pl.col("max_z").max(),
        naive_mean=pl.col("naive_mean").median(),
    )
    out = expected_items.join(agg, on=["server_id", "item_id"], how="left").with_columns(
        day=pl.lit(day),
        n_obs=pl.col("n_obs").fill_null(0),
        status=pl.when(pl.col("price").is_not_null())
        .then(pl.lit("ok"))
        .when(pl.col("max_listings").fill_null(0) > 0)
        .then(pl.lit("thin"))
        .otherwise(pl.lit("missing")),
    )
    return out.sort("server_id", "item_id")


def hampel_daily(history: pl.DataFrame) -> pl.DataFrame:
    """history rows (server_id, item_id, day, average, volume, order_count, ...) sorted by day ->
    adds trailing-median diagnostics and status ok/rejected. Rows absent from history stay absent;
    callers add 'missing' rows for days without trades."""
    h = history.sort("server_id", "item_id", "day").with_columns(lp=pl.col("average").log())
    grp = ["server_id", "item_id"]
    # reference level: median of the previous HAMPEL_WINDOW observations (strictly the past)
    # scale: median absolute day-over-day log change over the same past window (robust volatility)
    h = h.with_columns(
        ref=pl.col("lp").shift(1).rolling_median(HAMPEL_WINDOW, min_samples=HAMPEL_MIN_HISTORY).over(grp),
        scale=pl.col("lp").diff().abs().shift(1).rolling_median(HAMPEL_WINDOW, min_samples=HAMPEL_MIN_HISTORY).over(grp)
        * 1.4826,
    )
    h = h.with_columns(
        dev=(pl.col("lp") - pl.col("ref")).abs(),
        limit=pl.max_horizontal(pl.col("scale") * HAMPEL_K, pl.lit(HAMPEL_FLOOR)),
    )
    return h.with_columns(
        status=pl.when(pl.col("dev") > pl.col("limit")).then(pl.lit("rejected")).otherwise(pl.lit("ok")),
        robust_z=pl.col("dev") / pl.max_horizontal(pl.col("scale"), pl.lit(MAD_FLOOR)),
    )
