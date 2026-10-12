"""Against a real PostGIS (and, where marked, a real Kafka): the paths where the integration is the risk."""

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest
from conftest import T_END, FakeProducer, load

from ghostfleet import sink
from ghostfleet.bus import ensure_topics, producer
from ghostfleet.collectors import Decoder, replay
from ghostfleet.config import Settings
from ghostfleet.db import migrate
from ghostfleet.detect import detect, refresh_coverage
from ghostfleet.evaluate import evaluate
from ghostfleet.simulate import World

pytestmark = pytest.mark.integration


def position(mmsi=257000001, ts=T_END, lat=60.0, lon=5.0, **kw):
    base = dict(kind="position", msg_type=1, mmsi=mmsi, ts=ts, lat=lat, lon=lon, sog=10.0, cog=90.0, heading=90,
                nav_status=0, source="test", received_at=ts + 0.5, cid=f"test/{mmsi}:{ts}", v=1)  # fmt: skip
    return {**base, **kw}


# --- migrations -------------------------------------------------------------------------------------


async def test_migrations_roll_back_and_forward(cfg, conn):
    async def tables():
        return {r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}

    ours = {"vessels", "positions", "vessel_latest", "coverage_cells", "dark_events", "quarantine", "mid_countries"}
    assert ours <= await tables()
    assert await conn.fetchval("SELECT has_table_privilege('ghost_api', 'positions', 'SELECT')")
    await migrate(cfg.dsn, 1)
    assert not await conn.fetchval("SELECT has_table_privilege('ghost_api', 'positions', 'SELECT')")
    await migrate(cfg.dsn, 0)
    assert not ours & await tables()
    assert await migrate(cfg.dsn) == 2
    assert ours <= await tables()
    assert await conn.fetchval("SELECT count(*) FROM mid_countries") > 280
    # Least privilege survives the round trip: the API role can read, and only read.
    assert await conn.fetchval("SELECT has_table_privilege('ghost_api', 'positions', 'SELECT')")
    assert not await conn.fetchval("SELECT has_table_privilege('ghost_api', 'positions', 'INSERT')")
    assert not await conn.fetchval("SELECT has_table_privilege('ghost_ingest', 'dark_events', 'INSERT')")
    assert not await conn.fetchval("SELECT has_table_privilege('ghost_detect', 'positions', 'INSERT')")


# --- the sink ---------------------------------------------------------------------------------------


async def test_replaying_the_same_window_creates_no_duplicates(conn, history):
    positions, statics, dead, _ = history
    unique = len({(p["mmsi"], p["ts"]) for p in positions})
    assert unique < len(positions)  # the feed really does repeat itself
    assert await load(conn, positions, statics, dead) == unique
    before = await conn.fetch("SELECT mmsi, ts, ingested_at FROM positions ORDER BY 1, 2")
    assert await load(conn, positions, statics, dead) == 0
    assert await conn.fetch("SELECT mmsi, ts, ingested_at FROM positions ORDER BY 1, 2") == before
    assert await conn.fetchval("SELECT count(*) FROM quarantine") == len({(d["cid"], d["stage"]) for d in dead})


