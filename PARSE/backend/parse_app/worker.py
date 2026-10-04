"""Background worker: runs queued jobs and schedules retraining.

Failure behaviour: a failed job is retried with backoff (30s, 120s) up to MAX_ATTEMPTS, then
marked failed with its error; the API and the labelling queue keep serving the last good model
throughout. A job left 'running' by a crashed worker is re-queued on the next start.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

from sqlalchemy import Engine, text

from .config import Settings, get_settings
from .db import get_engine
from .log import correlation_id, event, metrics, setup_logging
from .pipeline import stage_embed
from .store import active_model, dumps, enqueue_job, set_progress
from .train import NotEnoughLabels, score_pool, train

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
POLL_S = 3.0


def run_job(engine: Engine, settings: Settings, job: Any) -> dict[str, Any]:
    if job.kind == "retrain":
        return train(engine, settings, job_id=job.id)
    if job.kind == "embed_score":
        set_progress(engine, job.id, 0.1, "embedding new feedback")
        embedded = stage_embed(engine, settings)
        set_progress(engine, job.id, 0.7, "scoring with the active model")
        with engine.connect() as conn:
            current = active_model(conn)
        return {"embedded": embedded, "scored": score_pool(engine, *current) if current else 0}
    raise ValueError(f"unknown job kind {job.kind!r}")


def claim(engine: Engine) -> Any | None:
    """Take the oldest runnable job. SKIP LOCKED makes this safe with several workers."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                """UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1, stage = 'starting'
                   WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND requested_at <= now()
                               ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING id, kind, attempts"""
            )
        ).one_or_none()


def work_one(engine: Engine, settings: Settings) -> bool:
    job = claim(engine)
    if job is None:
        return False
    correlation_id.set(f"job-{job.id}")
    event(logger, "job_started", job_id=job.id, kind=job.kind, attempt=job.attempts)
    try:
        result = run_job(engine, settings, job)
    except NotEnoughLabels as e:  # not a fault and not worth retrying: say so and stop
        _finish(engine, job.id, "failed", error=str(e))
        return True
    except Exception as e:  # noqa: BLE001 - a job must never take the worker down
        metrics.inc("job_failures_total", kind=job.kind)
        if job.attempts < MAX_ATTEMPTS:
            delay = 30 * 4 ** (job.attempts - 1)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """UPDATE jobs SET status = 'queued', stage = 'waiting to retry', error = :e,
                           requested_at = now() + make_interval(secs => :d) WHERE id = :id"""
                    ),
                    {"e": f"{type(e).__name__}: {e}"[:2000], "d": delay, "id": job.id},
                )
            event(logger, "job_retry_scheduled", logging.WARNING, job_id=job.id, delay_s=delay, error=str(e))
        else:
            _finish(engine, job.id, "failed", error=f"{type(e).__name__}: {e}"[:2000])
            logger.exception("job_failed")
        return True
    _finish(engine, job.id, "succeeded", result=result)
    return True


def _finish(engine: Engine, job_id: int, status: str, *, result: Any = None, error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE jobs SET status = :s, finished_at = now(), progress = CASE WHEN :s = 'succeeded' THEN 1 ELSE progress END,
                   stage = :s, result = CAST(:r AS jsonb), error = :e WHERE id = :id"""
            ),
            {"s": status, "r": dumps(result) if result is not None else None, "e": error, "id": job_id},
        )
    event(logger, "job_finished", job_id=job_id, status=status, error=error)


def schedule_retrain(engine: Engine, settings: Settings) -> dict[str, Any] | None:
    """Enqueue a retrain when enough has changed since the active model: new labels, or a new
    taxonomy version. The idempotency key encodes that state, so repeated ticks enqueue one job."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT (SELECT max(id) FROM taxonomy_versions) AS tv,
                          (SELECT count(*) FROM annotations a JOIN feedback f ON f.id = a.feedback_id
                           WHERE f.split = 'pool' AND a.source <> 'gold') AS n_labeled,
                          m.id AS model_id, m.n_labeled AS model_n, m.taxonomy_version AS model_tv
                   FROM (SELECT 1) one LEFT JOIN model_versions m ON m.status = 'active'"""
            )
        ).one()
        if (
            row.model_id is not None
            and row.tv == row.model_tv
            and row.n_labeled - row.model_n < settings.retrain_min_new_labels
        ):
            return None
        # bucket the label count so that labelling during a run does not enqueue a job per label
        bucket = row.n_labeled // max(settings.retrain_min_new_labels, 1)
        return enqueue_job(conn, "retrain", f"scheduled:tv{row.tv}:b{bucket}", "scheduler")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = get_engine()
    stop = False

    def _stop(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # ponytail: single worker assumed here. With several, replace this with a heartbeat column
    # and re-queue only jobs whose heartbeat is stale.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET status = 'queued', stage = 're-queued after restart' WHERE status = 'running'")
        )
    event(logger, "worker_started", retrain_interval_s=settings.retrain_interval_s)
    next_tick = 0.0
    while not stop:
        try:
            if time.monotonic() >= next_tick:
                correlation_id.set("scheduler")
                job = schedule_retrain(engine, settings)
                if job and job["status"] == "queued":
                    event(logger, "retrain_scheduled", job_id=job["id"])
                next_tick = time.monotonic() + settings.retrain_interval_s
            if not work_one(engine, settings):
                time.sleep(POLL_S)
        except Exception:  # noqa: BLE001 - e.g. database restarting: log, back off, keep going
            logger.exception("worker_loop_error")
            time.sleep(10)
    event(logger, "worker_stopped")


if __name__ == "__main__":
    main()
