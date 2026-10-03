"""/v1 endpoints. Every query is parameterised; every input is validated before it reaches SQL."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from goldstandard.api.errors import ApiError
from goldstandard.api.models import (
    ActivityOut,
    Attribution,
    DivisionOut,
    FlowPoint,
    FlowsResponse,
    Freshness,
    HealthCheck,
    IndexPoint,
    IndexResponse,
    InflationCell,
    InflationMatrix,
    InflationPoint,
    InflationResponse,
    ItemOut,
    ItemPower,
    ManipulationOut,
    ManipulationPage,
    Page,
    PatchImpact,
    PatchIn,
    PatchOut,
    PatchPage,
    PowerPoint,
    PurchasingPowerResponse,
    ReadyResponse,
    Revision,
    RevisionsResponse,
    SeriesRef,
    ServerOut,
    ShockOut,
    ShocksResponse,
    WageRate,
    WorldOut,
    Yield,
)
from goldstandard.config import settings
from goldstandard.index import BASKET_VALUE
from goldstandard.reference import tag_divisions

router = APIRouter(prefix="/v1")
MAX_SPAN_DAYS = 1830
SlugQ = Annotated[str, Query(pattern=r"^[a-z0-9-]{1,40}$")]
ScopeQ = Annotated[str, Query(pattern=r"^[a-z0-9-]{1,40}$", description="server id, or 'all'")]
DivisionQ = Annotated[str, Query(pattern=r"^[a-z_]{1,32}$", description="division id, or 'all'")]


DictConn = psycopg.Connection[dict[str, Any]]


def conn(request: Request) -> Iterator[DictConn]:
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as c:
        c.row_factory = dict_row  # type: ignore[assignment]  # psycopg cannot narrow the generic row type here
        yield cast(DictConn, c)


Conn = Annotated[DictConn, Depends(conn)]


# ------------------------------------------------------------------------------------------ helpers
def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or date(9999, 12, 31)
    start = start or date(2000, 1, 1)
    if start > end:
        raise ApiError(400, "bad_range", "'from' must be on or before 'to'")
    if end != date(9999, 12, 31) and start != date(2000, 1, 1) and (end - start).days > MAX_SPAN_DAYS:
        raise ApiError(400, "range_too_large", f"date range may span at most {MAX_SPAN_DAYS} days")
    return start, end


def _world(c: DictConn, world: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM world WHERE world_id = %s", (world,)).fetchone()
    if row is None:
        raise ApiError(404, "unknown_world", f"world '{world}' does not exist")
    return row


def _series(c: DictConn, world: str, server: str, division: str) -> SeriesRef:
    _world(c, world)
    row = c.execute(
        """SELECT series_id FROM index_series WHERE world_id = %s
           AND server_id IS NOT DISTINCT FROM %s AND division_id IS NOT DISTINCT FROM %s""",
        (world, None if server == "all" else server, None if division == "all" else division),
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "unknown_series", f"no index series for server '{server}' and division '{division}' in '{world}'"
        )
    return SeriesRef(world_id=world, server_id=server, division_id=division, series_id=row["series_id"])


def _freshness(c: DictConn, world: str) -> Freshness:
    row = c.execute(
        """SELECT max(v.day) AS last_day FROM index_value_current v JOIN index_series s USING (series_id)
           WHERE s.world_id = %s AND s.server_id IS NULL AND s.division_id IS NULL AND v.value IS NOT NULL""",
        (world,),
    ).fetchone()
    last = row["last_day"] if row else None
    if last is None:
        return Freshness(last_day=None, age_hours=None, stale=True)
    end_of_day = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    age = (datetime.now(UTC) - end_of_day).total_seconds() / 3600
    return Freshness(last_day=last, age_hours=round(max(age, 0.0), 1), stale=age > settings().stale_after_hours)


def _encode_cursor(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, default=str).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, keys: tuple[str, ...]) -> dict[str, Any]:
    try:
        obj = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(obj, dict) or set(obj) != set(keys):
            raise ValueError
        return obj
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed; restart from the first page") from exc


# ------------------------------------------------------------------------------------------ health
def readiness(pool: ConnectionPool) -> ReadyResponse:
    checks: dict[str, HealthCheck] = {}
    fresh: dict[str, Freshness] = {}
    t0 = time.perf_counter()
    try:
        with pool.connection(timeout=2) as raw:
            raw.row_factory = dict_row  # type: ignore[assignment]
            c = cast(DictConn, raw)
            c.execute("SELECT 1")
            checks["postgres"] = HealthCheck(ok=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            worlds = [r["world_id"] for r in c.execute("SELECT world_id FROM world ORDER BY 1").fetchall()]
            for w in worlds:
                fresh[w] = _freshness(c, w)
            runs = c.execute("""SELECT DISTINCT ON (job) job, status, finished_at FROM ingest_run
                                ORDER BY job, started_at DESC""").fetchall()
            failed = [r["job"] for r in runs if r["status"] == "failed"]
            checks["ingestion"] = HealthCheck(
                ok=not failed, detail=f"last run failed: {', '.join(failed)}" if failed else None
            )
    except Exception as exc:  # any failure here means "not ready"
        checks["postgres"] = HealthCheck(ok=False, detail=type(exc).__name__)
        return ReadyResponse(status="unavailable", checks=checks, freshness=fresh)
    degraded = any(f.stale for f in fresh.values()) or not all(ch.ok for ch in checks.values()) or not fresh
    return ReadyResponse(status="degraded" if degraded else "ok", checks=checks, freshness=fresh)


# ------------------------------------------------------------------------------------------ reference
@router.get(
    "/worlds", response_model=list[WorldOut], tags=["reference"], summary="Worlds, servers, divisions, activities"
)
def worlds(c: Conn) -> list[WorldOut]:
    out = []
    for w in c.execute("SELECT * FROM world ORDER BY is_synthetic, world_id").fetchall():
        wid = w["world_id"]
        servers = c.execute(
            "SELECT server_id, name FROM server WHERE world_id = %s ORDER BY server_id", (wid,)
        ).fetchall()
        acts = c.execute("SELECT activity_id, label FROM activity WHERE world_id = %s ORDER BY 1", (wid,)).fetchall()
        first = c.execute(
            """SELECT min(day) AS d FROM index_value_current v JOIN index_series s USING (series_id)
                             WHERE s.world_id = %s AND s.server_id IS NULL AND s.division_id IS NULL""",
            (wid,),
        ).fetchone()
        out.append(
            WorldOut(
                world_id=wid,
                name=w["name"],
                price_source=w["price_source"],
                is_synthetic=w["is_synthetic"],
                currency=w["currency"],
                servers=[ServerOut(**r) for r in servers],
                divisions=[
                    DivisionOut(**r) for r in c.execute("SELECT division_id, label FROM division ORDER BY 1").fetchall()
                ],
                activities=[ActivityOut(**r) for r in acts],
                items=[
                    ItemOut(**r)
                    for r in c.execute("SELECT item_id, name, division_id FROM item ORDER BY name").fetchall()
                ],
                first_day=first["d"] if first else None,
                freshness=_freshness(c, wid),
            )
        )
    return out


# ------------------------------------------------------------------------------------------ index
@router.get(
    "/index", response_model=IndexResponse, tags=["index"], summary="Index series (latest vintage, or as of a time)"
)
def get_index(
    c: Conn,
    world: SlugQ,
    server: ScopeQ = "all",
    division: DivisionQ = "all",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    as_of: Annotated[datetime | None, Query(description="ISO time: reproduce what was published then")] = None,
) -> IndexResponse:
    s = _series(c, world, server, division)
    lo, hi = _range(start, end)
    if as_of is None:
        rows = c.execute(
            """SELECT day, value, coverage, status, vintage, revised FROM index_value_current
                            WHERE series_id = %s AND day BETWEEN %s AND %s ORDER BY day""",
            (s.series_id, lo, hi),
        ).fetchall()
    else:
        rows = c.execute(
            """SELECT DISTINCT ON (day) day, value, coverage, status, vintage, vintage > 1 AS revised
                            FROM index_value WHERE series_id = %s AND day BETWEEN %s AND %s AND computed_at <= %s
                            ORDER BY day, vintage DESC""",
            (s.series_id, lo, hi, as_of),
        ).fetchall()
    mv = c.execute("SELECT max(method_version) AS m FROM index_value WHERE series_id = %s", (s.series_id,)).fetchone()
    return IndexResponse(
        series=s,
        method_version=mv["m"] if mv else None,
        as_of=as_of,
        freshness=_freshness(c, world),
        points=[IndexPoint(**r) for r in rows],
    )


@router.get(
    "/index/revisions",
    response_model=RevisionsResponse,
    tags=["index"],
    summary="Every published vintage of one index value (audit trail)",
)
def get_revisions(
    c: Conn, world: SlugQ, day: date, server: ScopeQ = "all", division: DivisionQ = "all"
) -> RevisionsResponse:
    s = _series(c, world, server, division)
    rows = c.execute(
        """SELECT vintage, value, coverage, status, reason, input_hash, computed_at FROM index_value
                        WHERE series_id = %s AND day = %s ORDER BY vintage""",
        (s.series_id, day),
    ).fetchall()
    if not rows:
        raise ApiError(404, "no_value", f"nothing published for {day}")
    return RevisionsResponse(series=s, day=day, revisions=[Revision(**r) for r in rows])


@router.get(
    "/inflation", response_model=InflationResponse, tags=["analytics"], summary="Inflation rate over a rolling window"
)
def get_inflation(
    c: Conn,
    world: SlugQ,
    server: ScopeQ = "all",
    division: DivisionQ = "all",
    window: Annotated[int, Query(description="7, 30, 90 or 365 days")] = 30,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> InflationResponse:
    if window not in (7, 30, 90, 365):
        raise ApiError(400, "bad_window", "window must be one of 7, 30, 90, 365")
    s = _series(c, world, server, division)
    lo, hi = _range(start, end)
    rows = c.execute(
        """SELECT day, rate, annualized FROM inflation_rate WHERE series_id = %s AND window_days = %s
                        AND day BETWEEN %s AND %s ORDER BY day""",
        (s.series_id, window, lo, hi),
    ).fetchall()
    return InflationResponse(series=s, window_days=window, points=[InflationPoint(**r) for r in rows])


@router.get(
    "/inflation/matrix",
    response_model=InflationMatrix,
    tags=["analytics"],
    summary="Latest inflation rate for every server x division",
)
def get_inflation_matrix(c: Conn, world: SlugQ, window: int = 30) -> InflationMatrix:
    if window not in (7, 30, 90, 365):
        raise ApiError(400, "bad_window", "window must be one of 7, 30, 90, 365")
    _world(c, world)
    rows = c.execute(
        """SELECT DISTINCT ON (r.series_id) coalesce(s.server_id, 'all') AS server_id,
                               coalesce(s.division_id, 'all') AS division_id, r.day, r.rate, r.annualized
                        FROM inflation_rate r JOIN index_series s USING (series_id)
                        WHERE s.world_id = %s AND r.window_days = %s ORDER BY r.series_id, r.day DESC""",
        (world, window),
    ).fetchall()
    return InflationMatrix(world_id=world, window_days=window, cells=[InflationCell(**r) for r in rows])


# ------------------------------------------------------------------------------------------ patches + shocks
@router.get("/patches", response_model=PatchPage, tags=["patches"], summary="Patch timeline (keyset-paginated)")
def list_patches(
    c: Conn,
    world: SlugQ,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    major_only: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PatchPage:
    head = _series(c, world, "all", "all")
    lo, hi = _range(start, end)
    after_ts, after_id = datetime(1970, 1, 1, tzinfo=UTC), ""
    if cursor:
        cur = _decode_cursor(cursor, ("t", "p"))
        try:
            after_ts, after_id = datetime.fromisoformat(cur["t"]), str(cur["p"])
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_cursor", "cursor is malformed; restart from the first page") from exc
    rows = c.execute(
        """SELECT p.world_id, p.patch_id, p.released_at, p.version, p.title, p.tags, p.is_major, p.source,
                  left(p.notes, 400) AS notes_excerpt, i.log_change, i.robust_z
           FROM patch_event p
           LEFT JOIN patch_impact i ON i.world_id = p.world_id AND i.patch_id = p.patch_id AND i.series_id = %s
           WHERE p.world_id = %s AND p.released_at >= %s AND p.released_at < %s::date + 1
             AND (p.released_at, p.patch_id) > (%s, %s) AND (NOT %s OR p.is_major)
           ORDER BY p.released_at, p.patch_id LIMIT %s""",
        (head.series_id, world, lo, hi, after_ts, after_id, major_only, limit + 1),
    ).fetchall()
    items = [
        PatchOut(
            **{k: r[k] for k in r if k not in ("log_change", "robust_z")},
            impact=PatchImpact(log_change=r["log_change"], robust_z=r["robust_z"])
            if r["log_change"] is not None
            else None,
        )
        for r in rows[:limit]
    ]
    nxt = (
        _encode_cursor({"t": items[-1].released_at.isoformat(), "p": items[-1].patch_id}) if len(rows) > limit else None
    )
    return PatchPage(items=items, page=Page(next_cursor=nxt, limit=limit))


@router.get(
    "/shocks", response_model=ShocksResponse, tags=["patches"], summary="Detected shocks with patch attribution"
)
def list_shocks(
    c: Conn,
    world: SlugQ,
    server: ScopeQ = "all",
    division: DivisionQ = "all",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> ShocksResponse:
    s = _series(c, world, server, division)
    lo, hi = _range(start, end)
    shocks = c.execute(
        """SELECT day, log_change, robust_z, direction, persistence FROM shock
                          WHERE series_id = %s AND day BETWEEN %s AND %s ORDER BY day""",
        (s.series_id, lo, hi),
    ).fetchall()
    attrib = c.execute(
        """SELECT a.day, a.patch_id, p.title, p.released_at, a.lag_hours, a.relevance, a.rank
                          FROM shock_attribution a JOIN patch_event p USING (world_id, patch_id)
                          WHERE a.series_id = %s AND a.day BETWEEN %s AND %s AND a.rank <= 3
                          ORDER BY a.day, a.rank""",
        (s.series_id, lo, hi),
    ).fetchall()
    by_day: dict[date, list[Attribution]] = {}
    for a in attrib:
        by_day.setdefault(a["day"], []).append(Attribution(**{k: a[k] for k in a if k != "day"}))
    return ShocksResponse(series=s, shocks=[ShockOut(**r, attributions=by_day.get(r["day"], [])) for r in shocks])


def _authorize(c: DictConn, authorization: str | None, scope: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "unauthenticated", "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.removeprefix("Bearer ").strip()
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = c.execute(
        "SELECT key_id, key_hash, scopes FROM api_key WHERE key_hash = %s AND revoked_at IS NULL", (digest,)
    ).fetchone()
    if row is None or not hmac.compare_digest(row["key_hash"], digest):
        raise ApiError(401, "unauthenticated", "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
    if scope not in row["scopes"]:
        raise ApiError(403, "forbidden", f"token lacks scope '{scope}'")
    return str(row["key_id"])


@router.post(
    "/patches",
    response_model=PatchOut,
    status_code=201,
    tags=["patches"],
    summary="Record a patch note (auth: patches:write; Idempotency-Key required)",
    responses={
        200: {"description": "idempotent replay of an earlier identical request"},
        401: {"description": "missing/invalid token"},
        403: {"description": "missing scope"},
        409: {"description": "idempotency key reused with a different body, or patch exists"},
    },
)
def create_patch(
    c: Conn,
    body: PatchIn,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(min_length=8, max_length=128)] = None,
) -> PatchOut:
    key_id = _authorize(c, authorization, "patches:write")
    if not idempotency_key:
        raise ApiError(
            400, "idempotency_key_required", "send an Idempotency-Key header (8-128 chars) so retries are safe"
        )
    request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    c.commit()  # end the implicit read-only transaction of the auth lookup; the write gets its own below
    for _attempt in range(2):
        try:
            with c.transaction():
                c.execute("SET TRANSACTION READ WRITE")
                prior = c.execute(
                    """SELECT request_hash, status_code, response FROM idempotency_key
                                     WHERE key_id = %s AND idempotency_key = %s""",
                    (key_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise ApiError(
                            409, "idempotency_conflict", "this Idempotency-Key was used with a different body"
                        )
                    response.status_code = 200
                    response.headers["Idempotent-Replayed"] = "true"
                    return PatchOut(**prior["response"])
                if c.execute("SELECT 1 FROM world WHERE world_id = %s", (body.world_id,)).fetchone() is None:
                    raise ApiError(404, "unknown_world", f"world '{body.world_id}' does not exist")
                tags = tag_divisions(f"{body.title} {body.notes}")
                created = c.execute(
                    """INSERT INTO patch_event (world_id, patch_id, released_at, version, title, notes, tags, is_major, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'api') ON CONFLICT (world_id, patch_id) DO NOTHING
                       RETURNING world_id, patch_id, released_at, version, title, tags, is_major, source,
                                 left(notes, 400) AS notes_excerpt""",
                    (
                        body.world_id,
                        body.patch_id,
                        body.released_at,
                        body.version,
                        body.title,
                        body.notes,
                        tags,
                        body.is_major,
                    ),
                ).fetchone()
                if created is None:
                    raise ApiError(409, "patch_exists", f"patch '{body.patch_id}' already exists in '{body.world_id}'")
                out = PatchOut(**created, impact=None)
                c.execute(
                    """INSERT INTO idempotency_key (key_id, idempotency_key, request_hash, status_code, response)
                             VALUES (%s, %s, %s, 201, %s)""",
                    (key_id, idempotency_key, request_hash, out.model_dump_json()),
                )
                return out
        except psycopg.errors.UniqueViolation:
            continue  # a concurrent request with the same key won the race: loop once to replay its result
    raise ApiError(409, "idempotency_conflict", "concurrent request with the same Idempotency-Key")


# ------------------------------------------------------------------------------------------ purchasing power
@router.get(
    "/purchasing-power",
    response_model=PurchasingPowerResponse,
    tags=["analytics"],
    summary="What an hour of an activity buys over time (labour-hours)",
)
def purchasing_power(
    c: Conn,
    world: SlugQ,
    server: SlugQ,
    activity: SlugQ,
    items: Annotated[str | None, Query(pattern=r"^\d{1,12}(,\d{1,12}){0,9}$", description="up to 10 item ids")] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> PurchasingPowerResponse:
    head = _series(c, world, server, "all")
    lo, hi = _range(start, end)
    act = c.execute(
        "SELECT activity_id, label FROM activity WHERE activity_id = %s AND world_id = %s", (activity, world)
    ).fetchone()
    if act is None:
        raise ApiError(404, "unknown_activity", f"activity '{activity}' does not exist in '{world}'")
    rates = c.execute(
        "SELECT effective_from, isk_per_hour FROM activity_rate WHERE activity_id = %s ORDER BY 1", (activity,)
    ).fetchall()
    yields = c.execute(
        """SELECT y.item_id, i.name, y.qty_per_hour FROM activity_yield y JOIN item i USING (item_id)
                          WHERE y.activity_id = %s ORDER BY y.item_id""",
        (activity,),
    ).fetchall()
    item_ids = sorted({int(x) for x in items.split(",")} if items else set())
    meta = c.execute("SELECT item_id, name, division_id FROM item WHERE item_id = ANY(%s)", (item_ids,)).fetchall()
    if len(meta) != len(item_ids):
        raise ApiError(404, "unknown_item", "one or more item ids do not exist")
    need = sorted({*item_ids, *(y["item_id"] for y in yields)})
    prices: dict[tuple[date, int], float] = {
        (r["day"], r["item_id"]): r["price"]
        for r in c.execute(
            """SELECT day, item_id, price FROM item_price_current
                              WHERE server_id = %s AND item_id = ANY(%s) AND day BETWEEN %s AND %s AND price IS NOT NULL""",
            (server, need, lo, hi),
        ).fetchall()
    }
    idx = c.execute(
        """SELECT day, value FROM index_value_current WHERE series_id = %s AND day BETWEEN %s AND %s
                       AND value IS NOT NULL ORDER BY day""",
        (head.series_id, lo, hi),
    ).fetchall()
    points = []
    for r in idx:
        d, level = r["day"], r["value"]
        isk = next((float(x["isk_per_hour"]) for x in reversed(rates) if x["effective_from"] <= d), 0.0)
        goods: float | None = 0.0
        for y in yields:
            p = prices.get((d, y["item_id"]))
            goods = None if p is None or goods is None else goods + p * y["qty_per_hour"]
        wage = None if goods is None else isk + goods
        basket = BASKET_VALUE * level / 100.0
        ip = []
        for i in item_ids:
            p = prices.get((d, i))
            ip.append(
                ItemPower(
                    item_id=i,
                    price=p,
                    units_per_hour=wage / p if wage and p else None,
                    hours_per_unit=p / wage if wage and p else None,
                )
            )
        points.append(
            PowerPoint(
                day=d,
                wage=wage,
                wage_isk=isk,
                wage_goods=goods,
                basket_cost=basket,
                hours_per_basket=basket / wage if wage else None,
                real_wage=wage * 100.0 / level if wage else None,
                items=ip,
            )
        )
    return PurchasingPowerResponse(
        world_id=world,
        server_id=server,
        activity=ActivityOut(**act),
        rates=[WageRate(**x) for x in rates],
        yields=[Yield(**y) for y in yields],
        items=[ItemOut(**m) for m in meta],
        points=points,
    )


# ------------------------------------------------------------------------------------------ integrity + flows
@router.get(
    "/manipulation",
    response_model=ManipulationPage,
    tags=["analytics"],
    summary="Manipulation / thin-market events for a server, newest first (keyset-paginated)",
)
def list_manipulation(
    c: Conn,
    world: SlugQ,
    server: SlugQ,
    kind: Annotated[str | None, Query(pattern=r"^(extreme_listing|hampel_reject|thin_market_spike)$")] = None,
    min_severity: Annotated[float, Query(ge=0)] = 0.0,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ManipulationPage:
    _series(c, world, server, "all")
    after = ("9999-12-31", 2**62, "~")
    if cursor:
        cur = _decode_cursor(cursor, ("d", "i", "k"))
        after = (str(cur["d"]), int(cur["i"]), str(cur["k"]))
    rows = c.execute(
        """SELECT m.server_id, m.item_id, i.name AS item_name, m.day, m.kind, m.severity, m.n_obs, m.thin, m.detail
           FROM manipulation_event m JOIN item i USING (item_id)
           WHERE m.server_id = %s AND (%s::text IS NULL OR m.kind = %s) AND m.severity >= %s
             AND (m.day, m.item_id, m.kind) < (%s::date, %s, %s)
           ORDER BY m.day DESC, m.item_id DESC, m.kind DESC LIMIT %s""",
        (server, kind, kind, min_severity, *after, limit + 1),
    ).fetchall()
    items = [ManipulationOut(**r) for r in rows[:limit]]
    nxt = (
        _encode_cursor({"d": items[-1].day.isoformat(), "i": items[-1].item_id, "k": items[-1].kind})
        if len(rows) > limit
        else None
    )
    return ManipulationPage(items=items, page=Page(next_cursor=nxt, limit=limit))


@router.get(
    "/money-flows", response_model=FlowsResponse, tags=["analytics"], summary="Currency sinks and faucets per day"
)
def money_flows(
    c: Conn,
    world: SlugQ,
    server: SlugQ,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> FlowsResponse:
    _series(c, world, server, "all")
    lo, hi = _range(start, end)
    rows = c.execute(
        """SELECT day, kind, amount, method FROM money_flow WHERE server_id = %s AND day BETWEEN %s AND %s
                        ORDER BY day""",
        (server, lo, hi),
    ).fetchall()
    methods: dict[str, str] = {}
    by_day: dict[date, dict[str, float]] = {}
    for r in rows:
        methods[r["kind"]] = r["method"]
        by_day.setdefault(r["day"], {})[r["kind"]] = r["amount"]
    has_faucets = "faucet_bounty" in methods
    pts = []
    for d, k in sorted(by_day.items()):
        sinks = k.get("sink_sales_tax", 0.0) + k.get("sink_broker_fee", 0.0)
        faucets = k.get("faucet_bounty") if has_faucets else None
        pts.append(FlowPoint(day=d, sinks=sinks, faucets=faucets, net=None if faucets is None else faucets - sinks))
    return FlowsResponse(world_id=world, server_id=server, methods=methods, points=pts)
