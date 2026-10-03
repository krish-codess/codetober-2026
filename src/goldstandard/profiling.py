"""Profile the real EVE data and derive the synthetic generator's calibration from it.

Outputs:
  docs/DATA_PROFILE.md                  human-readable profile (nulls, cardinality, distributions,
                                        assumption violations) - regenerate with `goldstandard profile`
  reference/synthetic_calibration.json  per-item price level, volume, book depth and ask dispersion
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from goldstandard.config import Settings
from goldstandard.parse import parse_history, parse_order_bundles
from goldstandard.raw import RawStore
from goldstandard.sources.eve import load_universe


def _records(store: RawStore, kind: str) -> list[Any]:
    return [store.read(ref) for d in store.days("eve", kind) for ref in store.iter_refs("eve", kind, d)]


def _md(df: pl.DataFrame, floatfmt: str = "{:.4g}") -> str:
    cols = df.columns
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.iter_rows():
        out.append("| " + " | ".join(floatfmt.format(v) if isinstance(v, float) else str(v) for v in row) + " |")
    return "\n".join(out)


def profile(cfg: Settings, out_doc: Path, out_calibration: Path) -> dict[str, Any]:
    store = RawStore(cfg.raw_dir)
    universe = load_universe()
    items = {i["type_id"] for i in universe["items"]}
    servers = {s["server_id"] for s in universe["servers"]}
    names = {}
    for rec in _records(store, "type_meta"):
        t = json.loads(rec.body)["type"]
        names[t["type_id"]] = t["name"]

    hist = parse_history(_records(store, "market_history"), items, servers)
    h = hist.valid
    orders = parse_order_bundles("eve", _records(store, "orders"), items, servers)
    o = orders.valid
    sells = o.filter(~pl.col("is_buy"))

    # --- history profile
    span = h.select(pl.col("day").min().alias("first"), pl.col("day").max().alias("last"))
    n_days = (span["last"][0] - span["first"][0]).days + 1
    cov = h.group_by("server_id", "item_id").agg(pl.len().alias("days_with_trades"))
    missing_series = len(servers) * len(items) - cov.height
    returns = (
        h.sort("server_id", "item_id", "day")
        .with_columns(r=pl.col("average").log().diff().over("server_id", "item_id"))
        .drop_nulls("r")
    )
    ret_q = returns.select(
        *[pl.col("r").abs().quantile(q).alias(f"p{int(q * 1000) / 10}") for q in (0.5, 0.9, 0.99, 0.999)],
        pl.col("r").abs().max().alias("max"),
    )
    spikes = returns.filter(pl.col("r").abs() > 2.0).height  # > e^2 = 7.4x day-over-day
    spread = h.with_columns(ratio=pl.col("highest") / pl.col("lowest"))
    wide = spread.filter(pl.col("ratio") > 10)
    worst = (
        returns.sort(pl.col("r").abs(), descending=True)
        .head(8)
        .select("server_id", "item_id", "day", "average", "lowest", "highest", "volume", "order_count", "r")
        .with_columns(item=pl.col("item_id").replace_strict(names, default="?"))
    )

    # --- order book profile
    book = (
        sells.group_by("server_id", "item_id")
        .agg(
            n=pl.len(),
            mn=pl.col("price").min(),
            med=pl.col("price").median(),
            mx=pl.col("price").max(),
            mean=pl.col("price").mean(),
        )
        .with_columns(max_over_median=pl.col("mx") / pl.col("med"), mean_over_median=pl.col("mean") / pl.col("med"))
    )
    absurd = (
        book.sort("max_over_median", descending=True)
        .head(8)
        .with_columns(item=pl.col("item_id").replace_strict(names, default="?"))
    )

    # --- calibration (The Forge = deepest market; servers scale down from it)
    recent = h.filter(pl.col("day") >= pl.col("day").max() - pl.duration(days=90))
    forge_hist = recent.filter(pl.col("server_id") == "eve-the-forge")
    p0 = forge_hist.group_by("item_id").agg(p0=pl.col("average").median(), vol0=pl.col("volume").median())
    depth = (
        sells.filter(pl.col("server_id") == "eve-the-forge")
        .with_columns(lp=pl.col("price").log())
        .with_columns(dev=(pl.col("lp") - pl.col("lp").median().over("item_id")))
        .filter(pl.col("dev").abs() < 1.0)  # ignore absurd listings when measuring normal dispersion
        .group_by("item_id")
        .agg(n_sell=pl.len(), disp=(pl.col("dev").abs().median() * 1.4826))
    )
    cal = p0.join(depth, on="item_id", how="left").with_columns(
        n_sell=pl.col("n_sell").fill_null(10), disp=pl.col("disp").fill_null(0.05).clip(0.01, 0.4)
    )
    calibration = {
        "_doc": "Derived from real ESI data by goldstandard.profiling; consumed by the synthetic generator.",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "items": {
            str(r["item_id"]): {
                "name": names.get(r["item_id"], str(r["item_id"])),
                "p0": r["p0"],
                "vol0": max(1.0, r["vol0"]),
                "n_sell": int(r["n_sell"]),
                "disp": round(r["disp"], 4),
            }
            for r in cal.sort("item_id").iter_rows(named=True)
        },
    }
    out_calibration.write_text(json.dumps(calibration, indent=1) + "\n", newline="\n")

    days_col: Any = cov["days_with_trades"]
    n_col: Any = book["n"]
    ratio_col: Any = book["mean_over_median"]
    by_ret = returns.group_by("item_id").agg(daily_vol=pl.col("r").std())
    lines = [
        "# Data profile (real EVE Online data)",
        "",
        f"Generated by `goldstandard profile` from the raw store on {datetime.now(UTC):%Y-%m-%d}. "
        "This profile was produced *before* the schema and estimators were designed, and drove them "
        "(see DECISIONS.md).",
        "",
        "## Market history (`/markets/{region}/history/`)",
        "",
        f"- Rows: **{h.height:,}** across {cov.height} (server, item) series; "
        f"{missing_series} of {len(servers) * len(items)} requested series have **no history at all**.",
        f"- Span: {span['first'][0]} to {span['last'][0]} ({n_days} days). "
        f"Median series has {int(days_col.median() or 0)} trading days; the thinnest has "
        f"{days_col.min()} -> **days without trades are absent, not zero-filled**.",
        f"- Nulls: {int(sum(h.null_count().row(0)))}. Duplicates on (server,item,day): "
        f"{h.height - h.unique(['server_id', 'item_id', 'day']).height}. "
        f"Quarantined by the boundary validator: {hist.reasons or 'none'}.",
        f"- Days where highest/lowest > 10x: **{wide.height}** (single trades far from the rest of the day).",
        "- Day-over-day |log change| of the daily average:",
        "",
        _md(ret_q),
        "",
        f"- **{spikes}** day-over-day moves larger than e^2 (7.4x). The worst are 0.01 ISK trades and their "
        "rebound, i.e. manipulation / fat-finger, not real repricing:",
        "",
        _md(worst),
        "",
        "## Order book snapshot (`/markets/{region}/orders/`)",
        "",
        f"- Listings: **{o.height:,}** ({sells.height:,} sell / {o.height - sells.height:,} buy). "
        f"Quarantined: {orders.reasons or 'none'}. Fetch gaps: {orders.gaps.height}.",
        f"- Sell listings per (server,item): median {int(n_col.median() or 0)}, 10th pct "
        f"{int(n_col.quantile(0.1) or 0)}, min {n_col.min()}; "
        f"{book.filter(pl.col('n') < 3).height} series have fewer than 3 (thin).",
        f"- Mean/median ask ratio: median {ratio_col.median():.3g}, max "
        f"{ratio_col.max():.3g} -> **a mean is unusable**; one listing can move it by orders of "
        "magnitude.",
        "",
        "Most extreme listings relative to their book's median:",
        "",
        _md(absurd.select("server_id", "item", "n", "mn", "med", "mx", "max_over_median")),
        "",
        "## Assumptions this data violates",
        "",
        "1. *Every item trades every day* - false; thin series miss up to half the days.",
        "2. *Listing prices are honest* - false; absurd asks of 10^3-10^7x the median sit in deep books.",
        "3. *Daily average is a fair price* - false on thin days; a single 0.01 ISK fill sets the average.",
        "4. *Patch notes have times* - false; only dates (deployments happen at 11:00 UTC downtime).",
        "5. *History is final* - not guaranteed; ESI may revise the most recent day, so fetches are versioned.",
        "",
        "## Daily volatility by item (std of daily log change)",
        "",
        _md(
            by_ret.join(cal.select("item_id", "p0", "n_sell", "disp"), on="item_id")
            .sort("item_id")
            .with_columns(item=pl.col("item_id").replace_strict(names, default="?"))
        ),
        "",
    ]
    out_doc.write_text("\n".join(lines), newline="\n")
    return {"history_rows": h.height, "listings": o.height, "items_calibrated": cal.height}