async def test_data_invariants_after_a_full_load(conn, history):
    positions, statics, dead, _ = history
    await load(conn, positions, statics, dead)
    one = conn.fetchval
    # Most of the fleet and most of the feed must make it through: a validator that quietly rejects
    # half the input still produces a self-consistent, duplicate-free, wrong database.
    assert await one("SELECT count(*) FROM vessels") > 850
    assert len(dead) < 0.06 * len(positions)
    assert await one("SELECT count(*) FROM positions") == len({(p["mmsi"], p["ts"]) for p in positions})
    assert await one("SELECT count(*) FROM (SELECT 1 FROM positions GROUP BY mmsi, ts HAVING count(*) > 1) d") == 0
    assert await one("SELECT count(*) FROM positions p LEFT JOIN vessels v USING (mmsi) WHERE v.mmsi IS NULL") == 0
    assert await one("SELECT count(*) FROM vessel_latest") == await one("SELECT count(DISTINCT mmsi) FROM positions")
    # The live table really is the latest position of each vessel, not merely the last one written.
    assert (
        await one(
            "SELECT count(*) FROM vessel_latest l JOIN (SELECT mmsi, max(ts) AS ts FROM positions GROUP BY 1) m USING (mmsi) "
            "WHERE l.ts <> m.ts"
        )
        == 0
    )
    assert await one("SELECT count(*) FROM positions WHERE ts > $1 OR ts < $2", datetime.fromtimestamp(T_END, UTC),
                     datetime.fromtimestamp(T_END - 6 * 3600 - 60, UTC)) == 0  # fmt: skip
    assert (
        await one(
            "SELECT count(*) FROM positions WHERE abs(ST_Y(geog::geometry)) > 90 OR abs(ST_X(geog::geometry)) > 180"
        )
        == 0
    )
    # Distribution: a fleet where nobody or everybody is moored means the generator or the decoder broke.
    stationary = await one("SELECT avg((sog < 0.5)::int) FROM positions")
    assert 0.1 < stationary < 0.6
    no_heading = await one("SELECT avg((heading IS NULL)::int) FROM positions")
    assert 0.3 < no_heading < 0.6
    # Identity: garbage IMO numbers were dropped, real ones kept, flags resolve.
    assert await one("SELECT count(*) FROM vessels WHERE name IS NOT NULL") > 500
    assert 0 < await one("SELECT count(*) FROM vessels WHERE name IS NOT NULL AND imo IS NULL") < 400
    assert await one("SELECT count(*) FROM vessels v JOIN mid_countries m ON m.mid = v.mid") == await one(
        "SELECT count(*) FROM vessels"
    )
    reasons = {r["reason"] for r in await conn.fetch("SELECT DISTINCT reason FROM quarantine")}
    assert reasons >= {
        "bad_checksum",
        "malformed_sentence",
        "incomplete_multipart",
        "non_vessel_mmsi",
        "position_unavailable",
    }


async def test_late_and_out_of_order_arrivals_never_move_a_vessel_backwards(conn):
    await sink.write_batch(conn, [position(ts=T_END, lon=5.2)], [], [])
    await sink.write_batch(conn, [position(ts=T_END - 300, lon=5.0), position(ts=T_END - 600, lon=4.8)], [], [])
    assert await conn.fetchval("SELECT count(*) FROM positions") == 3
    row = await conn.fetchrow("SELECT ts, ST_X(geom) AS lon FROM vessel_latest")
    assert (row["ts"].timestamp(), row["lon"]) == (T_END, pytest.approx(5.2))


async def test_same_key_different_position_keeps_the_first_and_counts_the_second(conn):
    assert len(await sink.write_batch(conn, [position(lon=5.0)], [], [])) == 1
    assert len(await sink.write_batch(conn, [position(lon=99.0)], [], [])) == 0
    assert await conn.fetchval("SELECT ST_X(geog::geometry) FROM positions") == pytest.approx(5.0)


async def test_static_data_merges_across_partial_reports(conn):
    rx = float(T_END)
    part_a = {"kind": "static", "msg_type": 24, "mmsi": 257000001, "name": "NORDLYS", "received_at": rx}
    part_b = {"kind": "static", "msg_type": 24, "mmsi": 257000001, "ship_type": 60, "callsign": "LAB1", "to_bow": 60,
              "to_stern": 60, "to_port": 9, "to_starboard": 9, "received_at": rx + 1}  # fmt: skip
    await sink.write_batch(conn, [], [part_a], [])
    await sink.write_batch(conn, [], [part_b], [])
    await sink.write_batch(conn, [], [{**part_a, "name": None, "imo": 123}], [])  # blanks and garbage erase nothing
    v = await conn.fetchrow("SELECT name, callsign, ship_type, length_m, beam_m, imo, mid FROM vessels")
    assert tuple(v) == ("NORDLYS", "LAB1", 60, 120, 18, None, 257)


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("INSERT INTO vessels (mmsi) VALUES (123)", asyncpg.CheckViolationError),
        (
            "INSERT INTO positions (mmsi, ts, geog, msg_type, source, received_at) VALUES (257000009, now(), 'POINT(5 60)', 1, 't', now())",
            asyncpg.ForeignKeyViolationError,
        ),
        (
            "INSERT INTO vessels (mmsi) VALUES (257000001); INSERT INTO positions (mmsi, ts, geog, sog, msg_type, source, received_at) VALUES (257000001, now(), 'POINT(5 60)', 200, 1, 't', now())",
            asyncpg.CheckViolationError,
        ),
        (
            "INSERT INTO vessels (mmsi) VALUES (257000001); INSERT INTO positions (mmsi, ts, geog, msg_type, source, received_at) VALUES (257000001, now(), 'POINT(5 60)', 4, 't', now())",
            asyncpg.CheckViolationError,
        ),
        (
            "INSERT INTO vessels (mmsi) VALUES (257000001); INSERT INTO dark_events (mmsi, started_at, ended_at, start_geog, duration_s, start_reliability, neighbours_heard, classification, score, reasons) VALUES (257000001, now(), now() + interval '1 hour', 'POINT(5 60)', 3600, 1, 1, 'dark', 50, '[]')",
            asyncpg.CheckViolationError,
        ),
        (
            "INSERT INTO vessels (mmsi) VALUES (257000001); INSERT INTO dark_events (mmsi, started_at, start_geog, duration_s, start_reliability, neighbours_heard, classification, score, reasons) VALUES (257000001, now(), 'POINT(5 60)', 3600, 1, 1, 'suspicious', 50, '[]')",
            asyncpg.CheckViolationError,
        ),
        (
            "INSERT INTO coverage_cells (cx, cy, cell_deg, hours_heard, hours_window, vessels, reliability, geom) VALUES (1, 1, 0.25, 9, 8, 1, 1, ST_MakeEnvelope(0, 0, 1, 1, 4326))",
            asyncpg.CheckViolationError,
        ),
    ],
)
async def test_the_database_itself_refuses_bad_rows(conn, sql, error):
    with pytest.raises(error):
        await conn.execute(sql)


