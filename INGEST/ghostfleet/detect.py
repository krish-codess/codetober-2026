"""Gap detection: which silences are the receiver network's fault, and which are the vessel's?

A silence is blamed on the vessel ("dark") only when the evidence says we would have heard it:

  1. where it was heading when it vanished is water we reliably hear  (coverage grid, dead reckoning)
  2. where it came back is water we reliably hear, on the track it arrived by
  3. somebody else was heard right there while it was silent          (the receiver was up)

Anything less is a coverage gap. That is deliberately conservative: an investigator's time is the
scarce resource, so the ranking would rather miss a dark ship than cry wolf at the edge of range.

`assess` is pure and carries every rule; the rest of the module fetches its inputs and stores its outputs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import log10

import asyncpg

from . import db, obs
from .collectors import Backoff
from .config import Settings
from .geo import KNOT_MS, cell_of, destination

log = logging.getLogger(__name__)
NEIGHBOUR_WINDOW_S = 1800
JUMP_MIN_M = 18_520  # 10 nm: below this an "impossible speed" is GPS jitter, not a teleport
Coverage = dict[tuple[int, int], float]


@dataclass(frozen=True, slots=True)
class Gap:
    mmsi: int
    t0: datetime
    lat0: float
    lon0: float
    sog0: float | None
    cog0: float | None
    duration_s: float
    # None while the vessel has not been heard again:
    t1: datetime | None = None
    lat1: float | None = None
    lon1: float | None = None
    sog1: float | None = None
    cog1: float | None = None
    dist_m: float | None = None


@dataclass(frozen=True, slots=True)
class Assessment:
    classification: str
    impossible_speed: bool
    implied_speed_kn: float | None
    score: float
    reasons: list[str]


def projected(lat: float, lon: float, sog: float | None, cog: float | None, seconds: float) -> tuple[float, float]:
    """Dead reckoning: where a vessel holding its course and speed is `seconds` from now (negative: was)."""
    if not sog or cog is None:
        return lat, lon
    return destination(lat, lon, cog if seconds >= 0 else (cog + 180) % 360, sog * KNOT_MS * abs(seconds))


def reliability(
    cov: Coverage, lat: float, lon: float, sog: float | None, cog: float | None, sign: int, cell_deg: float
) -> float:
    """Worst reception along 30 minutes of track: forward from a disappearance, backward from a reappearance."""
    points = [projected(lat, lon, sog, cog, sign * s) for s in (0, 900, 1800)]
    return min(cov.get(cell_of(la, lo, cell_deg), 0.0) for la, lo in points)


def _hm(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def assess(gap: Gap, rel_start: float, rel_end: float | None, neighbours: int, s: Settings) -> Assessment:
    reasons = [f"silent for {_hm(gap.duration_s)}" + ("" if gap.t1 else ", not yet heard again")]
    speed = None
    impossible = False
    if gap.dist_m is not None:
        speed = gap.dist_m / gap.duration_s / KNOT_MS
        impossible = speed > s.max_speed_kn and gap.dist_m >= JUMP_MIN_M
    moving = (gap.sog0 or 0) >= 1 or (gap.dist_m or 0) > 5000
    if gap.duration_s < (s.gap_moving_s if moving else s.gap_stationary_s):
        cls = "position_jump"  # only here because of the jump; too brief to be a dark period
    elif rel_start >= s.coverage_min and (rel_end is None or rel_end >= s.coverage_min) and neighbours >= 1:
        cls = "dark"
    else:
        cls = "coverage_gap"

    score = {"dark": 45.0, "position_jump": 30.0, "coverage_gap": 5.0}[cls]
    if cls == "dark":
        reasons.append(
            f"area was covered: reception {rel_start:.2f} where it vanished"
            + (f", {rel_end:.2f} where it returned" if rel_end is not None else "")
        )
        reasons.append(f"{neighbours} other vessel{'s' if neighbours != 1 else ''} heard nearby during the silence")
        score += min(10.0, 10 * gap.duration_s / 43_200) + min(10.0, 2.0 * neighbours)
        if (gap.sog0 or 0) >= 5:
            score += 5
            reasons.append(f"under way at {gap.sog0:.1f} kn when it vanished")
    elif cls == "coverage_gap":
        if rel_start < s.coverage_min:
            reasons.append(f"was leaving reliable coverage (reception {rel_start:.2f} ahead of its last position)")
        if rel_end is not None and rel_end < s.coverage_min:
            reasons.append(
                f"returned from outside reliable coverage (reception {rel_end:.2f} behind its first position)"
            )
        if neighbours == 0:
            reasons.append("no other vessel heard nearby during the silence: the receiver may have been down")
    if impossible and speed is not None and gap.dist_m is not None:
        score += 30 + min(10.0, 5 * log10(speed / s.max_speed_kn))
        reasons.append(
            f"reappeared {gap.dist_m / 1852:.0f} nm away: needs {speed:.0f} kn, limit {s.max_speed_kn:.0f} kn"
        )
    return Assessment(cls, impossible, speed, round(min(100.0, score), 1), reasons)


# --- database ---------------------------------------------------------------------------------

_REFRESH_COVERAGE = """
WITH bounds AS (
    SELECT date_trunc('hour', max(ts)) AS hi FROM positions
), win AS (
    SELECT hi - make_interval(hours => $2) AS lo, hi FROM bounds
), hours AS (
    -- Hours in which we heard anything at all. An hour our own collector was down penalises no cell.
    SELECT count(DISTINCT date_trunc('hour', ts)) AS n FROM positions, win WHERE ts >= lo AND ts < hi
), agg AS (
    SELECT floor(ST_X(geog::geometry) / $1)::int AS cx, floor(ST_Y(geog::geometry) / $1)::int AS cy,
           count(DISTINCT date_trunc('hour', ts)) AS heard, count(DISTINCT mmsi) AS vessels
    FROM positions, win WHERE ts >= lo AND ts < hi GROUP BY 1, 2
)
INSERT INTO coverage_cells (cx, cy, cell_deg, hours_heard, hours_window, vessels, reliability, geom, computed_at)
SELECT cx, cy, $1, heard, n, vessels, heard::real / n,
       ST_MakeEnvelope(cx * $1, cy * $1, (cx + 1) * $1, (cy + 1) * $1, 4326), $3
