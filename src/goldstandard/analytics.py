"""Analytics on top of published index values and daily prices. All pure functions on Polars frames.

Shock detection (causal, so a published shock is never re-decided by future data):
    d_t = ln I_t - ln I_{t-1} (consecutive calendar days)
    z_t = (d_t - median(d over previous 60 days)) / (1.4826 * MAD(d over previous 60 days)), MAD floored
    shock if |z_t| >= SHOCK_Z and |d_t| >= SHOCK_MIN_MOVE; consecutive shock days in the same
    direction are merged into one event dated at its largest move.

Patch attribution for a shock on day t: every patch released in [t - ATTRIB_LOOKBACK_DAYS, end of t],
scored relevance = topic match * recency, topic match = 1 / rank of the series' division among the
patch's tags (or 0.5 for an untagged major release, 0.15 otherwise), recency = exp(-lag_days / 2).
A shock with no candidate is reported as UNATTRIBUTED - the system says "we do not know" rather
than blaming the nearest patch.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

INFLATION_WINDOWS = (7, 30, 90, 365)
SHOCK_WINDOW = 60
SHOCK_MIN_HISTORY = 20
SHOCK_Z = 6.0
SHOCK_MIN_MOVE = 0.02
SHOCK_MAD_FLOOR = 0.002
PERSISTENCE_DAYS = 3
ATTRIB_LOOKBACK_DAYS = 3
IMPACT_DAYS = 7
THIN_LISTINGS = 10
THIN_SPIKE = math.log(1.75)

SALES_TAX = {"synthetic": [(date(1970, 1, 1), 0.036)], "eve": [(date(1970, 1, 1), 0.036)]}
BROKER_FEE = {"synthetic": 0.015}


def inflation_rates(index: pl.DataFrame) -> pl.DataFrame:
    """index: series_id, day, value -> series_id, day, window_days, rate, annualized (calendar-day windows)."""
    v = index.filter(pl.col("value").is_not_null()).select("series_id", "day", "value")
    out = []
    for w in INFLATION_WINDOWS:
        past = v.with_columns(day=pl.col("day") + timedelta(days=w)).rename({"value": "past"})
        j = v.join(past, on=["series_id", "day"], how="inner").with_columns(
            window_days=pl.lit(w, pl.Int32), rate=pl.col("value") / pl.col("past") - 1
        )
        out.append(j.with_columns(annualized=(1 + pl.col("rate")) ** (365.0 / w) - 1))
    return (
        pl.concat(out)
        .select("series_id", "day", "window_days", "rate", "annualized")
        .sort("series_id", "window_days", "day")
    )


def detect_shocks(index: pl.DataFrame) -> pl.DataFrame:
    """index: series_id, day, value -> shocks: series_id, day, log_change, robust_z, direction."""
    v = index.filter(pl.col("value").is_not_null()).sort("series_id", "day")
    prev = v.select("series_id", (pl.col("day") + timedelta(days=1)).alias("day"), pl.col("value").alias("prev"))
    d = v.join(prev, on=["series_id", "day"], how="inner").with_columns(dl=(pl.col("value") / pl.col("prev")).log())
    d = d.sort("series_id", "day").with_columns(
        med=pl.col("dl").shift(1).rolling_median(SHOCK_WINDOW, min_samples=SHOCK_MIN_HISTORY).over("series_id"),
    )
    d = d.with_columns(
        mad=(pl.col("dl") - pl.col("med"))
        .abs()
        .shift(1)
        .rolling_median(SHOCK_WINDOW, min_samples=SHOCK_MIN_HISTORY)
        .over("series_id")
        * 1.4826
    ).with_columns(z=(pl.col("dl") - pl.col("med")) / pl.max_horizontal(pl.col("mad"), pl.lit(SHOCK_MAD_FLOOR)))
    hits = d.filter((pl.col("z").abs() >= SHOCK_Z) & (pl.col("dl").abs() >= SHOCK_MIN_MOVE)).with_columns(
        direction=pl.when(pl.col("dl") > 0).then(pl.lit("up")).otherwise(pl.lit("down"))
    )
    if hits.is_empty():
        return pl.DataFrame(
            schema={
                "series_id": pl.Int32,
                "day": pl.Date,
                "log_change": pl.Float64,
                "robust_z": pl.Float64,
                "direction": pl.String,
                "persistence": pl.Float64,
            }
        )
    # merge runs of consecutive same-direction shock days; keep the largest move of each run
    hits = (
        hits.sort("series_id", "day")
        .with_columns(
            new_run=((pl.col("day") - pl.col("day").shift(1).over("series_id")).dt.total_days().fill_null(99) > 1)
            | (pl.col("direction") != pl.col("direction").shift(1).over("series_id")).fill_null(True)
        )
        .with_columns(run=pl.col("new_run").cum_sum().over("series_id"))
    )
    best = hits.sort(pl.col("dl").abs(), descending=True).unique(["series_id", "run"], keep="first")
    # persistence: (ln I[t+k] - ln I[t-1]) / d_t ; null while day t+k is not published yet
    lv = v.select("series_id", "day", pl.col("value").log().alias("lv"))
    later = lv.with_columns(day=pl.col("day") - timedelta(days=PERSISTENCE_DAYS)).rename({"lv": "lv_later"})
    before = lv.with_columns(day=pl.col("day") + timedelta(days=1)).rename({"lv": "lv_before"})
    best = best.join(later, on=["series_id", "day"], how="left").join(before, on=["series_id", "day"], how="left")
    best = best.with_columns(persistence=((pl.col("lv_later") - pl.col("lv_before")) / pl.col("dl")).clip(-2, 2))
    return best.select(
        "series_id", "day", pl.col("dl").alias("log_change"), pl.col("z").alias("robust_z"), "direction", "persistence"
    ).sort("series_id", "day")


def attribute(
    shocks: pl.DataFrame, series_division: dict[int, str | None], patches: list[dict[str, Any]]
) -> pl.DataFrame:
    """shocks x patches -> candidate attributions with relevance and rank (rank 1 = most likely)."""
    rows = []
    for s in shocks.iter_rows(named=True):
        end = datetime.combine(s["day"] + timedelta(days=1), datetime.min.time()).replace(tzinfo=None)
        start = end - timedelta(days=ATTRIB_LOOKBACK_DAYS + 1)
        div = series_division.get(s["series_id"])
        cands = []
        for p in patches:
            rel_at = p["released_at"].replace(tzinfo=None)
            if not (start <= rel_at < end):
                continue
            lag_h = max(0.0, (end - rel_at).total_seconds() / 3600 - 24 + 12)  # measured to the shock day's noon
            tags = p["tags"]
            if div is not None and div in tags:
                topic = 1.0 / (tags.index(div) + 1)
            elif div is None and tags:
                topic = 0.8
            else:
                topic = 0.5 if p["is_major"] else 0.15
            relevance = topic * math.exp(-lag_h / 48)
            cands.append((relevance, lag_h, p))
        cands.sort(key=lambda c: (-c[0], c[1], c[2]["patch_id"]))
        for rank, (relevance, lag_h, p) in enumerate(cands, start=1):
            rows.append(
                {
                    "series_id": s["series_id"],
                    "day": s["day"],
                    "world_id": p["world_id"],
                    "patch_id": p["patch_id"],
                    "lag_hours": round(lag_h, 2),
                    "relevance": round(min(1.0, relevance), 4),
                    "rank": rank,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "series_id": pl.Int32,
            "day": pl.Date,
            "world_id": pl.String,
            "patch_id": pl.String,
            "lag_hours": pl.Float64,
            "relevance": pl.Float64,
            "rank": pl.Int32,
        },
    )


def patch_impact(index: pl.DataFrame, patches: list[dict[str, Any]]) -> pl.DataFrame:
    """For each patch x series: log change of the median level in the IMPACT_DAYS after vs before release,
    and its robust z against every other day's same-shaped before/after change."""
    v = index.filter(pl.col("value").is_not_null()).sort("series_id", "day")
    lv = v.with_columns(lv=pl.col("value").log())
    # rolling medians: before = days t-7..t-1, after = t+1..t+7
    lv = lv.with_columns(
        before=pl.col("lv").shift(1).rolling_median(IMPACT_DAYS, min_samples=4).over("series_id"),
        after=pl.col("lv").shift(-IMPACT_DAYS).rolling_median(IMPACT_DAYS, min_samples=4).over("series_id"),
    ).with_columns(chg=pl.col("after") - pl.col("before"))
    stats = lv.group_by("series_id").agg(
        m=pl.col("chg").median(), mad=((pl.col("chg") - pl.col("chg").median()).abs().median() * 1.4826)
    )
    pdf = pl.DataFrame(
        [{"world_id": p["world_id"], "patch_id": p["patch_id"], "day": p["released_at"].date()} for p in patches],
        schema={"world_id": pl.String, "patch_id": pl.String, "day": pl.Date},
    )
    j = pdf.join(lv.select("series_id", "day", "chg"), on="day").join(stats, on="series_id").drop_nulls("chg")
    return j.select(
        "world_id",
        "patch_id",
        "series_id",
        pl.col("chg").alias("log_change"),
        ((pl.col("chg") - pl.col("m")) / pl.max_horizontal(pl.col("mad"), pl.lit(1e-4))).alias("robust_z"),
    )


