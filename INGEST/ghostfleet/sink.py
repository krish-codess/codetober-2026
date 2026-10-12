"""The consumer group: Kafka -> PostGIS, idempotent on (mmsi, ts).

One batch is one database transaction; Kafka offsets are committed only after it. A crash between
the two redelivers the batch, and the primary key turns the redelivery into a no-op.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from . import db, obs
from .bus import dead_letter, producer
from .collectors import Backoff
from .config import Settings
from .validate import is_vessel_mmsi, validate_position

log = logging.getLogger(__name__)

_POSITIONS = """
WITH batch AS (
    SELECT * FROM unnest($1::int[], $2::timestamptz[], $3::float8[], $4::float8[], $5::real[], $6::real[],
                         $7::smallint[], $8::smallint[], $9::smallint[], $10::text[], $11::timestamptz[])
        AS t (mmsi, ts, lon, lat, sog, cog, heading, nav_status, msg_type, source, received_at)
)
"""
# Nearly every vessel in a batch is already known. The anti-join skips them without taking the
# speculative-insert path; ON CONFLICT remains for the rare race with another consumer.
_INSERT_VESSELS = (
    _POSITIONS
    + """
INSERT INTO vessels (mmsi)
SELECT DISTINCT b.mmsi FROM batch b WHERE NOT EXISTS (SELECT FROM vessels v WHERE v.mmsi = b.mmsi) ORDER BY 1
ON CONFLICT DO NOTHING
"""
)
_INSERT_POSITIONS = (
    _POSITIONS
    + """
INSERT INTO positions (mmsi, ts, geog, sog, cog, heading, nav_status, msg_type, source, received_at)
SELECT mmsi, ts, ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, sog, cog, heading, nav_status, msg_type, source, received_at
FROM batch ORDER BY mmsi, ts
ON CONFLICT (mmsi, ts) DO NOTHING
RETURNING received_at
"""
)
# Out-of-order and late arrivals must not move a vessel backwards on the live map.
_UPSERT_LATEST = (
    _POSITIONS
    + """
INSERT INTO vessel_latest (mmsi, ts, geom, sog, cog, heading, nav_status)
SELECT DISTINCT ON (mmsi) mmsi, ts, ST_SetSRID(ST_MakePoint(lon, lat), 4326), sog, cog, heading, nav_status
FROM batch ORDER BY mmsi, ts DESC
ON CONFLICT (mmsi) DO UPDATE SET ts = excluded.ts, geom = excluded.geom, sog = excluded.sog, cog = excluded.cog,
    heading = excluded.heading, nav_status = excluded.nav_status, updated_at = clock_timestamp()
WHERE excluded.ts > vessel_latest.ts
"""
)
# Static data arrives in pieces (type 24 A and B) and with blanks: keep what we know, add what is new.
_UPSERT_STATIC = """
INSERT INTO vessels (mmsi, imo, name, callsign, ship_type, length_m, beam_m, draught_m, destination, static_updated_at)
SELECT * FROM unnest($1::int[], $2::int[], $3::text[], $4::text[], $5::smallint[], $6::smallint[], $7::smallint[],
                     $8::real[], $9::text[], $10::timestamptz[])
ON CONFLICT (mmsi) DO UPDATE SET
    imo = coalesce(excluded.imo, vessels.imo), name = coalesce(excluded.name, vessels.name),
    callsign = coalesce(excluded.callsign, vessels.callsign), ship_type = coalesce(excluded.ship_type, vessels.ship_type),
    length_m = coalesce(excluded.length_m, vessels.length_m), beam_m = coalesce(excluded.beam_m, vessels.beam_m),
    draught_m = coalesce(excluded.draught_m, vessels.draught_m),
    destination = coalesce(excluded.destination, vessels.destination), static_updated_at = excluded.static_updated_at
