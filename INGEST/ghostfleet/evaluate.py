"""Evidence tooling: score detection against ground truth, measure latency under load, replay dead letters."""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any

import asyncpg

from .collectors import Archive, Decoder, pump, start_producer
from .config import Settings
from .nmea import encode_position, to_sentences


async def evaluate(cfg: Settings, truth: list[dict[str, Any]]) -> str:
    """Confusion matrix of the detector's verdicts against what the simulator knows really happened."""
    conn = await asyncpg.connect(cfg.dsn)
    try:
        rows = await conn.fetch(
            "SELECT mmsi, extract(epoch FROM started_at)::float8 AS t0, extract(epoch FROM ended_at)::float8 AS t1, "
            "classification, impossible_speed FROM dark_events WHERE ended_at IS NOT NULL AND classification <> 'position_jump'"
        )
    finally:
        await conn.close()
    by_mmsi: dict[int, list[asyncpg.Record]] = {}
    for r in rows:
        by_mmsi.setdefault(r["mmsi"], []).append(r)
    matrix: dict[tuple[str, str], int] = {}
    teleports = teleports_flagged = false_impossible = 0
    matched: set[tuple[int, float]] = set()
    for t in truth:
        if not t["moving"]:
            continue
        best, best_overlap = None, 0.0
        for r in by_mmsi.get(t["mmsi"], []):
            overlap = min(r["t1"], t["end"]) - max(r["t0"], t["start"])
            if overlap > best_overlap:
                best, best_overlap = r, overlap
        verdict = "not detected"
        if best is not None and best_overlap >= 0.5 * (t["end"] - t["start"]):
            verdict = best["classification"]
            matched.add((best["mmsi"], best["t0"]))
            teleports_flagged += bool(t["teleport"] and best["impossible_speed"])
            false_impossible += bool(best["impossible_speed"] and not t["teleport"])
        teleports += bool(t["teleport"])
        key = (t["cause"], verdict)
        matrix[key] = matrix.get(key, 0) + 1
    unmatched = sum(1 for r in rows if (r["mmsi"], r["t0"]) not in matched)

    def cell(cause: str, verdict: str) -> int:
        return matrix.get((cause, verdict), 0)

    tp = cell("dark", "dark")
    fn = cell("dark", "coverage_gap") + cell("dark", "not detected")
    fp = cell("range", "dark") + cell("outage", "dark")
    tn = cell("range", "coverage_gap") + cell("outage", "coverage_gap")
    lines = [
        "| ground truth \\ verdict | dark | coverage_gap | not detected |",
        "|---|---|---|---|",
        *(
            f"| {label} | {cell(c, 'dark')} | {cell(c, 'coverage_gap')} | {cell(c, 'not detected')} |"
            for c, label in (
                ("dark", "transponder off"),
                ("range", "out of receiver range"),
                ("outage", "receiver outage"),
            )
        ),
        "",
        f"- precision of `dark`: {tp}/{tp + fp} = {tp / (tp + fp):.3f}" if tp + fp else "- precision of `dark`: n/a",
        f"- recall of `dark`: {tp}/{tp + fn} = {tp / (tp + fn):.3f}" if tp + fn else "- recall of `dark`: n/a",
        f"- innocent silences correctly left as `coverage_gap`: {tn}/{tn + fp}" if tn + fp else "",
        f"- impossible-speed reappearances flagged: {teleports_flagged}/{teleports}; flagged without cause: {false_impossible}",
        f"- detector events with no ground-truth silence behind them: {unmatched}",
    ]
    return "\n".join(lines)


async def replay_dlq(cfg: Settings, reason: str) -> int:
    """Put quarantined inputs back through the pipeline, e.g. after fixing the decoder that refused them.

    Inputs that still fail are simply quarantined again under the same key, so this is safe to repeat.
    """
    conn = await asyncpg.connect(cfg.dsn)
    prod = await start_producer(cfg)
    try:
        rows = await conn.fetch(
            "SELECT cid, stage, source, payload, extract(epoch FROM received_at)::float8 AS rx FROM quarantine "
            "WHERE reason = $1 AND replayed_at IS NULL AND stage IN ('decode', 'validate')",
            reason,
        )
        for r in rows:
            decoder = Decoder(r["source"], cfg)
            if r["stage"] == "validate":
                msg = json.loads(r["payload"])
                records = list(decoder.emit(msg, r["rx"], r["cid"]))
            else:
                decode = decoder.aisstream if r["payload"].startswith("{") else decoder.nmea
                records = [
                    rec for line in r["payload"].split("\n") for rec in decode(line, r["rx"], r["cid"], ("dlq", 0))
                ]
            for topic, key, value in records:
                await prod.send_and_wait(topic, value=value, key=key)
        await conn.execute(
            "UPDATE quarantine SET replayed_at = now() WHERE reason = $1 AND replayed_at IS NULL", reason
        )
        return len(rows)
    finally:
        await prod.stop()
        await conn.close()