def manipulation_events(daily: pl.DataFrame) -> pl.DataFrame:
    """daily prices with diagnostics -> manipulation_event rows.

    extreme_listing   a listing >= EXTREME_Z robust deviations from its book (snapshot worlds);
                      severity = max z; detail shows how far a naive mean would have been dragged.
    hampel_reject     a daily trade average rejected by the causal Hampel filter (history worlds).
    thin_market_spike a published price in a thin market (< THIN_LISTINGS listings or trades) that
                      jumps >= 75% from its trailing 14-day median: a cornered market or a real move -
                      flagged for review, not removed (it was not a single listing).
    """
    out = []
    if "n_extreme" in daily.columns:
        ext = daily.filter(pl.col("n_extreme").fill_null(0) > 0).select(
            "server_id",
            "item_id",
            "day",
            pl.lit("extreme_listing").alias("kind"),
            pl.col("max_z").alias("severity"),
            pl.col("max_listings").fill_null(0).cast(pl.Int32).alias("n_obs"),
            (pl.col("max_listings").fill_null(0) < THIN_LISTINGS).alias("thin"),
            pl.struct(n_extreme="n_extreme", robust_price="price", naive_mean="naive_mean").alias("detail"),
        )
        out.append(ext)
    if "hampel_z" in daily.columns:
        rej = daily.filter(pl.col("status") == "rejected").select(
            "server_id",
            "item_id",
            "day",
            pl.lit("hampel_reject").alias("kind"),
            pl.col("hampel_z").alias("severity"),
            pl.col("n_obs").cast(pl.Int32),
            (pl.col("n_obs") < THIN_LISTINGS).alias("thin"),
            pl.struct(rejected_average="raw_price", trailing_median="hampel_ref").alias("detail"),
        )
        out.append(rej)
    depth = pl.col("max_listings") if "max_listings" in daily.columns else pl.col("n_obs")
    sp = (
        daily.sort("server_id", "item_id", "day")
        .with_columns(
            lp=pl.col("price").log(),
            depth=depth.fill_null(0),
        )
        .with_columns(
            ref=pl.col("lp").shift(1).rolling_median(14, min_samples=5).over("server_id", "item_id"),
        )
        .filter((pl.col("depth") < THIN_LISTINGS) & ((pl.col("lp") - pl.col("ref")).abs() >= THIN_SPIKE))
    )
    out.append(
        sp.select(
            "server_id",
            "item_id",
            "day",
            pl.lit("thin_market_spike").alias("kind"),
            ((pl.col("lp") - pl.col("ref")).abs() / THIN_SPIKE).alias("severity"),
            pl.col("depth").cast(pl.Int32).alias("n_obs"),
            pl.lit(True).alias("thin"),
            pl.struct(price="price", trailing_median=pl.col("ref").exp()).alias("detail"),
        )
    )
    frames = [f.with_columns(pl.col("detail").struct.json_encode()) for f in out if not f.is_empty()]
    if not frames:
        return pl.DataFrame(
            schema={
                "server_id": pl.String,
                "item_id": pl.Int64,
                "day": pl.Date,
                "kind": pl.String,
                "severity": pl.Float64,
                "n_obs": pl.Int32,
                "thin": pl.Boolean,
                "detail": pl.String,
            }
        )
    return pl.concat(frames, how="vertical_relaxed").with_columns(pl.col("severity").fill_null(0).clip(0, 1e9))