def record(topic, value, offset=0):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return SimpleNamespace(topic=topic, partition=0, offset=offset, value=raw)


async def test_poison_records_are_dead_lettered_and_the_rest_of_the_batch_survives(cfg, conn):
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=2)
    prod = FakeProducer()
    try:
        batch = [
            record(cfg.topic_positions, position(ts=T_END - 3), 0),
            record(cfg.topic_positions, b"\xff\xfe not json", 1),
            record(cfg.topic_positions, position(ts=T_END - 2, lat=91, lon=181), 2),
            record(
                cfg.topic_positions, position(ts=T_END - 1, source="re" + chr(0) + "lay"), 3
            ),  # valid JSON, valid position, and PostgreSQL will not store it
            record(cfg.topic_positions, {"mmsi": 257000001, "ts": T_END}, 4),
            record(cfg.topic_positions, position(ts=T_END + 10**9), 5),
            record(cfg.topic_positions, position(ts=T_END), 6),
            record(
                cfg.topic_dlq,
                {
                    "cid": "sim/x:1",
                    "stage": "decode",
                    "reason": "bad_checksum",
                    "source": "sim",
                    "payload": "!AIVDM...",
                    "received_at": T_END,
                },
                7,
            ),
            record(cfg.topic_dlq, b"garbage", 8),
        ]
        assert await sink.process(pool, prod, batch, cfg) == 2
    finally:
        await pool.close()
    assert await conn.fetchval("SELECT count(*) FROM positions") == 2
    dead = {v["reason"]: v for _, _, v in prod.sent}
    assert set(dead) == {
        "unparseable_record",
        "position_unavailable",
        "db_CharacterNotInRepertoireError",
        "missing_envelope",
        "timestamp_in_future",
    }
    assert all(v["stage"] == "sink" and v["payload"] for v in dead.values())
    assert (
        json.loads(dead["db_CharacterNotInRepertoireError"]["payload"])["ts"] == T_END - 1
    )  # the original, intact, for replay
    assert dead["unparseable_record"]["cid"] == f"kafka/{cfg.topic_positions}/0:1"
    stored = {r["reason"] for r in await conn.fetch("SELECT reason FROM quarantine")}
    assert stored == {"bad_checksum", "malformed_dead_letter"}


# --- detection --------------------------------------------------------------------------------------


