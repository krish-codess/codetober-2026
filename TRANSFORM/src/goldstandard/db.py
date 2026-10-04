"""PostgreSQL access for the pipeline (role gs_pipeline). Every statement is parameterised.

Publishing rules live in SQL, next to the data they protect:
  * item_price_daily / index_value are append-only (trigger). Publishing stages the new values
    and inserts vintage = current + 1 ONLY where the value differs from the current vintage, so
    re-running a partition with unchanged inputs writes nothing (idempotent), and a changed value
    becomes a new, visible vintage with a reason - never a silent overwrite.
  * publishes for one world are serialised with a transaction-scoped advisory lock, so two
    concurrent retries cannot both claim the same vintage number.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from typing import Any

import polars as pl
import psycopg
from psycopg import sql

from goldstandard.config import Settings


@contextmanager
def connect(cfg: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(
        cfg.database_url.get_secret_value(), connect_timeout=10, application_name="goldstandard-pipeline"
    ) as conn:
        yield conn


def _lock(conn: psycopg.Connection, world_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"publish:{world_id}",))


def _copy(
    conn: psycopg.Connection, table: str, columns: Sequence[str], rows: Iterator[tuple[Any, ...]] | list[Any]
) -> None:
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
    )
    with conn.cursor().copy(stmt) as cp:
        for r in rows:
            cp.write_row(r)


# ------------------------------------------------------------------------------------ reference data
def upsert_reference(conn: psycopg.Connection, ref: dict[str, list[dict[str, Any]]]) -> None:
    with conn.transaction():
        for w in ref["worlds"]:
            conn.execute(
                """INSERT INTO world (world_id, name, price_source, currency, is_synthetic)
                            VALUES (%(world_id)s, %(name)s, %(price_source)s, %(currency)s, %(is_synthetic)s)
                            ON CONFLICT (world_id) DO UPDATE SET name = EXCLUDED.name""",
                w,
            )
        for s in ref["servers"]:
            conn.execute(
                """INSERT INTO server (server_id, world_id, name, region_id)
                            VALUES (%(server_id)s, %(world_id)s, %(name)s, %(region_id)s)
                            ON CONFLICT (server_id) DO UPDATE SET name = EXCLUDED.name""",
                s,
            )
        for d in ref["divisions"]:
            conn.execute(
                """INSERT INTO division (division_id, label, keywords) VALUES (%(division_id)s, %(label)s, %(keywords)s)
                            ON CONFLICT (division_id) DO UPDATE SET label = EXCLUDED.label, keywords = EXCLUDED.keywords""",
                d,
            )
        for i in ref["items"]:
            conn.execute(
                """INSERT INTO item (item_id, name, division_id, esi_group, esi_category, volume_m3)
                            VALUES (%(item_id)s, %(name)s, %(division_id)s, %(esi_group)s, %(esi_category)s, %(volume_m3)s)
                            ON CONFLICT (item_id) DO UPDATE SET name = EXCLUDED.name, esi_group = EXCLUDED.esi_group,
                              esi_category = EXCLUDED.esi_category, volume_m3 = EXCLUDED.volume_m3""",
                i,
            )
        for a in ref["activities"]:
            conn.execute(
                """INSERT INTO activity (activity_id, world_id, label) VALUES (%(activity_id)s, %(world_id)s, %(label)s)
                            ON CONFLICT (activity_id) DO UPDATE SET label = EXCLUDED.label""",
                a,
            )
            for r in a["rates"]:
                conn.execute(
                    """INSERT INTO activity_rate (activity_id, effective_from, isk_per_hour) VALUES (%s, %s, %s)
                                ON CONFLICT (activity_id, effective_from) DO UPDATE SET isk_per_hour = EXCLUDED.isk_per_hour""",
                    (a["activity_id"], r["effective_from"], r["isk_per_hour"]),
                )
            for y in a["yields"]:
                conn.execute(
                    """INSERT INTO activity_yield (activity_id, item_id, qty_per_hour) VALUES (%s, %s, %s)
                                ON CONFLICT (activity_id, item_id) DO UPDATE SET qty_per_hour = EXCLUDED.qty_per_hour""",
                    (a["activity_id"], y["type_id"], y["qty_per_hour"]),
                )


def ensure_series(
    conn: psycopg.Connection, world_id: str, servers: list[str], divisions: list[str]
) -> dict[tuple[str, str], int]:
    """(scope, division) -> series_id, creating missing series. scope/division 'all' map to NULL."""
    with conn.transaction():
        for s in [*servers, None]:
            for d in [*divisions, None]:
                conn.execute(
                    """INSERT INTO index_series (world_id, server_id, division_id) VALUES (%s, %s, %s)
                                ON CONFLICT (world_id, server_id, division_id) DO NOTHING""",
                    (world_id, s, d),
                )
    rows = conn.execute(
        "SELECT series_id, server_id, division_id FROM index_series WHERE world_id = %s", (world_id,)
    ).fetchall()
    return {(s or "all", d or "all"): int(i) for i, s, d in rows}


# ------------------------------------------------------------------------------------ provenance
def register_raw(conn: psycopg.Connection, refs: list[dict[str, Any]]) -> int:
    if not refs:
        return 0
    with conn.transaction():
        cur = conn.cursor()
        cur.executemany(
            """INSERT INTO raw_manifest (sha256, source, kind, day, key, path, bytes, fetched_at, observed_at)
                           VALUES (%(sha256)s, %(source)s, %(kind)s, %(day)s, %(key)s, %(path)s, %(bytes)s,
                                   %(fetched_at)s, %(observed_at)s)
                           ON CONFLICT (sha256) DO NOTHING""",
            refs,
        )
        return cur.rowcount


def get_fingerprints(conn: psycopg.Connection, world_id: str) -> dict[date, str]:
    rows = conn.execute(
        "SELECT day, raw_fingerprint FROM data_quality_daily WHERE world_id = %s", (world_id,)
    ).fetchall()
    return {d: f for d, f in rows}


def upsert_quality(conn: psycopg.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO data_quality_daily (world_id, day, n_payloads, n_late_payloads, n_rows, n_valid,
                        n_quarantined, reasons, raw_fingerprint, computed_at)
                    VALUES (%(world_id)s, %(day)s, %(n_payloads)s, %(n_late_payloads)s, %(n_rows)s, %(n_valid)s,
                        %(n_quarantined)s, %(reasons)s, %(raw_fingerprint)s, now())
                    ON CONFLICT (world_id, day) DO UPDATE SET n_payloads = EXCLUDED.n_payloads,
                        n_late_payloads = EXCLUDED.n_late_payloads, n_rows = EXCLUDED.n_rows, n_valid = EXCLUDED.n_valid,
                        n_quarantined = EXCLUDED.n_quarantined, reasons = EXCLUDED.reasons,
                        raw_fingerprint = EXCLUDED.raw_fingerprint, computed_at = now()""",
        {**row, "reasons": json.dumps(row["reasons"])},
    )