FROM agg, hours
ON CONFLICT (cx, cy) DO UPDATE SET cell_deg = excluded.cell_deg, hours_heard = excluded.hours_heard,
    hours_window = excluded.hours_window, vessels = excluded.vessels, reliability = excluded.reliability,
    geom = excluded.geom, computed_at = excluded.computed_at
"""

_GAPS = """
WITH w AS (
    SELECT mmsi, ts, geog, sog, cog, lag(ts) OVER v AS p_ts, lag(geog) OVER v AS p_geog,
           lag(sog) OVER v AS p_sog, lag(cog) OVER v AS p_cog
    FROM positions WHERE ts >= $1 WINDOW v AS (PARTITION BY mmsi ORDER BY ts)
), d AS (
    SELECT *, ST_Distance(p_geog, geog) AS dist_m, extract(epoch FROM ts - p_ts)::float8 AS secs FROM w WHERE p_ts IS NOT NULL
)
SELECT mmsi, p_ts AS t0, ST_Y(p_geog::geometry) AS lat0, ST_X(p_geog::geometry) AS lon0, p_sog AS sog0, p_cog AS cog0,
       secs AS duration_s, ts AS t1, ST_Y(geog::geometry) AS lat1, ST_X(geog::geometry) AS lon1, sog AS sog1, cog AS cog1, dist_m
FROM d
WHERE secs >= CASE WHEN coalesce(p_sog, 0) >= 1 OR dist_m > 5000 THEN $2::float8 ELSE $3::float8 END
   OR (dist_m >= $5::float8 AND dist_m / secs > $4::float8)
UNION ALL
-- Still silent. "Now" is the newest data we hold, not the wall clock: if ingestion stalls, the whole
-- fleet must not be reported dark.
SELECT l.mmsi, l.ts, ST_Y(l.geom), ST_X(l.geom), l.sog, l.cog,
       extract(epoch FROM $6::timestamptz - l.ts)::float8, NULL, NULL, NULL, NULL, NULL, NULL
