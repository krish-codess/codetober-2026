"""Chain-linked Laspeyres price index with quarterly re-weighting (see docs/METHODOLOGY.md).

Basket (one per world per calendar quarter P, frozen once built):
  * reference period  = the previous quarter (clipped to available data, >= MIN_REF_DAYS days)
  * eligibility       = a (server, item) cell with a robust price on >= ELIGIBLE_SHARE of the
                        reference days, a price in the link window, and traded volume > 0
  * expenditure       = sum over reference days of price * traded volume
  * weights           = expenditure share within the server, capped at WEIGHT_CAP per item
                        (excess redistributed pro rata), then scaled by the server's share of
                        world expenditure -> weights over all cells of a world sum to 1
  * base price p0     = median daily price over the link window (last LINK_DAYS of the reference
                        period). A median, not the last day, so one thin day cannot set the base.

Index of series S (a server or all servers, a division or all) on day t in period P:
    I_t = L_P(S) * sum_g W_g R_g,t / sum_g W_g        over groups g observed at t
    R_g,t = sum_{i in g, observed} w_i (p_i,t / p0_i) / sum_{i in g, observed} w_i
  with groups g = (server, division). An item without a price at t is NOT given a price; its
  weight is carried by the observed items of its own group (standard CPI cell-relative
  imputation). Coverage = observed weight / total weight. Coverage < MIN_COVERAGE -> no value.

Chain link: L_P(S) = L_{P-1}(S) * J, J = the old basket's aggregate relative evaluated at the
  link-window prices. The first period of a world has L = 100 (index reference = 100).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

METHOD_VERSION = "gs-1.0"
WEIGHT_CAP = 0.20
ELIGIBLE_SHARE = 0.5
LINK_DAYS = 7
MIN_REF_DAYS = 21
OK_COVERAGE = 0.9
MIN_COVERAGE = 0.5
BASKET_VALUE = 1_000_000.0  # currency units the base basket costs at base prices (for labour-hours)
ALL = "all"


@dataclass(frozen=True)
class Period:
    world_id: str
    period_id: str
    valid_from: date
    valid_to: date
    ref_from: date
    ref_to: date
    link_from: date
    link_to: date


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _next_quarter(d: date) -> date:
    return date(d.year + (d.month >= 10), (d.month + 2) % 12 + 1, 1)


def quarter_periods(world_id: str, data_start: date, through: date) -> list[Period]:
    """All basket periods whose validity starts on or before `through`."""
    out = []
    q = _next_quarter(_quarter_start(data_start))
    while q <= through:
        prev = _quarter_start(q - timedelta(days=1))
        ref_from, ref_to = max(prev, data_start), q - timedelta(days=1)
        if (ref_to - ref_from).days + 1 >= MIN_REF_DAYS:
            out.append(
                Period(
                    world_id,
                    f"{q.year}Q{(q.month - 1) // 3 + 1}",
                    q,
                    _next_quarter(q) - timedelta(days=1),
                    ref_from,
                    ref_to,
                    ref_to - timedelta(days=LINK_DAYS - 1),
                    ref_to,
                )
            )
        q = _next_quarter(q)
    return out


def period_for(periods: list[Period], d: date) -> Period | None:
    return next((p for p in periods if p.valid_from <= d <= p.valid_to), None)


def cap_weights(w: np.ndarray, cap: float = WEIGHT_CAP) -> np.ndarray:
    """Normalise to 1 and cap each weight at `cap`, redistributing the excess pro rata.
    If capping is infeasible (n * cap < 1) the weights become equal."""
    w = np.asarray(w, dtype=float)
    w = w / w.sum()
    n = len(w)
    if cap * n <= 1.0:
        return np.full(n, 1.0 / n)
    fixed = np.zeros(n, bool)
    for _ in range(n):
        over = (w > cap + 1e-12) & ~fixed
        if not over.any():
            break
        fixed |= over
        w[fixed] = cap
        free = ~fixed
        w[free] = w[free] / w[free].sum() * (1.0 - cap * fixed.sum())
    return w


def build_basket(period: Period, daily: pl.DataFrame, divisions: dict[int, str]) -> pl.DataFrame:
    """daily: server_id, item_id, day, price (null if not ok), volume. -> basket cells."""
    ref = daily.filter(pl.col("day").is_between(period.ref_from, period.ref_to) & pl.col("price").is_not_null())
    n_days = (period.ref_to - period.ref_from).days + 1
    cells = ref.group_by("server_id", "item_id").agg(
        days_ok=pl.len(),
        expenditure=(pl.col("price") * pl.col("volume").fill_null(0)).sum(),
        base_price=pl.col("price").filter(pl.col("day").is_between(period.link_from, period.link_to)).median(),
    )
    eligible = cells.filter(
        (pl.col("days_ok") >= ELIGIBLE_SHARE * n_days)
        & pl.col("base_price").is_not_null()
        & (pl.col("expenditure") > 0)
    ).sort("server_id", "item_id")
    parts = []
    world_total = float(eligible["expenditure"].sum())
    for _, g in eligible.group_by(["server_id"], maintain_order=True):
        server_total = float(g["expenditure"].sum())
        capped = cap_weights(g["expenditure"].to_numpy())
        parts.append(g.with_columns(weight=pl.Series(capped * server_total / world_total)))
    if not parts:
        return pl.DataFrame(
            schema={
                "server_id": pl.String,
                "item_id": pl.Int64,
                "division_id": pl.String,
                "weight": pl.Float64,
                "base_price": pl.Float64,
                "expenditure": pl.Float64,
                "quantity": pl.Float64,
            }
        )
    basket = pl.concat(parts).with_columns(
        division_id=pl.col("item_id").replace_strict(divisions, return_dtype=pl.String),
        quantity=pl.col("weight") * BASKET_VALUE / pl.col("base_price"),
    )
    return basket.select("server_id", "item_id", "division_id", "weight", "base_price", "expenditure", "quantity")


def group_relatives(basket: pl.DataFrame, prices: pl.DataFrame) -> pl.DataFrame:
    """basket x prices(server_id, item_id, day, price) -> per (day, server, division) group:
    W (full weight), O (observed weight), WR (sum of w * relative over observed cells)."""
    days = prices.select("day").unique()
    grid = basket.join(days, how="cross").join(
        prices.select("server_id", "item_id", "day", "price"), on=["server_id", "item_id", "day"], how="left"
    )
    grid = grid.with_columns(rel=pl.col("price") / pl.col("base_price"))
    return grid.group_by("day", "server_id", "division_id").agg(
        W=pl.col("weight").sum(),
        O=pl.col("weight").filter(pl.col("rel").is_not_null()).sum(),
        WR=(pl.col("weight") * pl.col("rel")).filter(pl.col("rel").is_not_null()).sum(),
        n_obs=pl.col("rel").is_not_null().sum(),
    )


def aggregate(groups: pl.DataFrame, servers: list[str], divisions: list[str]) -> pl.DataFrame:
    """Group relatives -> one row per (scope, division, day): rel (aggregate relative), coverage, n_items."""
    out = []
    for scope in [*servers, ALL]:
        g1 = groups if scope == ALL else groups.filter(pl.col("server_id") == scope)
        for div in [*divisions, ALL]:
            g = g1 if div == ALL else g1.filter(pl.col("division_id") == div)
            if g.is_empty():
                continue
            agg = g.group_by("day").agg(
                num=(pl.col("W") * pl.col("WR") / pl.col("O")).filter(pl.col("O") > 0).sum(),
                den=pl.col("W").filter(pl.col("O") > 0).sum(),
                coverage=pl.col("O").sum() / pl.col("W").sum(),
                n_items=pl.col("n_obs").sum().cast(pl.Int32),
            )
            out.append(agg.with_columns(scope=pl.lit(scope), division=pl.lit(div)))
    if not out:
        return pl.DataFrame(
            schema={
                "day": pl.Date,
                "rel": pl.Float64,
                "coverage": pl.Float64,
                "n_items": pl.Int32,
                "scope": pl.String,
                "division": pl.String,
            }
        )
    res = pl.concat(out).with_columns(rel=pl.when(pl.col("den") > 0).then(pl.col("num") / pl.col("den")))
    return res.select("scope", "division", "day", "rel", pl.col("coverage").clip(0, 1), "n_items")


def link_factors(
    prev_basket: pl.DataFrame, link_prices: pl.DataFrame, servers: list[str], divisions: list[str]
) -> dict[tuple[str, str], float]:
    """J per series: the previous basket's aggregate relative at the link-window prices.
    link_prices: server_id, item_id, price (median over the link window)."""
    lp = link_prices.with_columns(day=pl.lit(date(1970, 1, 1)))
    agg = aggregate(group_relatives(prev_basket, lp), servers, divisions)
    return {
        (r["scope"], r["division"]): r["rel"]
        for r in agg.iter_rows(named=True)
        if r["rel"] is not None and r["coverage"] >= MIN_COVERAGE
    }


def index_values(
    basket: pl.DataFrame,
    prices: pl.DataFrame,
    links: dict[tuple[str, str], float],
    servers: list[str],
    divisions: list[str],
) -> pl.DataFrame:
    """Index level per (scope, division, day) with coverage-based status. Value null if insufficient
    coverage or no link factor (never extrapolated)."""
    agg = aggregate(group_relatives(basket, prices), servers, divisions)
    link_df = pl.DataFrame(
        [{"scope": s, "division": d, "link": v} for (s, d), v in links.items()],
        schema={"scope": pl.String, "division": pl.String, "link": pl.Float64},
    )
    out = agg.join(link_df, on=["scope", "division"], how="left").with_columns(
        status=pl.when(pl.col("rel").is_null() | pl.col("link").is_null() | (pl.col("coverage") < MIN_COVERAGE))
        .then(pl.lit("insufficient"))
        .when(pl.col("coverage") < OK_COVERAGE)
        .then(pl.lit("partial"))
        .otherwise(pl.lit("ok"))
    )
    return (
        out.with_columns(
            value=pl.when(pl.col("status") != "insufficient").then((pl.col("link") * pl.col("rel")).round(6)),
        )
        .select("scope", "division", "day", "value", "coverage", "n_items", "status")
        .sort("scope", "division", "day")
    )


def price_fingerprint(prices: pl.DataFrame) -> str:
    """Stable 16-hex fingerprint of a price vector (server_id, item_id, day, price)."""
    rows = prices.select("server_id", "item_id", "day", "price").sort("server_id", "item_id", "day")
    h = hashlib.sha256()
    for r in rows.iter_rows():
        h.update(repr(r).encode())
    return h.hexdigest()[:16]
