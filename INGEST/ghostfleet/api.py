"""Read-only HTTP API over PostGIS. The contract lives in the models below; docs/openapi.json is generated from them."""

import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import obs
from .config import Settings

log = logging.getLogger(__name__)
REQUESTS = Histogram("ghost_http_request_seconds", "API latency", ["route", "status"],
                     buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))  # fmt: skip
Classification = Literal["dark", "coverage_gap", "position_jump"]
Mmsi = Annotated[
    int, Field(ge=200_000_000, le=799_999_999, description="Maritime Mobile Service Identity of a ship station")
]

# --- contract -----------------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code")
    message: str
    details: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-field problems, when the input was invalid"
    )
    request_id: str


class Error(BaseModel):
    error: ErrorBody


class Ready(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok"]
    newest_position_at: datetime | None = Field(description="Fix time of the newest stored position")
    newest_position_age_s: float | None
    ingest_stale: bool = Field(description="True when nothing has been ingested for over two minutes")


class Live(BaseModel):
    server_time: datetime = Field(description="Pass back as `since` to fetch only what changed")
    data_time: datetime | None = Field(description="Fix time of the newest position in the system")
    truncated: bool = Field(description="True if `limit` cut the result; zoom in or raise the limit")
    vessels: list[tuple[int, float, float, float | None, float | None, int | None, int]] = Field(
        description="Rows of [mmsi, lon, lat, sog_kn, cog_deg, heading_deg, fix_time_unix_s]"
    )


class Vessel(BaseModel):
    mmsi: int
    name: str | None
    callsign: str | None
    imo: int | None
    ship_type: int | None = Field(description="AIS ship type code, 1-255")
    length_m: int | None
    beam_m: int | None
    flag: str | None = Field(description="ISO 3166 alpha-2 of the MMSI's country of registry")
    country: str | None
    first_seen: datetime
    last_seen: datetime | None
    lon: float | None
    lat: float | None
    sog: float | None
    events: int = Field(description="Dark events on record for this vessel")


class VesselHit(BaseModel):
    mmsi: int
    name: str | None
    flag: str | None


class Track(BaseModel):
    mmsi: int
    points: list[tuple[int, float, float, float | None, float | None]] = Field(
        description="Rows of [fix_time_unix_s, lon, lat, sog_kn, cog_deg], oldest first"
    )
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page")


class Trails(BaseModel):
    truncated: bool
    trails: dict[int, list[tuple[float, float, int]]] = Field(
        description="mmsi -> [[lon, lat, fix_time_unix_s], ...], oldest first"
    )


class DarkEvent(BaseModel):
    id: int
    mmsi: int
    name: str | None
    flag: str | None
    classification: Classification
    score: float = Field(description="Suspiciousness, 0-100")
    impossible_speed: bool
    started_at: datetime = Field(description="Last position before the silence")
    ended_at: datetime | None = Field(description="First position after it; null while still silent")
    duration_s: int
    start: tuple[float, float] = Field(description="[lon, lat]")
    end: tuple[float, float] | None
    distance_nm: float | None
    implied_speed_kn: float | None
    start_reliability: float = Field(description="Worst reception (0-1) along the track ahead of the last position")
    end_reliability: float | None
    neighbours_heard: int = Field(description="Other vessels heard nearby during the first 30 minutes of silence")
    reasons: list[str] = Field(description="Plain-language evidence behind the classification and score")


class DarkEvents(BaseModel):
    items: list[DarkEvent]
    next_cursor: str | None


class Coverage(BaseModel):
    cell_deg: float | None = Field(description="Grid cell size in degrees")
    cells: list[tuple[int, int, float]] = Field(
        description="Rows of [cx, cy, reliability]; the cell spans cx*cell_deg..(cx+1)*cell_deg in longitude"
    )
    truncated: bool


class Stats(BaseModel):
    vessels: int
    positions_estimate: int = Field(description="Planner estimate; exact counts are too slow to serve")
    newest_position_at: datetime | None
    events: dict[str, int] = Field(description="Dark events by classification")
    quarantine: dict[str, int] = Field(description="Quarantined inputs by reason")


# --- plumbing -----------------------------------------------------------------------------------


def _cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def _uncursor(cursor: str | None, kinds: tuple[type, ...]) -> list[Any] | None:
    if cursor is None:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (
            not isinstance(values, list)
            or len(values) != len(kinds)
            or not all(isinstance(v, k) and not isinstance(v, bool) for v, k in zip(values, kinds, strict=True))
        ):
            raise ValueError
        return values
    except (ValueError, TypeError):
        raise HTTPException(400, "cursor is not one this API issued") from None


def _bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be four numbers: minLon,minLat,maxLon,maxLat") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(400, "bbox must satisfy -180 <= minLon < maxLon <= 180 and -90 <= minLat < maxLat <= 90")
    return w, s, e, n


BBox = Annotated[str | None, Query(description="minLon,minLat,maxLon,maxLat in degrees", examples=["4,58,12,62"])]
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": Error},
    404: {"model": Error},
    422: {"model": Error},
    503: {"model": Error},
}
_EVENT_COLUMNS = """
    e.id, e.mmsi, v.name, m.iso2 AS flag, e.classification, e.score::numeric::float8 AS score, e.impossible_speed, e.started_at, e.ended_at,
    e.duration_s, ST_X(e.start_geog::geometry) AS lon0, ST_Y(e.start_geog::geometry) AS lat0,
    ST_X(e.end_geog::geometry) AS lon1, ST_Y(e.end_geog::geometry) AS lat1, e.distance_m, round(e.implied_speed_kn::numeric, 1)::float8 AS implied_speed_kn,
    round(e.start_reliability::numeric, 2)::float8 AS start_reliability,
    round(e.end_reliability::numeric, 2)::float8 AS end_reliability, e.neighbours_heard, e.reasons
FROM dark_events e JOIN vessels v USING (mmsi) LEFT JOIN mid_countries m ON m.mid = v.mid
"""