FROM vessel_latest l
WHERE l.ts >= $1 AND l.ts <= $6::timestamptz - make_interval(secs => $2::float8) AND l.sog >= 1
"""

_NEIGHBOURS = """
SELECT g.i, count(DISTINCT p.mmsi) AS n
FROM unnest($1::int[], $2::int[], $3::float8[], $4::float8[], $5::timestamptz[], $6::timestamptz[]) AS g (i, mmsi, lon, lat, t0, t1)
LEFT JOIN LATERAL (
    SELECT p.mmsi FROM positions p
    WHERE p.ts > g.t0 AND p.ts <= g.t1 AND p.mmsi <> g.mmsi
      AND ST_DWithin(p.geog, ST_SetSRID(ST_MakePoint(g.lon, g.lat), 4326)::geography, $7)
) p ON true
GROUP BY g.i
"""

_UPSERT = """
INSERT INTO dark_events (mmsi, started_at, ended_at, start_geog, end_geog, duration_s, distance_m, implied_speed_kn,
                         start_sog, start_reliability, end_reliability, neighbours_heard, classification,
                         impossible_speed, score, reasons, detected_at)
SELECT mmsi, t0, t1, ST_SetSRID(ST_MakePoint(lon0, lat0), 4326)::geography,
       ST_SetSRID(ST_MakePoint(lon1, lat1), 4326)::geography, dur, dist, speed, sog0, rel0, rel1, nb, cls, imp, score,
       reasons::jsonb, now()
FROM unnest($1::int[], $2::timestamptz[], $3::timestamptz[], $4::float8[], $5::float8[], $6::float8[], $7::float8[],
            $8::int[], $9::float8[], $10::real[], $11::real[], $12::real[], $13::real[], $14::int[], $15::text[],
            $16::bool[], $17::real[], $18::text[])
     AS t (mmsi, t0, t1, lon0, lat0, lon1, lat1, dur, dist, speed, sog0, rel0, rel1, nb, cls, imp, score, reasons)
ON CONFLICT (mmsi, started_at) DO UPDATE SET
    ended_at = excluded.ended_at, end_geog = excluded.end_geog, duration_s = excluded.duration_s,
    distance_m = excluded.distance_m, implied_speed_kn = excluded.implied_speed_kn, start_sog = excluded.start_sog,
    start_reliability = excluded.start_reliability, end_reliability = excluded.end_reliability,
    neighbours_heard = excluded.neighbours_heard, classification = excluded.classification,
    impossible_speed = excluded.impossible_speed, score = excluded.score, reasons = excluded.reasons
"""
# Late data can fill a gap after it was reported. An event the positions no longer support goes away.
_DELETE_STALE = """
DELETE FROM dark_events e
WHERE e.started_at >= $1
  AND NOT EXISTS (SELECT FROM unnest($2::int[], $3::timestamptz[]) AS k (mmsi, t0) WHERE k.mmsi = e.mmsi AND k.t0 = e.started_at)
