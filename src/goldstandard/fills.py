"""Trade (fill) inference from consecutive order-book snapshots.

Order books show asks, not trades. Between two snapshots a -> b of one (server, item):

  * an order present in both whose volume_remain fell was partially filled: certain fill.
  * an order present in a but gone from b either expired, was cancelled, or was bought out.
      - expired: issued + duration <= ts_b                                   -> not a fill
      - otherwise it is counted as a fill iff its price is at or below the "fill frontier":
        the highest price at which we *saw* a partial fill in a->b; with no partial fill, the
        best ask remaining in b (buyers lift the cheapest asks first, so a vanished ask cheaper
        than what is still listed was bought); with an empty book in b, every vanished ask.
      - above the frontier -> treated as a cancellation.
  * an order present only in b is a new listing (broker-fee base).

This is a heuristic with a measurable error: tests/test_fills.py compares it against the
synthetic generator's ground-truth fills (differential test), and docs/PERFORMANCE.md reports
the error. Volumes feed basket weights, so the error only shifts weights, never prices.
"""

from __future__ import annotations

import polars as pl


def infer_fills(listings: pl.DataFrame) -> pl.DataFrame:
    """listings (sell + buy, possibly spanning several days) -> one row per (server, item, snapshot b)
    with fill_qty, fill_value, new_listing_value. Only sell orders are considered (asks lifted by buyers).
    The first snapshot of each series produces no row (no predecessor)."""
    s = listings.filter(~pl.col("is_buy")).select(
        "server_id",
        "item_id",
        "snapshot_ts",
        "order_id",
        "price",
        "volume_remain",
        "volume_total",
        "issued",
        "duration",
    )
    snaps = (
        s.select("server_id", "item_id", "snapshot_ts")
        .unique()
        .sort("server_id", "item_id", "snapshot_ts")
        .with_columns(prev_ts=pl.col("snapshot_ts").shift(1).over("server_id", "item_id"))
        .drop_nulls("prev_ts")
    )
    if snaps.is_empty():
        return pl.DataFrame(
            schema={
                "server_id": pl.String,
                "item_id": pl.Int64,
                "snapshot_ts": pl.Datetime("us", "UTC"),
                "fill_qty": pl.Int64,
                "fill_value": pl.Float64,
                "new_listing_value": pl.Float64,
            }
        )
    a = snaps.join(s.rename({"snapshot_ts": "prev_ts"}), on=["server_id", "item_id", "prev_ts"])
    b = s.select("server_id", "item_id", "snapshot_ts", "order_id", pl.col("volume_remain").alias("remain_b"))
    pair = a.join(b, on=["server_id", "item_id", "snapshot_ts", "order_id"], how="left")
    key = ["server_id", "item_id", "snapshot_ts"]
    pair = pair.with_columns(
        partial=pl.col("remain_b").is_not_null() & (pl.col("remain_b") < pl.col("volume_remain")),
        gone=pl.col("remain_b").is_null(),
        expired=(pl.col("issued") + pl.duration(days=pl.col("duration"))) <= pl.col("snapshot_ts"),
    )
    best_ask_b = s.group_by(key).agg(best_ask_b=pl.col("price").min())
    frontier = pair.filter(pl.col("partial")).group_by(key).agg(partial_frontier=pl.col("price").max())
    pair = (
        pair.join(frontier, on=key, how="left")
        .join(best_ask_b, on=key, how="left")
        .with_columns(frontier=pl.coalesce("partial_frontier", "best_ask_b", pl.lit(float("inf"))))
        .with_columns(
            qty=pl.when(pl.col("partial"))
            .then(pl.col("volume_remain") - pl.col("remain_b"))
            .when(pl.col("gone") & ~pl.col("expired") & (pl.col("price") <= pl.col("frontier")))
            .then(pl.col("volume_remain"))
            .otherwise(0)
        )
    )
    fills = pair.group_by(key).agg(
        fill_qty=pl.col("qty").sum().cast(pl.Int64),
        fill_value=(pl.col("qty") * pl.col("price")).sum(),
    )
    a_ids = a.select(*key, "order_id")
    new = (
        s.join(snaps.select(key), on=key)  # only snapshots that have a predecessor
        .join(a_ids, on=[*key, "order_id"], how="anti")
        .group_by(key)
        .agg(new_listing_value=(pl.col("price") * pl.col("volume_total")).sum())
    )
    out = snaps.select(key).join(fills, on=key, how="left").join(new, on=key, how="left")
    return out.with_columns(
        pl.col("fill_qty").fill_null(0), pl.col("fill_value").fill_null(0.0), pl.col("new_listing_value").fill_null(0.0)
    ).sort(key)
