"""The processing graph, as plain functions. Dagster assets (defs.py) and the CLI both call these.

    raw store --(stage + validate)--> lake: listings | history, quarantine, gaps, faucets
              --(robust estimators)--> lake: daily_prices, fills  --> pg: item_price_daily (vintaged)
              --(baskets, frozen)----> pg: basket_period / basket_item / basket_link
              --(Laspeyres)----------> pg: index_value (vintaged)
              --(analytics)----------> pg: inflation_rate, shock(+attribution), patch_impact,
                                           manipulation_event, money_flow

Idempotency and targeted backfills: every (world, day) partition records the fingerprint of the
raw inputs it was computed from (data_quality_daily.raw_fingerprint). `changed_days` compares
those with the raw store, so a late-arriving payload re-processes exactly its own day plus the
days that depend on it, and re-running anything with unchanged inputs writes nothing.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from typing import Any

import polars as pl
import psycopg

from goldstandard import analytics, db, estimators, index, parse
from goldstandard.config import Settings
from goldstandard.fills import infer_fills
from goldstandard.lake import Lake, fingerprint
from goldstandard.obs import log, metrics
from goldstandard.raw import RawRef, RawStore
from goldstandard.reference import build_reference, tag_divisions

logger = logging.getLogger(__name__)
WORLDS = ("synthetic", "eve")
SOURCE = {"synthetic": "synthetic", "eve": "eve"}
METHOD = {"synthetic": "lowq_ask_hampel_v1", "eve": "hampel_vwap_v1"}
REVISION_REASON = {"synthetic": "late_data", "eve": "source_revision"}
BASKET_GRACE_DAYS = 3  # late snapshots arrive up to 3 days late: freeze a basket only after that
HAMPEL_DEPENDENTS = estimators.HAMPEL_WINDOW


@dataclass
class Report:
    world: str
    days_processed: list[date] = field(default_factory=list)
    prices_written: int = 0
    index_written: int = 0
    baskets_frozen: list[str] = field(default_factory=list)
    quarantined: int = 0
    timings: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = self.days_processed
        return {
            "world": self.world,
            "days": len(d),
            "first": str(d[0]) if d else None,
            "last": str(d[-1]) if d else None,
            "prices_written": self.prices_written,
            "index_written": self.index_written,
            "baskets_frozen": self.baskets_frozen,
            "quarantined": self.quarantined,
            "timings_s": {k: round(v, 2) for k, v in self.timings.items()},
        }


class Pipeline:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.store = RawStore(cfg.raw_dir)
        self.lake = Lake(cfg.lake_dir)

    # ------------------------------------------------------------------------------ reference
    @cached_property
    def ref(self) -> dict[str, list[dict[str, Any]]]:
        return build_reference(self.cfg)

    @cached_property
    def item_division(self) -> dict[int, str]:
        return {i["item_id"]: i["division_id"] for i in self.ref["items"]}

    @cached_property
    def divisions(self) -> list[str]:
        return [d["division_id"] for d in self.ref["divisions"]]

    def servers(self, world: str) -> list[str]:
        return [s["server_id"] for s in self.ref["servers"] if s["world_id"] == world]

    def expected_cells(self, world: str) -> pl.DataFrame:
        items = sorted(self.item_division)
        if world == "synthetic":
            from goldstandard.sources.synthetic import load_calibration

            cal = load_calibration(self.cfg.reference_dir)
            items = [i for i in items if i in cal]
        return pl.DataFrame({"server_id": self.servers(world)}).join(
            pl.DataFrame({"item_id": items}, schema={"item_id": pl.Int64}), how="cross"
        )

    def sync_reference(self, conn: psycopg.Connection) -> dict[str, dict[tuple[str, str], int]]:
        db.upsert_reference(conn, self.ref)
        return {w: db.ensure_series(conn, w, self.servers(w), self.divisions) for w in WORLDS}

    # ------------------------------------------------------------------------------ change detection
    def _raw_refs(self, world: str, day: date) -> list[RawRef]:
        src = SOURCE[world]
        kinds = ("orders", "wallet_flows") if world == "synthetic" else ()
        return [r for k in kinds for r in self.store.iter_refs(src, k, day)]

    def raw_fingerprint(self, world: str, day: date) -> str:
        """Fingerprint of everything a day partition is computed from (content-addressed file names)."""
        refs = sorted(f"{r.kind}/{r.path.name}" for r in self._raw_refs(world, day))
        if world == "synthetic":  # fills for day D start from the last snapshot of D-1
            prev_day = day - timedelta(days=1)
            refs += sorted(f"prev/{r.path.name}" for r in self.store.iter_refs("synthetic", "orders", prev_day))
        return hashlib.sha256("\n".join(refs).encode()).hexdigest()

    def candidate_days(self, world: str) -> list[date]:
        if world == "synthetic":
            return self.store.days("synthetic", "orders")
        return self.lake.days("history", "eve")

    def changed_days(self, conn: psycopg.Connection, world: str) -> list[date]:
        """Days whose inputs differ from what was last processed, plus the days that depend on them."""
        if world == "eve":
            self.stage_history()
        done = db.get_fingerprints(conn, world)
        today = datetime.now(UTC).date()
        days = [d for d in self.candidate_days(world) if d < today]  # only complete UTC days
        changed = {d for d in days if done.get(d) != self.day_fingerprint(world, d)}
        dependents = HAMPEL_DEPENDENTS  # a day's Hampel decision reads the previous 14 observations
        available = set(days)
        for d in list(changed):
            changed |= {d + timedelta(days=k) for k in range(1, dependents + 1)} & available
        return sorted(changed)

    def day_fingerprint(self, world: str, day: date) -> str:
        if world == "synthetic":
            return self.raw_fingerprint(world, day)
        df = self.lake.read_day("history", "eve", day)
        return fingerprint(df) if df is not None else "absent"

    # ------------------------------------------------------------------------------ history world
    def stage_history(self) -> list[date]:
        """Parse every raw history fetch (latest fetch wins per day) into lake partitions; rewrite only
        partitions whose content changed. Returns the days rewritten."""
        t0 = time.perf_counter()
        recs = [
            self.store.read(r)
            for d in self.store.days("eve", "market_history")
            for r in self.store.iter_refs("eve", "market_history", d)
        ]
        cells = self.expected_cells("eve")
        parsed = parse.parse_history(recs, set(cells["item_id"].to_list()), set(self.servers("eve")))
        rewritten = []
        for (day,), part in parsed.valid.group_by(["day"], maintain_order=True):
            assert isinstance(day, date)
            old = self.lake.read_day("history", "eve", day)
            if old is None or fingerprint(old) != fingerprint(part):
                self.lake.write("history", "eve", day, part)
                rewritten.append(day)
        if not parsed.quarantine.is_empty():
            self.lake.write("quarantine", "eve", datetime.now(UTC).date(), parsed.quarantine)
        metrics.observe("stage_seconds", time.perf_counter() - t0, world="eve")
        log(
            logger,
            logging.INFO,
            "history staged",
            files=len(recs),
            rows=parsed.valid.height,
            rewritten=len(rewritten),
            quarantined=parsed.quarantine.height,
        )
        return sorted(rewritten)

    def _history_daily(self, days: list[date]) -> pl.DataFrame:
        lo = min(days) - timedelta(days=60)
        hist = self.lake.scan("history", "eve", start=lo, end=max(days))
        h = estimators.hampel_daily(hist) if not hist.is_empty() else hist
        grid = self.expected_cells("eve").join(pl.DataFrame({"day": days}), how="cross")
        out = grid.join(h, on=["server_id", "item_id", "day"], how="left") if not h.is_empty() else grid
        return (
            out.with_columns(
                status=pl.coalesce(pl.col("status"), pl.lit("missing"))
                if "status" in out.columns
                else pl.lit("missing"),
            )
            .with_columns(
                price=pl.when(pl.col("status") == "ok").then(pl.col("average")),
                raw_price=pl.col("average"),
                volume=pl.col("volume").cast(pl.Float64),
                n_obs=pl.col("order_count").fill_null(0).cast(pl.Int32),
                hampel_ref=pl.col("ref").exp(),
                hampel_z=pl.col("robust_z"),
            )
            .select(
                "server_id",
                "item_id",
                "day",
                "price",
                "volume",
                "n_obs",
                "status",
                "raw_price",
                "hampel_ref",
                "hampel_z",
            )
        )

    # ------------------------------------------------------------------------------ snapshot world
    def _stage_snapshot_day(self, world: str, day: date) -> tuple[parse.Parsed, list[Any]]:
        src = SOURCE[world]
        cells = self.expected_cells(world)
        recs = [self.store.read(r) for r in self.store.iter_refs(src, "orders", day)]
        parsed = parse.parse_order_bundles(world, recs, set(cells["item_id"].to_list()), set(self.servers(world)))
        self.lake.write("listings", world, day, parsed.valid)
        self.lake.write("quarantine", world, day, parsed.quarantine)
        self.lake.write("gaps", world, day, parsed.gaps)
        flow_recs = [self.store.read(r) for r in self.store.iter_refs(src, "wallet_flows", day)]
        flows = parse.parse_wallet_flows(world, flow_recs, set(self.servers(world)))
        self.lake.write("faucets", world, day, flows.valid)
        return parsed, recs + flow_recs

    def _snapshot_daily(self, world: str, day: date, listings: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        snaps = estimators.snapshot_prices(listings)
        prev = self.lake.read_day("listings", world, day - timedelta(days=1))
        frames = [listings]
        if prev is not None and not prev.is_empty():
            last = prev.group_by("server_id", "item_id").agg(pl.col("snapshot_ts").max().alias("snapshot_ts"))
            frames.insert(0, prev.join(last, on=["server_id", "item_id", "snapshot_ts"]))
        fills = infer_fills(pl.concat(frames)).filter(pl.col("snapshot_ts").dt.date() == day)
        daily = estimators.daily_from_snapshots(snaps, self.expected_cells(world), day)
        vol = fills.group_by("server_id", "item_id").agg(volume=pl.col("fill_qty").sum().cast(pl.Float64))
        daily = daily.join(vol, on=["server_id", "item_id"], how="left").with_columns(pl.col("volume").fill_null(0.0))
        return self._temporal_filter(world, day, daily), fills

    def _temporal_filter(self, world: str, day: date, daily: pl.DataFrame) -> pl.DataFrame:
        """Second robustness layer: causal Hampel on the daily series (same rule as the history path).
        A book captured wholesale by trolls, which no single-snapshot estimator can see, is rejected here."""
        daily = daily.with_columns(raw_price=pl.col("price"))
        cols = ["server_id", "item_id", "day", "raw_price"]
        trail = self.lake.scan(
            "daily_prices", world, start=day - timedelta(days=45), end=day - timedelta(days=1), columns=cols
        )
        frames = [daily.select(cols)] + ([trail.select(cols)] if not trail.is_empty() else [])
        hist = pl.concat(frames).rename({"raw_price": "average"}).drop_nulls("average")
        h = (
            estimators.hampel_daily(hist)
            .filter(pl.col("day") == day)
            .select(
                "server_id",
                "item_id",
                pl.col("status").alias("hampel"),
                pl.col("ref").exp().alias("hampel_ref"),
                pl.col("robust_z").alias("hampel_z"),
            )
        )
        rejected = (pl.col("hampel") == "rejected").fill_null(False)
        return (
            daily.join(h, on=["server_id", "item_id"], how="left")
            .with_columns(
                status=pl.when(rejected).then(pl.lit("rejected")).otherwise(pl.col("status")),
                price=pl.when(rejected).then(None).otherwise(pl.col("price")),
            )
            .drop("hampel")
        )

    # ------------------------------------------------------------------------------ per-day processing
    def process_days(
        self, conn: psycopg.Connection, world: str, days: list[date], report: Report | None = None
    ) -> Report:
        report = report or Report(world)
        if not days:
            return report
        t0 = time.perf_counter()
        days = sorted(days)
        history_daily = self._history_daily(days) if world == "eve" else None
        done = db.get_fingerprints(conn, world)
        for day in days:
            quality: dict[str, Any] | None
            if world == "synthetic":
                fp_now = self.raw_fingerprint(world, day)
                staged = self.lake.read_day("listings", world, day) if done.get(day) == fp_now else None
                if staged is None:  # new or changed inputs: parse + validate raw again
                    parsed, recs = self._stage_snapshot_day(world, day)
                    staged = parsed.valid
                    late = sum(1 for r in recs if (r.fetched_at - r.observed_at) > timedelta(hours=12))
                    quality = {
                        "n_payloads": len(recs),
                        "n_late_payloads": late,
                        "n_rows": parsed.valid.height + parsed.quarantine.height,
                        "n_valid": parsed.valid.height,
                        "n_quarantined": parsed.quarantine.height,
                        "reasons": {**parsed.reasons, "_gaps": parsed.gaps.height, "_coerced": parsed.coerced},
                    }
                    db.register_raw(conn, [_manifest(r) for r in recs])
                    report.quarantined += parsed.quarantine.height
                else:  # only a dependency changed: reuse the validated lake partition
                    quality = None
                daily, fills = self._snapshot_daily(world, day, staged)
                self.lake.write("fills", world, day, fills)
            else:
                assert history_daily is not None
                daily = history_daily.filter(pl.col("day") == day)
                staged = self.lake.read_day("history", "eve", day)
                n = 0 if staged is None else staged.height
                quality = {
                    "n_payloads": int(staged["raw_sha"].n_unique()) if staged is not None else 0,
                    "n_late_payloads": 0,
                    "n_rows": n,
                    "n_valid": n,
                    "n_quarantined": 0,
                    "reasons": {"_rejected": int((daily["status"] == "rejected").sum())},
                }
            self.lake.write("daily_prices", world, day, daily)
            fp = self.day_fingerprint(world, day)
            pub = daily.with_columns(
                method=pl.lit(METHOD[world]),
                input_hash=pl.lit(fp[:16]),
                reason=pl.lit(REVISION_REASON[world]),
                price=pl.when(pl.col("status") == "ok").then(pl.col("price")),
            )
            report.prices_written += db.publish_prices(conn, world, pub)
            # the fingerprint is recorded LAST: a crash before this line re-processes the day next time
            if quality is not None:
                db.upsert_quality(conn, {"world_id": world, "day": day, "raw_fingerprint": fp, **quality})
            conn.commit()
            report.days_processed.append(day)
        report.timings["prices"] = report.timings.get("prices", 0) + time.perf_counter() - t0
        metrics.inc("days_processed", len(days), world=world)
        return report

    # ------------------------------------------------------------------------------ baskets + index
    def periods(self, world: str) -> list[index.Period]:
        days = self.lake.days("daily_prices", world)
        if not days:
            return []
        return index.quarter_periods(world, days[0], days[-1])

    def ensure_baskets(self, conn: psycopg.Connection, world: str, series: dict[tuple[str, str], int]) -> list[str]:
        """Freeze every basket whose reference period is complete (plus the late-data grace period).
        A frozen basket is never rebuilt."""
        frozen: list[str] = []
        days = self.lake.days("daily_prices", world)
        if not days:
            return frozen
        last = days[-1]
        prev: index.Period | None = None
        for p in self.periods(world):
            if db.load_basket(conn, world, p.period_id) is not None:
                prev = p
                continue
            if last < p.ref_to + timedelta(days=BASKET_GRACE_DAYS) and last < p.valid_to:
                break  # reference period may still receive late data
            daily = self.lake.scan(
                "daily_prices",
                world,
                start=p.ref_from,
                end=p.ref_to,
                columns=["server_id", "item_id", "day", "price", "volume"],
            )
            basket = index.build_basket(p, daily, self.item_division)
            if basket.is_empty():
                log(
                    logger, logging.WARNING, "no eligible basket items; period skipped", world=world, period=p.period_id
                )
                prev = None
                continue
            servers, divs = self.servers(world), self.divisions
            prev_basket = db.load_basket(conn, world, prev.period_id) if prev else None
            if prev is None or prev_basket is None:
                cells = basket.select("server_id", "division_id").unique()
                links = {
                    (s, d): 100.0
                    for s in [*servers, index.ALL]
                    for d in [*divs, index.ALL]
                    if not cells.filter(
                        (pl.lit(s == index.ALL) | (pl.col("server_id") == s))
                        & (pl.lit(d == index.ALL) | (pl.col("division_id") == d))
                    ).is_empty()
                }
            else:
                old_links = db.load_links(conn, world, prev.period_id)
                link_prices = (
                    daily.filter(pl.col("day").is_between(p.link_from, p.link_to))
                    .group_by("server_id", "item_id")
                    .agg(price=pl.col("price").median())
                    .drop_nulls("price")
                )
                j = index.link_factors(prev_basket, link_prices, servers, divs)
                links = {k: old_links[k] * v for k, v in j.items() if k in old_links}
            db.freeze_basket(
                conn, p, index.METHOD_VERSION, basket, {series[k]: v for k, v in links.items() if k in series}
            )
            conn.commit()
            frozen.append(p.period_id)
            prev = p
            log(
                logger,
                logging.INFO,
                "basket frozen",
                world=world,
                period=p.period_id,
                cells=basket.height,
                links=len(links),
            )
        return frozen

    def index_days(
        self,
        conn: psycopg.Connection,
        world: str,
        days: list[date],
        series: dict[tuple[str, str], int],
        report: Report | None = None,
    ) -> Report:
        report = report or Report(world)
        t0 = time.perf_counter()
        frozen = self.ensure_baskets(conn, world, series)
        report.baskets_frozen += frozen
        periods = self.periods(world)
        # a newly frozen basket makes its whole validity window computable
        targets = set(days)
        for p in periods:
            if p.period_id in frozen:
                targets |= {d for d in self.lake.days("daily_prices", world) if p.valid_from <= d <= p.valid_to}
        by_period: dict[str, list[date]] = {}
        for d in sorted(targets):
            per = index.period_for(periods, d)
            if per is not None:
                by_period.setdefault(per.period_id, []).append(d)
        for period_id, pdays in by_period.items():
            basket = db.load_basket(conn, world, period_id)
            if basket is None:
                continue  # basket not frozen yet: these days are computed when it is
            links = db.load_links(conn, world, period_id)
            prices = self.lake.scan(
                "daily_prices",
                world,
                start=min(pdays),
                end=max(pdays),
                columns=["server_id", "item_id", "day", "price"],
            ).filter(pl.col("day").is_in(pdays) & pl.col("price").is_not_null())
            vals = index.index_values(basket, prices, links, self.servers(world), self.divisions)
            hashes = pl.DataFrame(
                [(d, index.price_fingerprint(g)) for (d,), g in prices.group_by(["day"])],
                schema={"day": pl.Date, "input_hash": pl.String},
                orient="row",
            )
            sid = pl.DataFrame(
                [(sc, dv, v) for (sc, dv), v in series.items()],
                schema={"scope": pl.String, "division": pl.String, "series_id": pl.Int32},
                orient="row",
            )
            vals = (
                vals.join(sid, on=["scope", "division"], how="inner")
                .join(hashes, on="day", how="left")
                .with_columns(
                    period_id=pl.lit(period_id),
                    method_version=pl.lit(index.METHOD_VERSION),
                    input_hash=pl.col("input_hash").fill_null("0" * 16),
                    reason=pl.lit(REVISION_REASON[world]),
                )
            )
            report.index_written += db.publish_index(conn, world, vals)
            conn.commit()
        report.timings["index"] = report.timings.get("index", 0) + time.perf_counter() - t0
        return report

    # ------------------------------------------------------------------------------ patches + analytics
    def sync_patches(self, conn: psycopg.Connection, world: str) -> int:
        src = SOURCE[world]
        days = self.store.days(src, "patch_rss")
        if not days:
            return 0
        latest = max((r for r in self.store.iter_refs(src, "patch_rss", days[-1])), key=lambda r: r.path.name)
        notes = parse.parse_patch_rss(self.store.read(latest).body)
        rows = [
            {
                "world_id": world,
                "patch_id": n.patch_id,
                "released_at": n.released_at,
                "version": n.version,
                "title": n.title,
                "notes": n.notes,
                "tags": tag_divisions(f"{n.title} {n.notes}"),
                "is_major": n.is_major or (world == "synthetic" and "Version" in n.title),
                "source": "synthetic" if world == "synthetic" else "rss",
            }
            for n in notes
        ]
        n = db.upsert_patches(conn, rows)
        conn.commit()
        return n

    def run_analytics(self, conn: psycopg.Connection, world: str, series: dict[tuple[str, str], int]) -> dict[str, int]:
        t0 = time.perf_counter()
        ids = list(series.values())
        idx = db.load_index(conn, world)
        patches = db.load_patches(conn, world)
        series_division = {sid: (None if d == index.ALL else d) for (s, d), sid in series.items()}
        infl = analytics.inflation_rates(idx) if not idx.is_empty() else pl.DataFrame()
        shocks = analytics.detect_shocks(idx) if not idx.is_empty() else pl.DataFrame()
        attrib = analytics.attribute(shocks, series_division, patches) if not shocks.is_empty() else pl.DataFrame()
        impact = analytics.patch_impact(idx, patches) if not idx.is_empty() and patches else pl.DataFrame()
        daily = self.lake.scan("daily_prices", world)
        manip = analytics.manipulation_events(daily) if not daily.is_empty() else pl.DataFrame()
        if world == "synthetic":
            fills = self.lake.scan("fills", world)
            faucets = self.lake.scan("faucets", world)
            flows = analytics.money_flows(
                world, fills, None, faucets.rename({"amount": "amount"}) if not faucets.is_empty() else None
            )
        else:
            hist = self.lake.scan("history", world, columns=["server_id", "day", "average", "volume"])
            flows = analytics.money_flows(world, None, hist, None)
        servers = self.servers(world)
        out = {
            "inflation_rate": db.replace_rows(
                conn,
                "inflation_rate",
                "series_id = ANY(%s)",
                [ids],
                ["series_id", "day", "window_days", "rate", "annualized"],
                infl,
            ),
            "shock": db.replace_rows(
                conn,
                "shock",
                "series_id = ANY(%s)",
                [ids],
                ["series_id", "day", "log_change", "robust_z", "direction"],
                shocks,
            ),
            "shock_attribution": db.replace_rows(
                conn,
                "shock_attribution",
                "series_id = ANY(%s)",
                [ids],
                ["series_id", "day", "world_id", "patch_id", "lag_hours", "relevance", "rank"],
                attrib,
            ),
            "patch_impact": db.replace_rows(
                conn,
                "patch_impact",
                "world_id = %s",
                [world],
                ["world_id", "patch_id", "series_id", "log_change", "robust_z"],
                impact,
            ),
            "manipulation_event": db.replace_rows(
                conn,
                "manipulation_event",
                "server_id = ANY(%s)",
                [servers],
                ["server_id", "item_id", "day", "kind", "severity", "n_obs", "thin", "detail"],
                manip,
            ),
            "money_flow": db.replace_rows(
                conn,
                "money_flow",
                "server_id = ANY(%s)",
                [servers],
                ["server_id", "day", "kind", "amount", "method"],
                flows,
            ),
        }
        conn.commit()
        metrics.observe("analytics_seconds", time.perf_counter() - t0, world=world)
        return out

    # ------------------------------------------------------------------------------ one-shot run
    def run(self, conn: psycopg.Connection, world: str, days: list[date] | None = None) -> Report:
        series = self.sync_reference(conn)[world]
        report = Report(world)
        self.sync_patches(conn, world)
        todo = self.changed_days(conn, world) if days is None else sorted(days)
        self.process_days(conn, world, todo, report)
        self.index_days(conn, world, todo, series, report)
        t0 = time.perf_counter()
        self.run_analytics(conn, world, series)
        report.timings["analytics"] = time.perf_counter() - t0
        log(logger, logging.INFO, "pipeline run complete", **report.as_dict())
        return report


def _manifest(r: Any) -> dict[str, Any]:
    ref = r.ref
    return {
        "sha256": ref.sha256,
        "source": ref.source,
        "kind": ref.kind,
        "day": ref.day,
        "key": ref.key,
        "path": ref.path.as_posix(),
        "bytes": ref.path.stat().st_size,
        "fetched_at": r.fetched_at,
        "observed_at": r.observed_at,
    }