"""


async def refresh_coverage(conn: asyncpg.Connection, s: Settings) -> int:
    stamp = datetime.now(UTC)
    async with conn.transaction():
        await conn.execute(_REFRESH_COVERAGE, s.coverage_cell_deg, s.lookback_h, stamp)
        await conn.execute("DELETE FROM coverage_cells WHERE computed_at < $1", stamp)
    return int(await conn.fetchval("SELECT count(*) FROM coverage_cells"))


async def detect(conn: asyncpg.Connection, s: Settings) -> dict[str, int]:
    """One idempotent pass over the lookback window. Safe to run at any time, any number of times."""
    now = await conn.fetchval("SELECT max(ts) FROM vessel_latest")
    if now is None:
        return {}
    since = now - timedelta(hours=s.lookback_h)
    rows = await conn.fetch(
        _GAPS, since, float(s.gap_moving_s), float(s.gap_stationary_s), s.max_speed_kn * KNOT_MS, float(JUMP_MIN_M), now
    )
    gaps = [Gap(**dict(r)) for r in rows]
    cov: Coverage = {
        (r["cx"], r["cy"]): r["reliability"] for r in await conn.fetch("SELECT cx, cy, reliability FROM coverage_cells")
    }

    # Was anyone heard, during the first half hour of silence, around where this vessel should have been?
    spots = [projected(g.lat0, g.lon0, g.sog0, g.cog0, min(900.0, float(g.duration_s) / 2)) for g in gaps]
    heard = dict.fromkeys(range(len(gaps)), 0)
    if gaps:
        ends = [g.t0 + timedelta(seconds=min(NEIGHBOUR_WINDOW_S, float(g.duration_s))) for g in gaps]
        for r in await conn.fetch(
            _NEIGHBOURS, list(range(len(gaps))), [g.mmsi for g in gaps], [p[1] for p in spots], [p[0] for p in spots],
            [g.t0 for g in gaps], ends, s.neighbour_radius_m,
        ):  # fmt: skip
            heard[r["i"]] = r["n"]

    out: list[tuple[Gap, float, float | None, int, Assessment]] = []
    for i, g in enumerate(gaps):
        rel0 = reliability(cov, g.lat0, g.lon0, g.sog0, g.cog0, +1, s.coverage_cell_deg)
        rel1 = (
            None
            if g.lat1 is None or g.lon1 is None
            else reliability(cov, g.lat1, g.lon1, g.sog1, g.cog1, -1, s.coverage_cell_deg)
        )
        out.append((g, rel0, rel1, heard[i], assess(g, rel0, rel1, heard[i], s)))

    async with conn.transaction():
        await conn.execute(
            _UPSERT, [g.mmsi for g, *_ in out], [g.t0 for g, *_ in out], [g.t1 for g, *_ in out],
            [g.lon0 for g, *_ in out], [g.lat0 for g, *_ in out], [g.lon1 for g, *_ in out], [g.lat1 for g, *_ in out],
            [max(1, round(float(g.duration_s))) for g, *_ in out], [g.dist_m for g, *_ in out], [a.implied_speed_kn for *_, a in out],
            [g.sog0 for g, *_ in out], [r0 for _, r0, *_ in out], [r1 for _, _, r1, *_ in out], [n for *_, n, _ in out],
            [a.classification for *_, a in out], [a.impossible_speed for *_, a in out], [a.score for *_, a in out],
            [json.dumps(a.reasons) for *_, a in out],
        )  # fmt: skip
        await conn.execute(_DELETE_STALE, since, [g.mmsi for g, *_ in out], [g.t0 for g, *_ in out])
    counts: dict[str, int] = {}
    for *_, a in out:
        counts[a.classification] = counts.get(a.classification, 0) + 1
    return counts


async def run(s: Settings, *, coverage_every: int = 10) -> None:
    obs.serve_metrics(s.metrics_port)
    backoff = Backoff(s.reconnect_base_s, s.reconnect_cap_s)
    n = 0
    while True:
        try:
            pool = await db.pool(s.dsn, size=2)
            try:
                while True:
                    obs.correlation_id.set(f"detect/{int(time.time())}")
                    async with pool.acquire() as conn:
                        with obs.DETECT_RUNS.time():
                            if n % coverage_every == 0:
                                cells = await refresh_coverage(conn, s)
                                log.info("coverage refreshed", extra={"cells": cells})
                            counts = await detect(conn, s)
                    n += 1
                    for cls in ("dark", "coverage_gap", "position_jump"):
                        obs.DARK_EVENTS.labels(cls).set(counts.get(cls, 0))
                    obs.LAST_SUCCESS.set_to_current_time()
                    backoff.reset()
                    log.info("detector pass", extra=counts)
                    await asyncio.sleep(s.detect_interval_s)
            finally:
                await pool.close()
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            delay = backoff.next()
            log.warning("detector interrupted, retrying", extra={"error": repr(e), "retry_in_s": round(delay, 2)})
            await asyncio.sleep(delay)