async def test_detector_separates_dark_from_coverage_and_is_idempotent(cfg, conn, history):
    positions, statics, _, truth = history
    await load(conn, positions, statics)
    await refresh_coverage(conn, cfg)
    first = await detect(conn, cfg)
    assert first["dark"] >= 10 and first["coverage_gap"] >= 30, first

    report = await evaluate(cfg, [t.as_dict() for t in truth])
    numbers = {line.split(":")[0].strip("- `"): line for line in report.splitlines() if line.startswith("- ")}
    precision = float(numbers["precision of `dark"].rsplit("= ", 1)[1])
    recall = float(numbers["recall of `dark"].rsplit("= ", 1)[1])
    # Floors, not targets: the measured values are in docs/evidence/detection.md. A rule change that
    # drops below these has broken the distinction this system exists to make.
    assert precision >= 0.8 and recall >= 0.8, report
    flagged, teleports = (
        int(x) for x in numbers["impossible-speed reappearances flagged"].split(": ")[1].split(";")[0].split("/")
    )
    assert teleports > 3 and flagged == teleports, report
    assert numbers["impossible-speed reappearances flagged"].endswith("flagged without cause: 0"), report

    snapshot = "SELECT id, mmsi, started_at, ended_at, classification, score, reasons FROM dark_events ORDER BY id"
    before = await conn.fetch(snapshot)
    assert await detect(conn, cfg) == first
    assert await conn.fetch(snapshot) == before  # same rows, same ids: nothing duplicated, nothing churned

    # Ranking: every impossible reappearance outranks every plain dark period, which outranks every coverage gap.
    r = await conn.fetchrow(
        "SELECT min(score) FILTER (WHERE impossible_speed AND classification = 'dark') AS imp, "
        "max(score) FILTER (WHERE NOT impossible_speed AND classification = 'dark') AS dark_hi, "
        "min(score) FILTER (WHERE NOT impossible_speed AND classification = 'dark') AS dark_lo, "
        "max(score) FILTER (WHERE NOT impossible_speed AND classification = 'coverage_gap') AS cov FROM dark_events"
    )
    assert r["imp"] > r["dark_hi"] and r["dark_lo"] > r["cov"]


async def test_late_data_that_fills_a_gap_withdraws_the_event(cfg, conn, history):
    positions, _, _, _ = history
    await load(conn, positions)
    await refresh_coverage(conn, cfg)
    await detect(conn, cfg)
    e = await conn.fetchrow(
        "SELECT mmsi, started_at, ended_at, ST_X(start_geog::geometry) AS lon, ST_Y(start_geog::geometry) AS lat "
        "FROM dark_events WHERE classification = 'dark' AND ended_at IS NOT NULL AND NOT impossible_speed ORDER BY id LIMIT 1"
    )
    t0, t1 = int(e["started_at"].timestamp()), int(e["ended_at"].timestamp())
    late = [position(mmsi=e["mmsi"], ts=t, lat=e["lat"], lon=e["lon"]) for t in range(t0 + 60, t1, 60)]
    await sink.write_batch(conn, late, [], [])  # the missing reports turn up after all, via a slower provider
    await detect(conn, cfg)
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM dark_events WHERE mmsi = $1 AND started_at = $2", e["mmsi"], e["started_at"]
        )
        == 0
    )


async def test_stalled_ingestion_does_not_turn_the_fleet_dark(cfg, conn):
    old = T_END - 20 * 3600
    await sink.write_batch(conn, [position(mmsi=257000000 + i, ts=old + i) for i in range(50)], [], [])
    await refresh_coverage(conn, cfg)
    assert await detect(conn, cfg) == {}  # twenty hours of nothing from anyone is our outage, not theirs


async def test_spatial_indexes_serve_their_queries(conn, history):
    positions, _, _, _ = history
    await load(conn, positions)
    await conn.execute("ANALYZE")
    await conn.execute(
        "SET enable_seqscan = off"
    )  # small test tables: ask whether the index can serve, not whether it is cheapest here

    async def plan(sql, *args):
        return "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + sql, *args))

    assert "vessel_latest_geom_gist" in await plan(
        "SELECT mmsi FROM vessel_latest WHERE geom && ST_MakeEnvelope(0, 50, 10, 62, 4326)"
    )
    near = await plan(
        "SELECT mmsi FROM positions WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint(5, 60), 4326)::geography, 20000) "
        "AND ts > $1 AND ts <= $2", datetime.fromtimestamp(T_END - 1800, UTC), datetime.fromtimestamp(T_END, UTC),
    )  # fmt: skip
    assert "positions_geog_gist" in near or "positions_ts_brin" in near
    assert "positions_pkey" in await plan(
        "SELECT ts FROM positions WHERE mmsi = 257000001 AND ts > now() - interval '1 day' ORDER BY ts"
    )
    assert "dark_events_rank" in await plan("SELECT id FROM dark_events ORDER BY score DESC, id DESC LIMIT 50")


# --- through Kafka ------------------------------------------------------------------------------------


def write_capture(tmp_path, ships=250, minutes=25):
    world = World(seed=3, n_ships=ships)
    lines = [line for _, line in world.sentences(T_END - minutes * 60, T_END)]
    path = tmp_path / "relay" / "capture.nmea"
    path.parent.mkdir()
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    decoder = Decoder("relay", Settings())
    keys, dead = set(), set()
    for i, line in enumerate(lines):
        for topic, _, value in decoder.nmea(line, None, f"x:{i}", ("x", i)):
            if topic.endswith("positions"):
                keys.add((value["mmsi"], value["ts"]))
            elif topic.endswith("dlq"):
                dead.add((value["cid"], value["stage"]))
    return path, len(keys), len(dead)


