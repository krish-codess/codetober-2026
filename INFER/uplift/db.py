"""PostgreSQL access. Every statement in this package is parameterized; nothing is formatted into SQL."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from uplift.config import warn

logger = logging.getLogger("uplift.db")

RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)  # capped exponential backoff: at most 15.5 s, then give up loudly


def connect(url: str, delays: tuple[float, ...] = RETRY_DELAYS) -> psycopg.Connection[dict[str, Any]]:
    """Connect for a batch job, riding out a database that is still starting. Rows come back as dicts."""
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if delay is None:
                raise SystemExit(
                    f"database unreachable after {attempt} attempts: {str(exc).strip().splitlines()[0]}"
                ) from exc
            warn(logger, "database not reachable, retrying", attempt=attempt, retry_in_seconds=delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def data_version(conn: psycopg.Connection[Any], dataset_id: int) -> str:
    """One hash for 'exactly these raw files'. A model trained on a different set of files is stale."""
    rows = conn.execute("SELECT sha256 FROM raw_file WHERE dataset_id = %s ORDER BY sha256", [dataset_id]).fetchall()
    return hashlib.sha256("".join(r["sha256"] for r in rows).encode()).hexdigest()