# ------------------------------------------------------------------------------------ vintaged publishing
PRICE_COLS = ("server_id", "item_id", "day", "price", "volume", "n_obs", "status", "method", "input_hash", "reason")


def publish_prices(conn: psycopg.Connection, world_id: str, df: pl.DataFrame) -> int:
    """Append a new vintage for every (server, item, day) whose published value changed. Returns rows written."""
    if df.is_empty():
        return 0
    with conn.transaction():
        _lock(conn, world_id)
        conn.execute("""CREATE TEMP TABLE stage_price (server_id text, item_id bigint, day date, price double precision,
                          volume double precision, n_obs integer, status text, method text, input_hash text, reason text)
                        ON COMMIT DROP""")
        _copy(conn, "stage_price", PRICE_COLS, df.select(PRICE_COLS).iter_rows())
        cur = conn.execute("""
            INSERT INTO item_price_daily (server_id, item_id, day, vintage, price, volume, n_obs, status, method,
                                          input_hash, reason)
            SELECT s.server_id, s.item_id, s.day, coalesce(c.vintage, 0) + 1, s.price, s.volume, s.n_obs, s.status,
                   s.method, s.input_hash,
                   CASE WHEN c.vintage IS NULL THEN 'initial'
                        WHEN c.method <> s.method THEN 'method_change' ELSE s.reason END
            FROM stage_price s
            LEFT JOIN LATERAL (
                SELECT p.vintage, p.price, p.volume, p.status, p.n_obs, p.method FROM item_price_daily p
                WHERE p.server_id = s.server_id AND p.item_id = s.item_id AND p.day = s.day
                ORDER BY p.vintage DESC LIMIT 1) c ON true
            WHERE c.vintage IS NULL OR c.price IS DISTINCT FROM s.price OR c.status <> s.status
               OR c.volume IS DISTINCT FROM s.volume OR c.n_obs <> s.n_obs OR c.method <> s.method""")
        return cur.rowcount


