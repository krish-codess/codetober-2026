"""PostgreSQL: connection with bounded retry, migrations, and every query the API runs.

All values travel as bound parameters. The only interpolated SQL is the migration
files themselves and fixed column lists.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

log = logging.getLogger("tydlc.store")
MIGRATIONS = Path(__file__).parent / "migrations"
Row = dict[str, Any]


def connect(dsn: str, attempts: int = 5) -> psycopg.Connection[Any]:
    """Connect, retrying a database that is still starting: 0.5 s, 1 s, 2 s, 4 s, then give up."""
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise
            delay = min(0.5 * 2 ** (attempt - 1), 8.0)
            log.warning("postgres unavailable, retrying",
                        extra={"attempt": attempt, "delay_s": delay, "error": str(exc).strip()})
            time.sleep(delay)
    raise AssertionError("unreachable")


def _query(conn: psycopg.Connection[Any], sql: str, params: Any = ()) -> list[Row]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


# --- migrations ---------------------------------------------------------------------

def _versions() -> list[int]:
    return sorted(int(p.name[:4]) for p in MIGRATIONS.glob("*.up.sql"))


def current_version(conn: psycopg.Connection[Any]) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    return int(_query(conn, "SELECT coalesce(max(version), 0) AS v FROM schema_migrations")[0]["v"])


def migrate(conn: psycopg.Connection[Any], target: int | None = None) -> int:
    """Move the schema to `target` (default: latest), applying up or down files in order.
    Each file runs in its own transaction together with its bookkeeping row."""
    current = current_version(conn)
    target = _versions()[-1] if target is None else target
    steps = ([(v, "up") for v in _versions() if current < v <= target] or
             [(v, "down") for v in reversed(_versions()) if target < v <= current])
    for version, direction in steps:
        (path,) = MIGRATIONS.glob(f"{version:04d}_*.{direction}.sql")
        with conn.transaction():
            conn.execute(path.read_text())
            if direction == "up":
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            else:
                conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        log.info("migrated", extra={"version": version, "direction": direction})
    return target


# --- writes --------------------------------------------------------------------------

def create_run(conn: psycopg.Connection[Any], key: str, subject: str, engine: str, seed: int,
               max_examples: int, git_sha: str | None = None) -> tuple[Row, bool]:
    """Insert a run, or return the existing one for this idempotency key. -> (run, created)"""
    rows = _query(conn, "INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples,"
                        " git_sha) VALUES (%s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                  (key, subject, engine, seed, max_examples, git_sha))
    if rows:
        return rows[0], True
    return _query(conn, "SELECT * FROM runs WHERE idempotency_key = %s", (key,))[0], False


def finish_run(conn: psycopg.Connection[Any], run_id: int, report: Row) -> None:
    """Record a report atomically. Only a 'running' run is finished, so a retry is a no-op."""
    with conn.transaction():
        updated = _query(conn, """
            UPDATE runs SET status = 'completed', finished_at = now(), examples = %s,
                   rows_generated = %s, duration_ms = %s, candidates = %s, candidates_held = %s,
                   gate_ok = %s
            WHERE id = %s AND status = 'running' RETURNING id""",
            (report["examples"], report["rows_generated"], report["timings_ms"]["total"],
             report["discovery"]["candidates"], report["discovery"]["held"], report["ok"], run_id))
        if not updated:
            return
        for p in report["properties"]:
            pid = _query(conn, """
                INSERT INTO properties (subject, name, source, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (subject, name) DO UPDATE SET description = EXCLUDED.description
                RETURNING id""",
                (report["subject"], p["name"], p["source"], p["description"]))[0]["id"]
            conn.execute("INSERT INTO property_results (run_id, property_id, passed, failed,"
                         " vacuous, status, confidence, known_bug)"
                         " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                         (run_id, pid, p["passed"], p["failed"], p["vacuous"], p["status"],
                          p["confidence"], p["known_bug"]))
            if f := p["failure"]:
                conn.execute("INSERT INTO failures (run_id, property_id, minimal_dataset,"
                             " minimal_rows, shrunk, shrink_calls, shrink_ms)"
                             " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                             (run_id, pid, Jsonb(f["minimal_dataset"]), f["minimal_rows"],
                              f["shrunk"], f["shrink_calls"], f["shrink_ms"]))


def fail_run(conn: psycopg.Connection[Any], run_id: int, error: str) -> None:
    conn.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = %s"
                 " WHERE id = %s AND status = 'running'", (error[:2000], run_id))


def fail_abandoned_runs(conn: psycopg.Connection[Any]) -> None:
    """A run still 'running' after an hour belonged to a process that died."""
    conn.execute("UPDATE runs SET status = 'failed', finished_at = now(),"
                 " error = 'abandoned: the process running it stopped'"
                 " WHERE status = 'running' AND started_at < now() - interval '1 hour'")


# --- reads (keyset pagination: opaque cursor over a unique, stable sort key) ----------

class BadCursor(ValueError):
    pass


def encode_cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def decode_cursor(cursor: str | None, kind: type) -> Any:
    if cursor is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except ValueError as exc:  # binascii.Error and JSONDecodeError are ValueErrors
        raise BadCursor("cursor is not valid") from exc
    if type(value) is not kind:
        raise BadCursor("cursor is not valid")
    return value


def _page(rows: list[Row], limit: int, key: str) -> Row:
    more = len(rows) > limit
    items = rows[:limit]
    return {"items": items, "next_cursor": encode_cursor(items[-1][key]) if more else None}


def list_runs(conn: psycopg.Connection[Any], cursor: str | None, limit: int,
              engine: str | None = None) -> Row:
    before = decode_cursor(cursor, int)
    rows = _query(conn, """
        SELECT * FROM runs
        WHERE (%(before)s::bigint IS NULL OR id < %(before)s)
          AND (%(engine)s::text IS NULL OR engine = %(engine)s)
        ORDER BY id DESC LIMIT %(n)s""", {"before": before, "engine": engine, "n": limit + 1})
    return _page(rows, limit, "id")


def get_run(conn: psycopg.Connection[Any], run_id: int) -> Row | None:
    rows = _query(conn, "SELECT * FROM runs WHERE id = %s", (run_id,))
    return rows[0] if rows else None


def catalog(conn: psycopg.Connection[Any], run_id: int, cursor: str | None, limit: int) -> Row:
    """One run's invariants, each with its record over the previous 20 runs on that engine."""
    after = decode_cursor(cursor, str)
    rows = _query(conn, """
        SELECT p.name, p.source, p.description, r.passed, r.failed, r.vacuous, r.status,
               r.confidence, r.known_bug, f.id AS failure_id, f.minimal_rows,
               h.runs AS history_runs, h.falsified AS history_falsified
        FROM property_results r
        JOIN properties p ON p.id = r.property_id
        JOIN runs this ON this.id = r.run_id
        LEFT JOIN failures f ON f.run_id = r.run_id AND f.property_id = r.property_id
        CROSS JOIN LATERAL (
            SELECT count(*) AS runs, count(*) FILTER (WHERE x.status = 'falsified') AS falsified
            FROM (SELECT pr.status
                  FROM property_results pr JOIN runs ru ON ru.id = pr.run_id
                  WHERE pr.property_id = r.property_id AND pr.run_id <= r.run_id
                    AND ru.engine = this.engine
                  ORDER BY pr.run_id DESC LIMIT 20) x) h
        WHERE r.run_id = %(run)s AND (%(after)s::text IS NULL OR p.name > %(after)s)
        ORDER BY p.name LIMIT %(n)s""", {"run": run_id, "after": after, "n": limit + 1})
    return _page(rows, limit, "name")


_FAILURE_COLUMNS = """
    SELECT f.id, f.run_id, p.name AS property, p.source, p.description, f.minimal_rows,
           f.shrunk, f.shrink_calls, f.shrink_ms, r.known_bug, r.passed, r.failed,
           ru.engine, ru.seed, ru.max_examples, ru.started_at"""
_FAILURE_FROM = """
    FROM failures f
    JOIN properties p ON p.id = f.property_id
    JOIN property_results r ON r.run_id = f.run_id AND r.property_id = f.property_id
    JOIN runs ru ON ru.id = f.run_id
"""
# Both statements are assembled from the constants above; nothing external is spliced in.
_FAILURE_LIST = _FAILURE_COLUMNS + _FAILURE_FROM + """
    WHERE (%(before)s::bigint IS NULL OR f.id < %(before)s)
      AND (%(run)s::bigint IS NULL OR f.run_id = %(run)s)
      AND (%(prop)s::text IS NULL OR f.property_id =
           (SELECT id FROM properties WHERE name = %(prop)s LIMIT 1))
    ORDER BY f.id DESC LIMIT %(n)s"""
_FAILURE_ONE = _FAILURE_COLUMNS + ", f.minimal_dataset" + _FAILURE_FROM + " WHERE f.id = %s"


def list_failures(conn: psycopg.Connection[Any], cursor: str | None, limit: int,
                  run_id: int | None = None, property_name: str | None = None) -> Row:
    before = decode_cursor(cursor, int)
    rows = _query(conn, _FAILURE_LIST,
                  {"before": before, "run": run_id, "prop": property_name, "n": limit + 1})
    return _page(rows, limit, "id")


def get_failure(conn: psycopg.Connection[Any], failure_id: int) -> Row | None:
    rows = _query(conn, _FAILURE_ONE, (failure_id,))
    return rows[0] if rows else None


def failure_frequency(conn: psycopg.Connection[Any], engine: str, runs: int) -> list[Row]:
    """Per property over the last `runs` completed runs on `engine`: how often a generated
    example violated it, and in how many runs it was falsified at all."""
    return _query(conn, """
        WITH recent AS (SELECT id FROM runs WHERE engine = %s AND status = 'completed'
                        ORDER BY id DESC LIMIT %s)
        SELECT p.name, p.source, count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'falsified') AS falsified_runs,
               sum(r.failed) AS failed_examples, sum(r.passed + r.failed) AS judged_examples,
               round(sum(r.failed)::numeric / nullif(sum(r.passed + r.failed), 0), 4)::float
                   AS failure_frequency
        FROM property_results r
        JOIN recent ON recent.id = r.run_id
        JOIN properties p ON p.id = r.property_id
        GROUP BY p.id
        HAVING sum(r.failed) > 0
        ORDER BY failure_frequency DESC, p.name""", (engine, runs))


def discovery_hit_rate(conn: psycopg.Connection[Any], engine: str, runs: int) -> list[Row]:
    """Per run: of the candidates inferred from seed data, the share that survived."""
    return _query(conn, """
        SELECT id AS run_id, started_at, seed, max_examples, candidates, candidates_held,
               round(candidates_held::numeric / nullif(candidates, 0), 4)::float AS hit_rate
        FROM runs WHERE engine = %s AND status = 'completed'
        ORDER BY id DESC LIMIT %s""", (engine, runs))