def money_flows(
    world_id: str, fills: pl.DataFrame | None, history: pl.DataFrame | None, faucets: pl.DataFrame | None
) -> pl.DataFrame:
    """Currency sinks (taxes/fees on observed trade flows) and faucets (payout feeds) per server-day."""
    out = []
    tax = SALES_TAX[world_id][-1][1]
    if fills is not None and not fills.is_empty():
        f = (
            fills.with_columns(day=pl.col("snapshot_ts").dt.date())
            .group_by("server_id", "day")
            .agg(traded=pl.col("fill_value").sum(), listed=pl.col("new_listing_value").sum())
        )
        out.append(
            f.select(
                "server_id",
                "day",
                pl.lit("sink_sales_tax").alias("kind"),
                (pl.col("traded") * tax).alias("amount"),
                pl.lit(f"inferred fills x {tax:.1%} sales tax").alias("method"),
            )
        )
        fee = BROKER_FEE.get(world_id)
        if fee:
            out.append(
                f.select(
                    "server_id",
                    "day",
                    pl.lit("sink_broker_fee").alias("kind"),
                    (pl.col("listed") * fee).alias("amount"),
                    pl.lit(f"new listing value x {fee:.1%} broker fee").alias("method"),
                )
            )
    if history is not None and not history.is_empty():
        h = history.group_by("server_id", "day").agg(traded=(pl.col("average") * pl.col("volume")).sum())
        out.append(
            h.select(
                "server_id",
                "day",
                pl.lit("sink_sales_tax").alias("kind"),
                (pl.col("traded") * tax).alias("amount"),
                pl.lit(f"basket-item trade value x {tax:.1%} assumed sales tax (lower bound)").alias("method"),
            )
        )
    if faucets is not None and not faucets.is_empty():
        out.append(
            faucets.group_by("server_id", "day")
            .agg(amount=pl.col("amount").sum())
            .select(
                "server_id",
                "day",
                pl.lit("faucet_bounty").alias("kind"),
                "amount",
                pl.lit("reported bounty + mission payouts").alias("method"),
            )
        )
    if not out:
        return pl.DataFrame(
            schema={
                "server_id": pl.String,
                "day": pl.Date,
                "kind": pl.String,
                "amount": pl.Float64,
                "method": pl.String,
            }
        )
    return pl.concat(out).sort("server_id", "day", "kind")