INDEX_COLS = (
    "series_id",
    "day",
    "value",
    "coverage",
    "n_items",
    "status",
    "period_id",
    "method_version",
    "input_hash",
    "reason",
)


def publish_index(conn: psycopg.Connection, world_id: str, df: pl.DataFrame) -> int:
    if df.is_empty():
        return 0
    with conn.transaction():
        _lock(conn, world_id)
        conn.execute("""CREATE TEMP TABLE stage_index (series_id integer, day date, value double precision,
                          coverage double precision, n_items integer, status text, period_id text, method_version text,
                          input_hash text, reason text) ON COMMIT DROP""")
        _copy(conn, "stage_index", INDEX_COLS, df.select(INDEX_COLS).iter_rows())
        cur = conn.execute("""
            INSERT INTO index_value (series_id, day, vintage, value, coverage, n_items, status, period_id,
                                     method_version, input_hash, reason)
            SELECT s.series_id, s.day, coalesce(c.vintage, 0) + 1, s.value, s.coverage, s.n_items, s.status,
                   s.period_id, s.method_version, s.input_hash,
                   CASE WHEN c.vintage IS NULL THEN 'initial'
                        WHEN c.method_version <> s.method_version THEN 'method_change' ELSE s.reason END
            FROM stage_index s
            LEFT JOIN LATERAL (
                SELECT v.vintage, v.value, v.coverage, v.status, v.method_version FROM index_value v
                WHERE v.series_id = s.series_id AND v.day = s.day ORDER BY v.vintage DESC LIMIT 1) c ON true
            WHERE c.vintage IS NULL OR c.value IS DISTINCT FROM s.value OR c.status <> s.status
               OR abs(c.coverage - s.coverage) > 1e-9 OR c.method_version <> s.method_version""")
        return cur.rowcount


# ------------------------------------------------------------------------------------ baskets (frozen)
def load_basket(conn: psycopg.Connection, world_id: str, period_id: str) -> pl.DataFrame | None:
    rows = conn.execute(
        """SELECT b.server_id, b.item_id, i.division_id, b.weight, b.base_price, b.expenditure, b.quantity
                           FROM basket_item b JOIN item i USING (item_id)
                           WHERE b.world_id = %s AND b.period_id = %s ORDER BY b.server_id, b.item_id""",
        (world_id, period_id),
    ).fetchall()
    exists = conn.execute(
        "SELECT 1 FROM basket_period WHERE world_id = %s AND period_id = %s", (world_id, period_id)
    ).fetchone()
    if not exists:
        return None
    return pl.DataFrame(
        rows,
        schema={
            "server_id": pl.String,
            "item_id": pl.Int64,
            "division_id": pl.String,
            "weight": pl.Float64,
            "base_price": pl.Float64,
            "expenditure": pl.Float64,
            "quantity": pl.Float64,
        },
        orient="row",
    )


def load_links(conn: psycopg.Connection, world_id: str, period_id: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        """SELECT coalesce(s.server_id, 'all'), coalesce(s.division_id, 'all'), l.link_value
                           FROM basket_link l JOIN index_series s USING (series_id)
                           WHERE l.world_id = %s AND l.period_id = %s""",
        (world_id, period_id),
    ).fetchall()
    return {(a, b): float(v) for a, b, v in rows}