async def settle(conn, expected, timeout=120):
    async with asyncio.timeout(timeout):
        while await conn.fetchval("SELECT count(*) FROM positions") < expected:
            await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)  # anything still in flight would show up as extra rows
    return await conn.fetchval("SELECT count(*) FROM positions")


async def test_archive_replayed_twice_through_kafka_stores_each_position_once(kafka_cfg, conn, tmp_path):
    cfg = dataclasses.replace(kafka_cfg, topic_positions=kafka_cfg.topic_positions + ".a", topic_static=kafka_cfg.topic_static + ".a",
                              topic_dlq=kafka_cfg.topic_dlq + ".a", group=kafka_cfg.group + ".a")  # fmt: skip
    path, unique, dead = write_capture(tmp_path)
    await ensure_topics(cfg)
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=2)
    prod = producer(cfg)
    await prod.start()
    consumer = asyncio.create_task(sink.consume(pool, prod, cfg))
    try:
        first = await replay(path, "relay", cfg)
        assert await settle(conn, unique) == unique
        quarantined = await conn.fetchval("SELECT count(*) FROM quarantine")
        assert quarantined == dead > 0
        second = await replay(path, "relay", cfg)  # the same archived window, again
        assert second == first
        await asyncio.sleep(5)
        assert await conn.fetchval("SELECT count(*) FROM positions") == unique
        assert await conn.fetchval("SELECT count(*) FROM quarantine") == quarantined
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await prod.stop()
        await pool.close()


async def test_killing_the_sink_mid_batch_loses_nothing_and_duplicates_nothing(kafka_cfg, conn, tmp_path):
    """Failure injection: SIGKILL the consumer process while it is writing, start another, compare with the truth."""
    cfg = dataclasses.replace(kafka_cfg, topic_positions=kafka_cfg.topic_positions + ".b", topic_static=kafka_cfg.topic_static + ".b",
                              topic_dlq=kafka_cfg.topic_dlq + ".b", group=kafka_cfg.group + ".b")  # fmt: skip
    path, unique, dead = write_capture(tmp_path, ships=400, minutes=40)
    await ensure_topics(cfg)
    await replay(path, "relay", cfg)  # everything is in Kafka before any consumer exists
    env = {**os.environ, "GHOST_DSN": cfg.dsn, "GHOST_KAFKA": cfg.kafka, "GHOST_TOPIC_POSITIONS": cfg.topic_positions,
           "GHOST_TOPIC_STATIC": cfg.topic_static, "GHOST_TOPIC_DLQ": cfg.topic_dlq, "GHOST_GROUP": cfg.group, "GHOST_METRICS_PORT": "0"}  # fmt: skip

    def start():
        return subprocess.Popen(
            [sys.executable, "-m", "ghostfleet", "sink"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    victim = start()
    try:
        async with asyncio.timeout(90):
            while await conn.fetchval("SELECT count(*) FROM positions") < unique * 0.2:
                await asyncio.sleep(0.02)
        victim.kill()  # no shutdown hook, no offset commit for the batch in flight
        victim.wait()
        at_kill = await conn.fetchval("SELECT count(*) FROM positions")
        assert 0 < at_kill < unique, "the sink finished before it could be killed; the test proved nothing"
        survivor = start()
        try:
            assert await settle(conn, unique) == unique
            assert await conn.fetchval("SELECT count(*) FROM quarantine") == dead
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM (SELECT 1 FROM positions GROUP BY mmsi, ts HAVING count(*) > 1) d"
                )
                == 0
            )
        finally:
            survivor.kill()
            survivor.wait()
    finally:
        if victim.poll() is None:
            victim.kill()


async def test_detector_window_bounds(cfg, conn):
    """A gap is only visible if both of its ends are inside the lookback window."""
    now = datetime.fromtimestamp(T_END, UTC)
    inside, outside = (
        int((now - timedelta(hours=3)).timestamp()),
        int((now - timedelta(hours=cfg.lookback_h + 5)).timestamp()),
    )
    rows = [position(mmsi=257000001, ts=inside), position(mmsi=257000001, ts=T_END),
            position(mmsi=257000002, ts=outside), position(mmsi=257000002, ts=T_END)]  # fmt: skip
    await sink.write_batch(conn, rows, [], [])
    await detect(conn, cfg)
    assert [r["mmsi"] for r in await conn.fetch("SELECT mmsi FROM dark_events")] == [257000001]
