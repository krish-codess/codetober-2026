"""Dagster definitions: the orchestration graph over goldstandard.pipeline.

Per world W (eve, synthetic):

    [ingest jobs, scheduled] --raw store--> W_item_prices [daily] --> W_index [daily] --+
                                            W_patch_events -----------------------------+--> W_analytics

* W_item_prices / W_index are daily-partitioned with a single-run backfill policy, so a backfill of
  any date range is one run that processes exactly those days.
* The W_changed_partitions sensor compares raw-input fingerprints with what was last processed and
  requests runs only for affected day ranges (late arrivals, ESI revisions); unchanged days are
  never recomputed. A run for an unchanged day publishes nothing (vintage logic is idempotent).
* W_analytics recomputes after every successful daily run (cheap; whole-world derived tables).
* Ingestion ops record an ingest_run row (ok / degraded / failed) that /health/ready reports.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import dagster as dg

from goldstandard import db
from goldstandard.config import settings
from goldstandard.obs import configure_logging, correlation_id
from goldstandard.pipeline import WORLDS, Pipeline
from goldstandard.raw import RawStore
from goldstandard.sources import eve, synthetic

RETRY = dg.RetryPolicy(max_retries=3, delay=15, backoff=dg.Backoff.EXPONENTIAL, jitter=dg.Jitter.PLUS_MINUS)
RANGE_START, RANGE_END = "dagster/asset_partition_range_start", "dagster/asset_partition_range_end"


def _partitions() -> dg.DailyPartitionsDefinition:
    return dg.DailyPartitionsDefinition(start_date=settings().synth_start.isoformat(), timezone="UTC")


def _days(context: dg.AssetExecutionContext, pipe: Pipeline, world: str) -> list[date]:
    window = context.partition_time_window
    start, end = window.start.date(), window.end.date()
    have = set(pipe.candidate_days(world))
    return [start + timedelta(days=i) for i in range((end - start).days) if start + timedelta(days=i) in have]


def _context_correlation(context: dg.AssetExecutionContext | dg.OpExecutionContext) -> None:
    configure_logging(settings().log_level, settings().log_json)
    correlation_id.set(context.run_id[:16])


def build_world(world: str) -> tuple[list[dg.AssetsDefinition], list[Any], list[Any]]:
    parts = _partitions()

    @dg.asset(
        name=f"{world}_item_prices",
        group_name=world,
        partitions_def=parts,
        retry_policy=RETRY,
        backfill_policy=dg.BackfillPolicy.single_run(),
        kinds={"polars", "parquet", "postgres"},
        description="Validated raw -> lake partitions -> robust daily prices (published, vintaged).",
    )
    def item_prices(context: dg.AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]
        _context_correlation(context)
        pipe = Pipeline(settings())
        if world == "eve":
            pipe.stage_history()
        days = _days(context, pipe, world)
        with db.connect(pipe.cfg) as conn:
            pipe.sync_reference(conn)
            report = pipe.process_days(conn, world, days)
        return dg.MaterializeResult(
            metadata={
                "days": len(days),
                "prices_written": report.prices_written,
                "quarantined": report.quarantined,
                "seconds": round(report.timings.get("prices", 0.0), 2),
            }
        )

    @dg.asset(
        name=f"{world}_index",
        group_name=world,
        partitions_def=parts,
        deps=[item_prices],
        retry_policy=RETRY,
        backfill_policy=dg.BackfillPolicy.single_run(),
        kinds={"postgres"},
        description="Frozen quarterly baskets + chain-linked Laspeyres index per server/division (vintaged).",
    )
    def index_values(context: dg.AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]
        _context_correlation(context)
        pipe = Pipeline(settings())
        days = _days(context, pipe, world)
        with db.connect(pipe.cfg) as conn:
            series = pipe.sync_reference(conn)[world]
            report = pipe.index_days(conn, world, days, series)
        return dg.MaterializeResult(
            metadata={
                "days": len(days),
                "index_values_written": report.index_written,
                "baskets_frozen": ", ".join(report.baskets_frozen) or "none",
            }
        )

    @dg.asset(
        name=f"{world}_patch_events",
        group_name=world,
        retry_policy=RETRY,
        kinds={"postgres"},
        description="Patch notes parsed from the (real or synthetic) RSS feed, tagged with CPI divisions.",
    )
    def patch_events(context: dg.AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]
        _context_correlation(context)
        pipe = Pipeline(settings())
        with db.connect(pipe.cfg) as conn:
            pipe.sync_reference(conn)
            n = pipe.sync_patches(conn, world)
        return dg.MaterializeResult(metadata={"new_patches": n})

    @dg.asset(
        name=f"{world}_analytics",
        group_name=world,
        deps=[index_values, patch_events],
        retry_policy=RETRY,
        kinds={"polars", "postgres"},
        description="Inflation rates, shocks + patch attribution, patch impact, manipulation events, money flows.",
    )
    def world_analytics(context: dg.AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]
        _context_correlation(context)
        pipe = Pipeline(settings())
        with db.connect(pipe.cfg) as conn:
            series = pipe.sync_reference(conn)[world]
            counts = pipe.run_analytics(conn, world, series)
        return dg.MaterializeResult(metadata={k: v for k, v in counts.items()})

    daily_job = dg.define_asset_job(f"{world}_daily_job", selection=[item_prices, index_values], partitions_def=parts)
    analytics_job = dg.define_asset_job(f"{world}_analytics_job", selection=[patch_events, world_analytics])

    @dg.sensor(
        name=f"{world}_changed_partitions",
        job=daily_job,
        minimum_interval_seconds=300,
        default_status=dg.DefaultSensorStatus.RUNNING,
    )
    def changed(context: dg.SensorEvaluationContext) -> dg.SensorResult | dg.SkipReason:
        pipe = Pipeline(settings())
        with db.connect(pipe.cfg) as conn:
            days = pipe.changed_days(conn, world)
        if not days:
            return dg.SkipReason("no partition has new or changed inputs")
        requests = []
        for start, end in _ranges(days):
            fp = hashlib.sha256("".join(pipe.day_fingerprint(world, d) for d in _span(start, end)).encode()).hexdigest()
            requests.append(
                dg.RunRequest(
                    run_key=f"{world}:{start}:{end}:{fp[:16]}",
                    tags={RANGE_START: start.isoformat(), RANGE_END: end.isoformat()},
                )
            )
        return dg.SensorResult(run_requests=requests)

    @dg.run_status_sensor(
        name=f"{world}_analytics_after_daily",
        run_status=dg.DagsterRunStatus.SUCCESS,
        monitored_jobs=[daily_job],
        request_job=analytics_job,
        default_status=dg.DefaultSensorStatus.RUNNING,
    )
    def after_daily(context: dg.RunStatusSensorContext) -> dg.RunRequest:
        return dg.RunRequest(run_key=f"analytics-after-{context.dagster_run.run_id}")

    return (
        [item_prices, index_values, patch_events, world_analytics],
        [daily_job, analytics_job],
        [changed, after_daily],
    )


def _span(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _ranges(days: list[date]) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for d in sorted(days):
        if out and d == out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


# ------------------------------------------------------------------------------------- ingestion
def _ingest(context: dg.OpExecutionContext, job: str, fn: Any) -> dict[str, Any]:
    _context_correlation(context)
    cfg = settings()
    run_id = str(uuid.uuid4())
    with db.connect(cfg) as conn:
        db.start_ingest_run(conn, run_id, job)
        try:
            report: dict[str, Any] = fn(cfg, RawStore(cfg.raw_dir))
        except Exception as exc:
            db.finish_ingest_run(conn, run_id, {"requested": 1, "failed": 1, "error": repr(exc)})
            raise
        db.finish_ingest_run(conn, run_id, report)
    context.log.info(f"{job}: {report}")
    if report.get("requested") and report.get("failed") == report.get("requested"):
        raise dg.Failure(f"{job}: every request failed", metadata={"report": dg.MetadataValue.json(report)})
    return report


@dg.op(retry_policy=RETRY)
def synthetic_tick(context: dg.OpExecutionContext) -> None:
    def fn(cfg: Any, store: RawStore) -> dict[str, Any]:
        sc = synthetic.SynthConfig(
            start=cfg.synth_start,
            servers=cfg.synth_servers,
            seed=cfg.synth_seed,
            snapshots_per_day=cfg.synth_snapshots_per_day,
        )
        stats = synthetic.generate(
            sc, store, cfg.reference_dir, datetime.now(UTC), truth_dir=cfg.lake_dir / "synthetic_truth"
        )
        return {"requested": stats["bundles_written"], "stored": stats["bundles_written"], "failed": 0, **stats}

    _ingest(context, "synthetic_feed", fn)


@dg.op(retry_policy=RETRY)
def eve_orders(context: dg.OpExecutionContext) -> None:
    _ingest(context, "eve_orders", lambda cfg, store: eve.ingest_order_snapshot(cfg, store).as_dict())


@dg.op(retry_policy=RETRY)
def eve_history(context: dg.OpExecutionContext) -> None:
    _ingest(context, "eve_history", lambda cfg, store: eve.ingest_history(cfg, store).as_dict())


@dg.op(retry_policy=RETRY)
def eve_patches_and_meta(context: dg.OpExecutionContext) -> None:
    _ingest(context, "eve_patch_notes", lambda cfg, store: eve.ingest_patch_notes(cfg, store).as_dict())
    _ingest(context, "eve_type_meta", lambda cfg, store: eve.ingest_type_metadata(cfg, store).as_dict())


@dg.job(description="Advance the simulated auction-house feed to now (simulates the live API).")
def synthetic_feed_job() -> None:
    synthetic_tick()


@dg.job(description="Snapshot live EVE order books for every (region, item).")
def eve_orders_job() -> None:
    eve_orders()


@dg.job(description="Fetch EVE daily market history (after downtime) + patch notes + item metadata.")
def eve_daily_ingest_job() -> None:
    eve_history()
    eve_patches_and_meta()


ASSETS: list[dg.AssetsDefinition] = []
JOBS: list[Any] = [synthetic_feed_job, eve_orders_job, eve_daily_ingest_job]
SENSORS: list[Any] = []
for _w in WORLDS:
    _a, _j, _s = build_world(_w)
    ASSETS += _a
    JOBS += _j
    SENSORS += _s

defs = dg.Definitions(
    assets=ASSETS,
    jobs=JOBS,
    sensors=SENSORS,
    schedules=[
        dg.ScheduleDefinition(
            job=synthetic_feed_job,
            cron_schedule="5 */6 * * *",
            execution_timezone="UTC",
            default_status=dg.DefaultScheduleStatus.RUNNING,
        ),
        dg.ScheduleDefinition(
            job=eve_orders_job,
            cron_schedule="15 */6 * * *",
            execution_timezone="UTC",
            default_status=dg.DefaultScheduleStatus.RUNNING,
        ),
        # ESI publishes the previous day's history after the 11:00 UTC downtime
        dg.ScheduleDefinition(
            job=eve_daily_ingest_job,
            cron_schedule="45 11 * * *",
            execution_timezone="UTC",
            default_status=dg.DefaultScheduleStatus.RUNNING,
        ),
    ],
)