async def loadtest(cfg: Settings, rate: int, seconds: int, *, fleet: int = 20_000) -> str:
    """Drive `rate` position reports per second through archive -> decode -> Kafka -> sink -> PostGIS.

    Needs a running `ghostfleet sink`. Latency is receive (the archive stamp) to the row's insert time.
    Uses the unallocated MID 799 so its rows are easy to tell apart, and removes them afterwards.
    """
    rng = random.Random(1)
    conn = await asyncpg.connect(cfg.dsn)
    prod = await start_producer(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        # A source name of its own: records a previous, aborted run left in Kafka cannot be mistaken for ours.
        source = f"loadtest-{int(time.time())}"
        archive = Archive(Path(tmp), source)
        task = asyncio.create_task(pump(archive, Decoder(source, cfg), prod, checkpoint=False))
        try:
            begun = time.time()
            sent = 0
            while (elapsed := time.time() - begun) < seconds:
                now = time.time()
                for _ in range(int(elapsed * rate) - sent):
                    payload, fill = encode_position(799_000_000 + sent % fleet, rng.uniform(-60, 60), rng.uniform(-170, 170),
                                                    rng.uniform(0, 20), rng.uniform(0, 359), None, int(now) % 60)  # fmt: skip
                    archive.append(now, to_sentences(payload, fill)[0])
                    sent += 1
                await asyncio.sleep(0.02)
            for _ in range(240):  # wait, up to two minutes, for the sink to store everything we sent
                await asyncio.sleep(0.5)
                if await conn.fetchval("SELECT count(*) FROM positions WHERE source = $1", source) >= sent:
                    break
            row = await conn.fetchrow(
                "SELECT count(*) AS n, percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP "
                "(ORDER BY extract(epoch FROM ingested_at - received_at)::float8) AS p, "
                "max(extract(epoch FROM ingested_at - received_at))::float8 AS worst FROM positions WHERE source = $1",
                source,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await prod.stop()
            archive.close()
            async with conn.transaction():
                await conn.execute("DELETE FROM positions WHERE source LIKE 'loadtest-%'")
                await conn.execute("DELETE FROM vessels WHERE mmsi BETWEEN 799000000 AND 799999999")
            await conn.close()
    assert row is not None
    p50, p95, p99 = row["p"] or [float("nan")] * 3
    return json.dumps({
        "target_rate_per_s": rate, "seconds": seconds, "sent": sent, "stored": row["n"],
        "achieved_rate_per_s": round(sent / seconds), "latency_s": {"p50": round(p50, 3), "p95": round(p95, 3),
        "p99": round(p99, 3), "max": round(row["worst"] or 0, 3)},
    }, indent=2)  # fmt: skip


async def seed_db(cfg: Settings, ships: int, hours: float, seed: int = 7) -> str:
    """Simulated history straight into PostGIS, then coverage and one detector pass. No Kafka involved:
    for a quick look, and for the browser tests. `docker compose up` seeds through the full pipeline instead."""
    from .detect import detect, refresh_coverage
    from .simulate import World
    from .sink import write_batch

    world = World(seed=seed, n_ships=ships)
    decoder = Decoder("seed", cfg)
    end = int(time.time())
    conn = await asyncpg.connect(cfg.dsn)
    try:
        batch: dict[str, list[dict[str, Any]]] = {cfg.topic_positions: [], cfg.topic_static: [], cfg.topic_dlq: []}
        stored = 0

        async def flush() -> None:
            nonlocal stored
            stored += len(
                await write_batch(conn, batch[cfg.topic_positions], batch[cfg.topic_static], batch[cfg.topic_dlq])
            )
            for rows in batch.values():
                rows.clear()

        for i, (_, line) in enumerate(world.sentences(end - int(hours * 3600), end)):
            for topic, _, value in decoder.nmea(line, None, f"seed/history:{i}", ("history", i)):
                batch[topic].append(value)
            if len(batch[cfg.topic_positions]) >= 5000:
                await flush()
        await flush()
        cells = await refresh_coverage(conn, cfg)
        events = await detect(conn, cfg)
    finally:
        await conn.close()
    return f"seeded {stored} positions from {ships} ships over {hours} h; {cells} coverage cells; events: {events}"