def _event(r: asyncpg.Record) -> DarkEvent:
    return DarkEvent(
        id=r["id"], mmsi=r["mmsi"], name=r["name"], flag=r["flag"], classification=r["classification"], score=r["score"],
        impossible_speed=r["impossible_speed"], started_at=r["started_at"], ended_at=r["ended_at"], duration_s=r["duration_s"],
        start=(r["lon0"], r["lat0"]), end=None if r["lon1"] is None else (r["lon1"], r["lat1"]),
        distance_nm=None if r["distance_m"] is None else round(r["distance_m"] / 1852, 1), implied_speed_kn=r["implied_speed_kn"],
        start_reliability=r["start_reliability"], end_reliability=r["end_reliability"], neighbours_heard=r["neighbours_heard"],
        reasons=json.loads(r["reasons"]),
    )  # fmt: skip


def create_app(cfg: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The pool is created lazily: the API must start, and say why it is not ready, with the database down.
        app.state.pool = None
        yield
        if getattr(app.state, "pool", None) is not None:
            await app.state.pool.close()

    app = FastAPI(
        title="GHOST FLEET", version="1", lifespan=lifespan,
        description="Live vessel positions, tracks, and ranked dark events. Read-only; all times are UTC.",
    )  # fmt: skip
    if cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(cfg.cors_origins), allow_methods=["GET"])

    async def conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
        try:
            if getattr(request.app.state, "pool", None) is None:
                request.app.state.pool = await asyncpg.create_pool(
                    cfg.dsn, min_size=0, max_size=8, command_timeout=10, timeout=3
                )
            async with request.app.state.pool.acquire(timeout=3) as c:
                yield c
        except (
            OSError,
            TimeoutError,
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            asyncpg.CannotConnectNowError,
        ) as e:
            log.error("database unavailable", extra={"error": repr(e)})
            raise HTTPException(503, "the database is unavailable; retry shortly") from None

    Db = Annotated[asyncpg.Connection, Depends(conn)]

    def error(
        status: int, code: str, message: str, request: Request, details: list[dict[str, Any]] | None = None
    ) -> JSONResponse:
        body = Error(
            error=ErrorBody(code=code, message=message, details=details or [], request_id=request.state.request_id)
        )
        headers = {"Retry-After": "5"} if status == 503 else None
        return JSONResponse(body.model_dump(), status_code=status, headers=headers)

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied if supplied.isascii() and 0 < len(supplied) <= 64 and supplied.isprintable() else uuid.uuid4().hex
        )
        obs.correlation_id.set(request.state.request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # anything unhandled: log it with the id, tell the client only the id
            log.exception("unhandled error", extra={"path": request.url.path})
            response = error(500, "internal_error", "something went wrong on our side; quote the request id", request)
        elapsed = time.perf_counter() - started
        route = getattr(request.scope.get("route"), "path", "unmatched")
        REQUESTS.labels(route, response.status_code).observe(elapsed)
        response.headers["X-Request-ID"] = request.state.request_id
        if route not in ("/healthz", "/metrics"):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round(elapsed * 1000, 1),
                },
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
        return error(422, "invalid_request", "one or more parameters are invalid", request, details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 503: "unavailable"}
        return error(exc.status_code, codes.get(exc.status_code, "error"), str(exc.detail), request)

    # --- operations ---

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}  # liveness only: is the process serving? Dependencies are /readyz's job.

    @app.get("/readyz", response_model=Ready, responses={503: {"model": Error}}, tags=["operations"])
    async def readyz(db: Db) -> Ready:
        """Runs a real query. 503 if the database cannot answer; `degraded` if it answers but ingestion has stalled."""
        newest, age = await db.fetchrow(
            "SELECT max(ts), extract(epoch FROM now() - max(ingested_at))::float8 FROM (SELECT ts, ingested_at FROM positions ORDER BY ts DESC LIMIT 1000) t"
        )
        stale = age is None or age > 120
        return Ready(status="degraded" if stale else "ok", database="ok", newest_position_at=newest,
                     newest_position_age_s=None if age is None else round(age, 1), ingest_stale=stale)  # fmt: skip

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --- vessels ---

    @app.get("/api/v1/vessels/live", response_model=Live, responses=_ERRORS, tags=["vessels"])
    async def live(
        db: Db, response: Response, bbox: BBox = None,
        since: Annotated[datetime | None, Query(description="Only vessels updated after this instant (a previous `server_time`)")] = None,
        max_age_h: Annotated[float, Query(gt=0, le=168, description="Hide vessels not heard for this long, relative to the newest data")] = 24,
        limit: Annotated[int, Query(ge=1, le=100_000)] = 50_000,
    ) -> Live:  # fmt: skip
        """Latest known position of every vessel in view. Poll with `since` to receive only changes."""
        box = _bbox(bbox)
        server_time = await db.fetchval("SELECT clock_timestamp()")
        data_time = await db.fetchval("SELECT max(ts) FROM vessel_latest")
        rows = await db.fetch(
            """
            SELECT mmsi, ST_X(geom) AS lon, ST_Y(geom) AS lat, sog::numeric::float8 AS sog, cog::numeric::float8 AS cog,
                   heading, extract(epoch FROM ts)::bigint AS t
            FROM vessel_latest
            WHERE ($1::float8 IS NULL OR geom && ST_MakeEnvelope($1, $2, $3, $4, 4326))
              AND ($5::timestamptz IS NULL OR updated_at > $5)
              AND ts >= $6::timestamptz
            ORDER BY mmsi LIMIT $7
            """,
            *(box or (None, None, None, None)), since, (data_time or server_time) - timedelta(hours=max_age_h), limit + 1,
        )  # fmt: skip
        response.headers["Cache-Control"] = "no-store"
        return Live(server_time=server_time, data_time=data_time, truncated=len(rows) > limit,
                    vessels=[(r["mmsi"], r["lon"], r["lat"], r["sog"], r["cog"], r["heading"], r["t"]) for r in rows[:limit]])  # fmt: skip

    @app.get("/api/v1/vessels/trails", response_model=Trails, responses=_ERRORS, tags=["vessels"])
    async def trails(
        db: Db, bbox: Annotated[str, Query(description="minLon,minLat,maxLon,maxLat in degrees")],
        minutes: Annotated[int, Query(ge=1, le=720)] = 60,
        max_vessels: Annotated[int, Query(ge=1, le=2000)] = 500,
    ) -> Trails:  # fmt: skip
        """Recent track of each vessel currently in the box. Meant for a zoomed-in map: capped at `max_vessels`."""
        box = _bbox(bbox)
        assert box is not None  # bbox is a required parameter here
        rows = await db.fetch(
            """
            WITH now_data AS (SELECT max(ts) AS t FROM vessel_latest),
            inside AS (
                SELECT mmsi FROM vessel_latest, now_data
                WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326) AND ts >= t - make_interval(mins => $5)
                ORDER BY mmsi LIMIT $6
            )
            SELECT p.mmsi, ST_X(p.geog::geometry) AS lon, ST_Y(p.geog::geometry) AS lat, extract(epoch FROM p.ts)::bigint AS t
            FROM inside JOIN positions p USING (mmsi), now_data
            WHERE p.ts >= now_data.t - make_interval(mins => $5)
            ORDER BY p.mmsi, p.ts
            """,
            *box, minutes, max_vessels + 1,
        )  # fmt: skip
        out: dict[int, list[tuple[float, float, int]]] = {}
        for r in rows:
            out.setdefault(r["mmsi"], []).append((r["lon"], r["lat"], r["t"]))
        truncated = len(out) > max_vessels
        if truncated:
            out.pop(max(out))
        return Trails(truncated=truncated, trails=out)

    @app.get("/api/v1/vessels/search", response_model=list[VesselHit], responses=_ERRORS, tags=["vessels"])
    async def search(
        db: Db,
        q: Annotated[str, Query(min_length=2, max_length=40, description="Name fragment, or leading MMSI digits")],
    ) -> list[VesselHit]:
        """Up to 20 best matches, MMSI order. A bounded lookup, not a listing."""
        if q.isdigit():
            lo = int(q.ljust(9, "0"))
            rows = await db.fetch(
                "SELECT v.mmsi, v.name, m.iso2 AS flag FROM vessels v LEFT JOIN mid_countries m ON m.mid = v.mid "
                "WHERE v.mmsi BETWEEN $1 AND $2 ORDER BY v.mmsi LIMIT 20", lo, int(q.ljust(9, "9")),
            )  # fmt: skip
        else:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = await db.fetch(
                "SELECT v.mmsi, v.name, m.iso2 AS flag FROM vessels v LEFT JOIN mid_countries m ON m.mid = v.mid "
                "WHERE v.name ILIKE $1 ORDER BY v.mmsi LIMIT 20", pattern,
            )  # fmt: skip
        return [VesselHit(**dict(r)) for r in rows]

    @app.get("/api/v1/vessels/{mmsi}", response_model=Vessel, responses=_ERRORS, tags=["vessels"])
    async def vessel(db: Db, mmsi: Mmsi) -> Vessel:
        r = await db.fetchrow(
            """
            SELECT v.mmsi, v.name, v.callsign, v.imo, v.ship_type, v.length_m, v.beam_m, m.iso2 AS flag, m.country,
                   v.first_seen, l.ts AS last_seen, ST_X(l.geom) AS lon, ST_Y(l.geom) AS lat, l.sog::numeric::float8 AS sog,
                   (SELECT count(*) FROM dark_events e WHERE e.mmsi = v.mmsi) AS events
            FROM vessels v LEFT JOIN mid_countries m ON m.mid = v.mid LEFT JOIN vessel_latest l USING (mmsi)
            WHERE v.mmsi = $1
            """,
            mmsi,
        )
        if r is None:
            raise HTTPException(404, f"no vessel with MMSI {mmsi} has been seen")
        return Vessel(**dict(r))

    @app.get("/api/v1/vessels/{mmsi}/track", response_model=Track, responses=_ERRORS, tags=["vessels"])
    async def track(
        db: Db, mmsi: Mmsi,
        start: Annotated[datetime | None, Query(description="Inclusive lower bound, ISO 8601")] = None,
        end: Annotated[datetime | None, Query(description="Exclusive upper bound, ISO 8601")] = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 5000,
        cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    ) -> Track:  # fmt: skip
        """A vessel's positions, oldest first, paged by fix time (the sort key is the primary key, so pages are stable)."""
        after = _uncursor(cursor, (int,))
        if start and end and start >= end:
            raise HTTPException(400, "start must be before end")
        rows = await db.fetch(
            """
            SELECT extract(epoch FROM ts)::bigint AS t, ST_X(geog::geometry) AS lon, ST_Y(geog::geometry) AS lat,
                   sog::numeric::float8 AS sog, cog::numeric::float8 AS cog
            FROM positions
            WHERE mmsi = $1 AND ($2::timestamptz IS NULL OR ts >= $2) AND ($3::timestamptz IS NULL OR ts < $3)
              AND ($4::bigint IS NULL OR ts > to_timestamp($4))
            ORDER BY ts LIMIT $5
            """,
            mmsi, _utc(start), _utc(end), after[0] if after else None, limit + 1,
        )  # fmt: skip
        if (
            not rows
            and after is None
            and not await db.fetchval("SELECT EXISTS (SELECT FROM vessels WHERE mmsi = $1)", mmsi)
        ):
            raise HTTPException(404, f"no vessel with MMSI {mmsi} has been seen")
        page = rows[:limit]
        return Track(mmsi=mmsi, points=[(r["t"], r["lon"], r["lat"], r["sog"], r["cog"]) for r in page],
                     next_cursor=_cursor([page[-1]["t"]]) if len(rows) > limit else None)  # fmt: skip

    # --- dark events ---

    @app.get("/api/v1/dark-events", response_model=DarkEvents, responses=_ERRORS, tags=["dark events"])
    async def dark_events(
        db: Db,
        classification: Annotated[Classification | None, Query()] = None,
        min_score: Annotated[float, Query(ge=0, le=100)] = 0,
        impossible_only: Annotated[bool, Query(description="Only reappearances that needed an impossible speed")] = False,
        mmsi: Annotated[Mmsi | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    ) -> DarkEvents:  # fmt: skip
        """Silences ranked by suspiciousness, highest first. Ties break on id, so paging never skips or repeats."""
        after = _uncursor(cursor, (float, int))
        rows = await db.fetch(
            f"""
            SELECT {_EVENT_COLUMNS}
            WHERE ($1::text IS NULL OR e.classification = $1) AND e.score >= $2 AND (NOT $3 OR e.impossible_speed)
              AND ($4::int IS NULL OR e.mmsi = $4) AND ($5::real IS NULL OR (e.score, e.id) < ($5::real, $6::bigint))
            ORDER BY e.score DESC, e.id DESC LIMIT $7
            """,
            classification, min_score, impossible_only, mmsi, after[0] if after else None, after[1] if after else None, limit + 1,
        )  # fmt: skip
        page = rows[:limit]
        return DarkEvents(items=[_event(r) for r in page],
                          next_cursor=_cursor([float(page[-1]["score"]), page[-1]["id"]]) if len(rows) > limit else None)  # fmt: skip

    @app.get("/api/v1/dark-events/{event_id}", response_model=DarkEvent, responses=_ERRORS, tags=["dark events"])
    async def dark_event(db: Db, event_id: Annotated[int, Field(ge=1)]) -> DarkEvent:
        r = await db.fetchrow(f"SELECT {_EVENT_COLUMNS} WHERE e.id = $1", event_id)
        if r is None:
            raise HTTPException(404, f"no dark event {event_id}; events disappear if late data fills the gap")
        return _event(r)

    # --- context ---

    @app.get("/api/v1/coverage", response_model=Coverage, responses=_ERRORS, tags=["context"])
    async def coverage(
        db: Db, response: Response, bbox: BBox = None, limit: Annotated[int, Query(ge=1, le=50_000)] = 20_000
    ) -> Coverage:
        """The empirical reception grid the detector reasons with."""
        box = _bbox(bbox)
        rows = await db.fetch(
            "SELECT cx, cy, round(reliability::numeric, 3)::float8 AS reliability, cell_deg::numeric::float8 AS cell_deg FROM coverage_cells "
            "WHERE ($1::float8 IS NULL OR geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)) ORDER BY cx, cy LIMIT $5",
            *(box or (None, None, None, None)), limit + 1,
        )  # fmt: skip
        response.headers["Cache-Control"] = "public, max-age=300"
        return Coverage(cell_deg=rows[0]["cell_deg"] if rows else None, truncated=len(rows) > limit,
                        cells=[(r["cx"], r["cy"], r["reliability"]) for r in rows[:limit]])  # fmt: skip

    @app.get("/api/v1/stats", response_model=Stats, responses=_ERRORS, tags=["context"])
    async def stats(db: Db) -> Stats:
        return Stats(
            vessels=await db.fetchval("SELECT count(*) FROM vessel_latest"),
            positions_estimate=max(
                0, await db.fetchval("SELECT reltuples::bigint FROM pg_class WHERE oid = 'positions'::regclass")
            ),
            newest_position_at=await db.fetchval("SELECT max(ts) FROM vessel_latest"),
            events={
                r["classification"]: r["n"]
                for r in await db.fetch("SELECT classification, count(*) AS n FROM dark_events GROUP BY 1")
            },
            quarantine={
                r["reason"]: r["n"] for r in await db.fetch("SELECT reason, count(*) AS n FROM quarantine GROUP BY 1")
            },
        )

    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


def _utc(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)