"""
_INSERT_QUARANTINE = """
INSERT INTO quarantine (cid, stage, reason, source, payload, received_at)
SELECT * FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::timestamptz[])
ON CONFLICT (cid, stage) DO NOTHING
"""
_POSITION_COLUMNS = (
    "mmsi",
    "ts",
    "lon",
    "lat",
    "sog",
    "cog",
    "heading",
    "nav_status",
    "msg_type",
    "source",
    "received_at",
)


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def valid_imo(imo: Any) -> bool:
    """IMO numbers carry a check digit; transponders are hand-configured and often carry garbage."""
    if not isinstance(imo, int) or not 1_000_000 <= imo <= 9_999_999:
        return False
    digits = [int(c) for c in str(imo)]
    return sum(d * w for d, w in zip(digits[:6], range(7, 1, -1), strict=True)) % 10 == digits[6]


def _clean_static(m: dict[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str | None:
        v = m.get(key)
        return v.strip() or None if isinstance(v, str) else None

    def size(a: str, b: str) -> int | None:
        total = (m.get(a) or 0) + (m.get(b) or 0)
        return total if 0 < total < 1000 else None

    ship_type = m.get("ship_type")
    draught = m.get("draught")
    return {
        "imo": m["imo"] if valid_imo(m.get("imo")) else None, "name": text("name"), "callsign": text("callsign"),
        "ship_type": ship_type if isinstance(ship_type, int) and 0 < ship_type <= 255 else None,
        "length_m": size("to_bow", "to_stern"), "beam_m": size("to_port", "to_starboard"),
        "draught_m": draught if isinstance(draught, int | float) and draught > 0 else None,
        "destination": text("destination"),
    }  # fmt: skip


def _columns(rows: list[dict[str, Any]]) -> list[list[Any]]:
    cols: list[list[Any]] = [[r.get(c) for r in rows] for c in _POSITION_COLUMNS]
    cols[1] = [_dt(t) for t in cols[1]]
    cols[10] = [_dt(t) for t in cols[10]]
    return cols


async def write_batch(
    conn: asyncpg.Connection, positions: list[dict[str, Any]], statics: list[dict[str, Any]], dead: list[dict[str, Any]]
) -> list[datetime]:
    """Write one batch in one transaction. Returns received_at of each row actually inserted."""
    inserted: list[datetime] = []
    async with conn.transaction():
        if positions:
            cols = _columns(positions)
            await conn.execute(_INSERT_VESSELS, *cols)
            inserted = [r["received_at"] for r in await conn.fetch(_INSERT_POSITIONS, *cols)]
            await conn.execute(_UPSERT_LATEST, *cols)
        if statics:
            merged: dict[int, dict[str, Any]] = {}
            for m in statics:
                row = merged.setdefault(m["mmsi"], {"received_at": m["received_at"]})
                row.update({k: v for k, v in _clean_static(m).items() if v is not None})
                row["received_at"] = max(row["received_at"], m["received_at"])
            keys = sorted(merged)
            fields = ("imo", "name", "callsign", "ship_type", "length_m", "beam_m", "draught_m", "destination")
            await conn.execute(
                _UPSERT_STATIC, keys, *[[merged[k].get(f) for k in keys] for f in fields],
                [_dt(merged[k]["received_at"]) for k in keys],
            )  # fmt: skip
        if dead:
            await conn.execute(
                _INSERT_QUARANTINE, *[[d[k] for d in dead] for k in ("cid", "stage", "reason", "source", "payload")],
                [_dt(d["received_at"]) for d in dead],
            )  # fmt: skip
    return inserted


def classify(topic: str, raw: bytes, cid: str, s: Settings, now: float) -> tuple[str, dict[str, Any]]:
    """Sort one Kafka record into 'position', 'static', 'dead' (already a dead letter) or 'reject'."""
    text = raw.decode("utf-8", "replace")
    try:
        m = json.loads(raw)
        if not isinstance(m, dict):
            raise ValueError("not an object")
    except ValueError:
        if topic == s.topic_dlq:
            # Straight to the table. Sending a broken dead letter back to the dead-letter topic would loop forever.
            return "dead", dead_letter("sink", "malformed_dead_letter", "unknown", text, now, cid)
        return "reject", dead_letter("sink", "unparseable_record", "unknown", text, now, cid)
    if topic == s.topic_dlq:
        strings = all(isinstance(m.get(k), str) for k in ("cid", "stage", "reason", "source", "payload"))
        if strings and m["stage"] in ("decode", "validate", "sink") and isinstance(m.get("received_at"), int | float):
            return "dead", m
        return "dead", dead_letter("sink", "malformed_dead_letter", "unknown", text, now, cid)
    source, received_at = m.get("source"), m.get("received_at")
    if not isinstance(source, str) or not isinstance(received_at, int | float):
        return "reject", dead_letter("sink", "missing_envelope", str(source), json.dumps(m), now, cid)
    if topic == s.topic_static:
        if not is_vessel_mmsi(m.get("mmsi")):
            return "reject", dead_letter(
                "sink", "non_vessel_mmsi", source, json.dumps(m), received_at, m.get("cid") or cid
            )
        return "static", m
    reason = validate_position(m, now) or (None if m.get("msg_type") in (1, 2, 3, 18, 19, 27) else "bad_msg_type")
    if reason:
        return "reject", dead_letter("sink", reason, source, json.dumps(m), received_at, m.get("cid") or cid)
    return "position", m


async def process(pool: asyncpg.Pool, prod: AIOKafkaProducer, records: list[Any], s: Settings) -> int:
    """Sink one polled batch. Returns the number of new positions."""
    now = time.time()
    buckets: dict[str, list[dict[str, Any]]] = {"position": [], "static": [], "dead": [], "reject": []}
    for r in records:
        kind, m = classify(r.topic, r.value, f"kafka/{r.topic}/{r.partition}:{r.offset}", s, now)
        buckets[kind].append(m)
    async with pool.acquire() as conn:
        try:
            inserted = await write_batch(conn, buckets["position"], buckets["static"], buckets["dead"])
        except (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError) as batch_error:
            # Something passed validation but the database refuses it. Find the offender row by row
            # so one poison record cannot wedge the partition, and dead-letter it with the reason.
            log.warning("batch rejected by database, isolating", extra={"error": repr(batch_error)})
            inserted = []
            for kind, arg in (("position", 0), ("static", 1), ("dead", 2)):
                for m in buckets[kind]:
                    args: list[list[dict[str, Any]]] = [[], [], []]
                    args[arg] = [m]
                    try:
                        inserted += await write_batch(conn, *args)
                    except (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError) as e:
                        buckets["reject"].append(
                            dead_letter(
                                "sink",
                                f"db_{type(e).__name__}",
                                str(m.get("source")),
                                json.dumps(m),
                                now,
                                str(m.get("cid")),
                            )
                        )
    for d in buckets["reject"]:
        obs.QUARANTINED.labels(d["stage"], d["reason"]).inc()
        await prod.send_and_wait(s.topic_dlq, value=d, key=d["cid"])
    obs.INSERTED.inc(len(inserted))
    obs.DUPLICATES.inc(len(buckets["position"]) - len(inserted))
    done = time.time()
    for received_at in inserted:
        obs.INGEST_LAG.observe(max(0.0, done - received_at.timestamp()))
    return len(inserted)


async def consume(pool: asyncpg.Pool, prod: AIOKafkaProducer, s: Settings, *, max_records: int = 2000) -> None:
    consumer = AIOKafkaConsumer(
        s.topic_positions, s.topic_static, s.topic_dlq, bootstrap_servers=s.kafka, group_id=s.group,
        enable_auto_commit=False, auto_offset_reset="earliest",
    )  # fmt: skip
    await consumer.start()
    try:
        while True:
            polled = await consumer.getmany(timeout_ms=250, max_records=max_records)
            records = [r for batch in polled.values() for r in batch]
            if not records:
                await pool.fetchval("SELECT 1")  # idle is healthy only if the database still answers
                obs.LAST_SUCCESS.set_to_current_time()
                continue
            obs.correlation_id.set(f"kafka/{records[0].topic}/{records[0].partition}:{records[0].offset}")
            with obs.BATCHES.time():
                new = await process(pool, prod, records, s)
            await consumer.commit()
            obs.LAST_SUCCESS.set_to_current_time()
            log.debug("batch sunk", extra={"records": len(records), "inserted": new})
    finally:
        await consumer.stop()


async def run(s: Settings) -> None:
    """Consume forever. Any dependency failure tears the consumer down and retries with backoff;
    uncommitted records are redelivered, which the primary key makes safe."""
    obs.serve_metrics(s.metrics_port)
    backoff = Backoff(s.reconnect_base_s, s.reconnect_cap_s)
    while True:
        pool = prod = None
        started = time.monotonic()
        try:
            pool = await db.pool(s.dsn)
            prod = producer(s)
            await prod.start()
            await consume(pool, prod, s)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if time.monotonic() - started > 30:
                backoff.reset()
            delay = backoff.next()
            log.warning("sink interrupted, retrying", extra={"error": repr(e), "retry_in_s": round(delay, 2)})
            await asyncio.sleep(delay)
        finally:
            if prod:
                await prod.stop()
            if pool:
                await pool.close()