def freeze_basket(
    conn: psycopg.Connection, period: Any, method_version: str, basket: pl.DataFrame, links: dict[int, float]
) -> None:
    """Insert a basket period with its cells and link factors in one transaction. Never updates."""
    with conn.transaction():
        _lock(conn, period.world_id)
        conn.execute(
            """INSERT INTO basket_period (world_id, period_id, valid_from, valid_to, ref_from, ref_to,
                            link_from, link_to, method_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                period.world_id,
                period.period_id,
                period.valid_from,
                period.valid_to,
                period.ref_from,
                period.ref_to,
                period.link_from,
                period.link_to,
                method_version,
            ),
        )
        _copy(
            conn,
            "basket_item",
            ("world_id", "period_id", "server_id", "item_id", "weight", "base_price", "expenditure", "quantity"),
            [
                (
                    period.world_id,
                    period.period_id,
                    r["server_id"],
                    r["item_id"],
                    r["weight"],
                    r["base_price"],
                    r["expenditure"],
                    r["quantity"],
                )
                for r in basket.iter_rows(named=True)
            ],
        )
        _copy(
            conn,
            "basket_link",
            ("world_id", "period_id", "series_id", "link_value"),
            [(period.world_id, period.period_id, sid, v) for sid, v in links.items()],
        )


# ------------------------------------------------------------------------------------ recomputable analytics
def replace_rows(
    conn: psycopg.Connection,
    table: str,
    delete_where: str,
    delete_params: Sequence[Any],
    columns: Sequence[str],
    df: pl.DataFrame,
) -> int:
    """DELETE a slice and INSERT its recomputed rows atomically (for derived, non-vintaged tables).
    `table`/`delete_where` are code-defined; values always travel as parameters."""
    with conn.transaction():
        conn.execute(
            sql.SQL("DELETE FROM {} WHERE ").format(sql.Identifier(table)) + sql.SQL(delete_where),
            delete_params,
        )
        if not df.is_empty():
            _copy(conn, table, columns, df.select(columns).iter_rows())
    return df.height


def upsert_patches(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.transaction():
        cur = conn.cursor()
        cur.executemany(
            """INSERT INTO patch_event (world_id, patch_id, released_at, version, title, notes, tags, is_major, source)
                           VALUES (%(world_id)s, %(patch_id)s, %(released_at)s, %(version)s, %(title)s, %(notes)s,
                                   %(tags)s, %(is_major)s, %(source)s)
                           ON CONFLICT (world_id, patch_id) DO NOTHING""",
            rows,
        )
        return cur.rowcount


def load_patches(conn: psycopg.Connection, world_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT world_id, patch_id, released_at, tags, is_major FROM patch_event
                           WHERE world_id = %s ORDER BY released_at""",
        (world_id,),
    ).fetchall()
    return [{"world_id": w, "patch_id": p, "released_at": r, "tags": list(t), "is_major": m} for w, p, r, t, m in rows]


def load_index(conn: psycopg.Connection, world_id: str) -> pl.DataFrame:
    rows = conn.execute(
        """SELECT v.series_id, v.day, v.value FROM index_value_current v
                           JOIN index_series s USING (series_id) WHERE s.world_id = %s ORDER BY 1, 2""",
        (world_id,),
    ).fetchall()
    return pl.DataFrame(rows, schema={"series_id": pl.Int32, "day": pl.Date, "value": pl.Float64}, orient="row")


# ------------------------------------------------------------------------------------ ingestion runs
def start_ingest_run(conn: psycopg.Connection, run_id: str, job: str) -> None:
    conn.execute(
        "INSERT INTO ingest_run (run_id, job, started_at, status) VALUES (%s, %s, now(), 'running')", (run_id, job)
    )
    conn.commit()


def finish_ingest_run(conn: psycopg.Connection, run_id: str, report: dict[str, Any]) -> None:
    requested, failed = int(report.get("requested", 0)), int(report.get("failed", 0))
    status = "failed" if requested and failed == requested else "degraded" if failed else "ok"
    conn.execute(
        """UPDATE ingest_run SET finished_at = now(), status = %s, requested = %s, stored = %s, failed = %s,
                    detail = %s WHERE run_id = %s""",
        (status, requested, int(report.get("stored", 0)), failed, json.dumps(report, default=str), run_id),
    )
    conn.commit()
