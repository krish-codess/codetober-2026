"""Load a built run into Postgres, make it the one being served, and take that back.

A run is loaded and activated in one transaction, so the API serves either the old run or the new
one, never half of each. The previous run stays loaded: rollback is flipping one row back.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import duckdb
import psycopg

from wrapped import cards
from wrapped.build import _connect as warehouse
from wrapped.build import run_identity
from wrapped.config import Settings, log, warn

logger = logging.getLogger(__name__)


def connect(url: str, attempts: int = 6) -> psycopg.Connection[Any]:
    """Connect with exponential backoff, capped: 1, 2, 4, 8, 10 s. A database that is still starting is normal."""
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(url, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise
            delay = min(10.0, 2.0 ** (attempt - 1))
            warn(logger, "database not reachable, retrying", attempt=attempt, wait_seconds=delay, error=str(exc)[:200])
            time.sleep(delay)
    raise AssertionError("unreachable")


def publish(settings: Settings) -> dict[str, Any]:
    """Load the run built from the current warehouse and activate it. Safe to retry: a loaded run is not reloaded."""
    started = time.perf_counter()
    con = warehouse(settings)
    try:
        run_id, _ = run_identity(settings, con)
    finally:
        con.close()
    run_dir = settings.payload_dir / str(run_id)
    summary_path = run_dir / "run.json"
    if not summary_path.exists():
        raise SystemExit(f"run {run_id} has not been built; run `wrapped build` first")
    summary = json.loads(summary_path.read_text())

    with connect(settings.batch_database_url) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO card_types (card_type, family, shareable) VALUES (%s, %s, %s) "
            "ON CONFLICT (card_type) DO UPDATE SET family = EXCLUDED.family, shareable = EXCLUDED.shareable",
            [(name, family, shareable) for name, (family, shareable) in cards.CARD_TYPES.items()],
        )
        cur.execute("SELECT status FROM generation_runs WHERE run_id = %s FOR UPDATE", [run_id])
        existing = cur.fetchone()
        loaded = 0
        if existing is None or existing[0] != "ready":
            cur.execute("DELETE FROM generation_runs WHERE run_id = %s", [run_id])  # a half-loaded earlier attempt
            cur.execute(
                "INSERT INTO generation_runs (run_id, year, status, source_fingerprint, population) "
                "VALUES (%s, %s, 'loading', %s, %s)",
                [run_id, settings.year, summary["source_fingerprint"], summary["population"]],
            )
            loaded = _copy_payloads(cur, run_id, str(run_dir / "payloads.parquet"), settings)
            cur.execute(
                "UPDATE generation_runs SET status = 'ready', finished_at = now(), user_count = %s WHERE run_id = %s",
                [loaded, run_id],
            )
        cur.execute(
            """INSERT INTO active_runs (year, run_id) VALUES (%s, %s)
               ON CONFLICT (year) DO UPDATE
                   SET previous_run_id = active_runs.run_id, run_id = EXCLUDED.run_id, activated_at = now()
                   WHERE active_runs.run_id <> EXCLUDED.run_id""",
            [settings.year, run_id],
        )
        activated = cur.rowcount == 1
        # Keep the active run and the one before it; anything older for this year is dead weight.
        cur.execute(
            """DELETE FROM generation_runs g USING active_runs a
               WHERE g.year = a.year AND a.year = %s AND g.run_id NOT IN (a.run_id, coalesce(a.previous_run_id, a.run_id))""",
            [settings.year],
        )
        pruned = cur.rowcount
    result = {
        "run_id": str(run_id),
        "loaded": loaded,
        "activated": activated,
        "pruned_runs": pruned,
        "seconds": round(time.perf_counter() - started, 2),
    }
    log(logger, "publish finished", **result)
    return result


def _copy_payloads(cur: psycopg.Cursor[Any], run_id: object, parquet: str, settings: Settings) -> int:
    """Stream the run from Parquet into Postgres with COPY. Nothing is held in memory beyond one chunk."""
    src = duckdb.connect(config={"memory_limit": settings.duckdb_memory})
    try:
        count = 0
        rows = src.execute("SELECT user_id, login, tier, payload FROM read_parquet(?)", [parquet])
        with cur.copy("COPY wrapped_payloads (run_id, user_id, login, tier, payload) FROM STDIN") as copy:
            while chunk := rows.fetchmany(5_000):
                for row in chunk:
                    copy.write_row((run_id, *row))
                count += len(chunk)
        rows = src.execute(
            "SELECT user_id, unnest(range(len(card_types))) AS position, unnest(card_types) AS card_type "
            "FROM read_parquet(?)",
            [parquet],
        )
        with cur.copy("COPY payload_cards (run_id, user_id, position, card_type) FROM STDIN") as copy:
            while chunk := rows.fetchmany(20_000):
                for row in chunk:
                    copy.write_row((run_id, *row))
        return count
    finally:
        src.close()


def rollback(settings: Settings) -> dict[str, Any]:
    """Serve the previous run again. Running it twice puts the newer run back."""
    with connect(settings.batch_database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE active_runs SET run_id = previous_run_id, previous_run_id = run_id, activated_at = now()
               WHERE year = %s AND previous_run_id IS NOT NULL
               RETURNING run_id, previous_run_id""",
            [settings.year],
        )
        row = cur.fetchone()
    if row is None:
        raise SystemExit(f"nothing to roll back to for {settings.year}: no previous run is loaded")
    result = {"now_serving": str(row[0]), "rolled_back_from": str(row[1])}
    log(logger, "rollback finished", **result)
    return result
