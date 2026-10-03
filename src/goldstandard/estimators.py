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

    Second layer: robust_daily() below. In the thinnest servers a book can be one honest ask plus two
    or three trolls - no single-book estimator can tell which is real, but the other servers can.

History path (daily trade aggregates, no listing detail):
    the daily volume-weighted average.

Both paths then pass robust_daily(): cross-server consensus where the item trades on enough
servers, a causal Hampel check against the cell's own accepted history where it does not.
Causal (trailing) on purpose: a centred window would let tomorrow's data rewrite today's decision,
i.e. silently revise history.

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
CROSS_MIN_SERVERS = 3
CROSS_LIMIT = math.log(4.0)  # a server more than 4x away from the cross-server median is not a market price
UNTRADED_LIMIT = math.log(1.5)  # without a single trade that day, even a 1.5x deviation is not trusted


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


def robust_daily(today: pl.DataFrame, trailing: pl.DataFrame) -> pl.DataFrame:
    """Day-level acceptance of raw daily prices (both worlds). Deterministic and causal.

    today:    server_id, item_id, raw_price (null = nothing observed), ...
    trailing: server_id, item_id, day, price  -- ACCEPTED prices of earlier days only

    1. Cross-server consensus. The same good cannot trade 4x apart across servers for long
       (hauling arbitrages it), so when >= CROSS_MIN_SERVERS servers *traded* the item today, a
       price more than CROSS_LIMIT (log) from the median of those traded prices is rejected - and a
       price that agrees with consensus is accepted, which is what prevents lock-out after a genuine
       shift. Only traded observations vote: two cornered books cannot outvote one honest market.
    2. Otherwise (item not traded widely enough today): causal Hampel against this cell's own
       last HAMPEL_WINDOW accepted prices - rejected when the log distance from their median
       exceeds max(HAMPEL_K * scale, HAMPEL_FLOOR), scale = 1.4826 * median |log change|. Using
       accepted prices only means a captured stretch cannot drag its own reference along.

    3. A price not corroborated by trades that day (no volume, or the day's traded VWAP more than
       UNTRADED_LIMIT away from it) that also sits more than UNTRADED_LIMIT from its reference
       (consensus, else own accepted history) is rejected. A cornered book - one actor buys every ask
       and relists at 3x - shows exactly this: a high ask nobody pays. History rows (real EVE) are
       trade averages, always corroborated, so this rule never fires there.

    Adds: price (accepted, else null), rejected (bool), reject_reason, cross_ref, hampel_ref, robust_z.
    """
    t = today.with_columns(lp=pl.col("raw_price").log())
    # consensus is formed only by prices that actually traded: a cornered book (asks, no buyers) gets no vote,
    # so two cornered servers cannot outvote one honest market
    traded = _corroborated(today)
    cross = (
        t.filter(pl.col("lp").is_not_null() & traded)
        .group_by("item_id")
        .agg(cross_lp=pl.col("lp").median(), cross_n=pl.len())
    )
    if trailing.is_empty():
        hist = pl.DataFrame(schema={"server_id": pl.String, "item_id": pl.Int64, "hist": pl.List(pl.Float64)})
    else:
        hist = (
            trailing.drop_nulls("price")
            .sort("day")
            .group_by("server_id", "item_id", maintain_order=True)
            .agg(hist=pl.col("price").log().tail(HAMPEL_WINDOW))
        )
    j = (
        t.join(cross, on="item_id", how="left")
        .join(hist, on=["server_id", "item_id"], how="left")
        .with_columns(
            n_hist=pl.col("hist").list.len().fill_null(0),
            ref=pl.col("hist").list.median(),
            scale=pl.col("hist").list.diff(null_behavior="drop").list.eval(pl.element().abs()).list.median() * 1.4826,
        )
    )
    has_cross = pl.col("cross_n").fill_null(0) >= CROSS_MIN_SERVERS
    cross_dev = (pl.col("lp") - pl.col("cross_lp")).abs()
    time_dev = (pl.col("lp") - pl.col("ref")).abs()
    limit = pl.max_horizontal(pl.col("scale").fill_null(0) * HAMPEL_K, pl.lit(HAMPEL_FLOOR))
    cross_reject = has_cross & (cross_dev > CROSS_LIMIT)
    time_reject = ~has_cross & (pl.col("n_hist") >= HAMPEL_MIN_HISTORY) & (time_dev > limit)
    # a price nobody paid: no trades that day and far from its reference (consensus, else own history)
    untraded = ~_corroborated(today)
    ref_dev = (
        pl.when(has_cross).then(cross_dev).otherwise(pl.when(pl.col("n_hist") >= HAMPEL_MIN_HISTORY).then(time_dev))
    )
    untraded_reject = untraded & (ref_dev > UNTRADED_LIMIT)
    rejected = pl.col("lp").is_not_null() & (cross_reject | time_reject | untraded_reject).fill_null(False)
    return j.with_columns(
        rejected=rejected,
        reject_reason=pl.when(rejected & cross_reject.fill_null(False))
        .then(pl.lit("cross_server"))
        .when(rejected & time_reject.fill_null(False))
        .then(pl.lit("temporal"))
        .when(rejected)
        .then(pl.lit("untraded_outlier")),
        price=pl.when(rejected).then(None).otherwise(pl.col("raw_price")),
        cross_ref=pl.col("cross_lp").exp(),
        hampel_ref=pl.col("ref").exp(),
        robust_z=pl.when(has_cross)
        .then(cross_dev / CROSS_LIMIT)
        .otherwise(time_dev / pl.max_horizontal(pl.col("scale"), pl.lit(MAD_FLOOR))),
    ).drop("lp", "cross_lp", "hist", "ref", "scale", "n_hist", "cross_n")


def _corroborated(today: pl.DataFrame) -> pl.Expr:
    """Was this price actually paid? Volume > 0 AND the day's traded VWAP within UNTRADED_LIMIT of the
    published (ask-based) price. A corner that ends mid-day has trades - at the honest price, not the
    cornered one - so volume alone is not evidence. Frames without trade data count as corroborated."""
    if "volume" not in today.columns:
        return pl.lit(True)
    expr = pl.col("volume").fill_null(0) > 0
    if "trade_vwap" in today.columns:
        expr = expr & ((pl.col("raw_price").log() - pl.col("trade_vwap").log()).abs() <= UNTRADED_LIMIT).fill_null(
            False
        )
    return expr


def robust_series(raw: pl.DataFrame) -> pl.DataFrame:
    """Reference driver: apply robust_daily day by day over (server_id, item_id, day, raw_price),
    feeding each day's accepted prices forward. The pipeline does exactly this, one partition at a time."""
    out: list[pl.DataFrame] = []
    accepted = pl.DataFrame(schema={"server_id": pl.String, "item_id": pl.Int64, "day": pl.Date, "price": pl.Float64})
    for d in sorted(raw["day"].unique().to_list()):
        today = robust_daily(raw.filter(pl.col("day") == d), accepted)
        out.append(today)
        accepted = pl.concat([accepted, today.select("server_id", "item_id", "day", "price")], how="vertical_relaxed")
    return pl.concat(out, how="vertical_relaxed").sort("server_id", "item_id", "day")
