"""The serving API: one user's story, view and share recording, public share cards, and admin analytics.

Authorization is by signed token only. No route takes a user id: `/v1/wrapped` answers for the
user the token names, so there is no id to tamper with. Public share routes read the `shares`
table, which holds a snapshot of exactly one card, so they cannot return anything else.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from wrapped import auth, config
from wrapped.cards import CARD_TYPES
from wrapped.config import Settings, correlation_id, log, warn
from wrapped.render import render_card

logger = logging.getLogger("wrapped.api")
CARD_TYPE_PATTERN = r"^[a-z_]{1,40}$"
SHARE_ID_PATTERN = r"^[A-Za-z0-9_-]{22,64}$"


# ---- contract -------------------------------------------------------------------------------------------------


class Claim(BaseModel):
    metric: str
    top_permille: int = Field(description="The claim in tenths of a percent: 50 means 'top 5%'.")
    text: str
    basis: str = Field(description="Who the user is being compared with, in words.")


class Stat(BaseModel):
    label: str
    value: str


class Card(BaseModel):
    type: str
    family: str
    shareable: bool
    headline: str
    value: str
    unit: str
    body: str
    claim: Claim | None = None
    stats: list[Stat] | None = None


class UserRef(BaseModel):
    id: int
    login: str


class Population(BaseModel):
    users: int
    basis: str


class WrappedResponse(BaseModel):
    version: int
    year: int
    user: UserRef
    tier: str = Field(description="full, light or minimal: how much activity the story is built from.")
    archetype: str
    population: Population
    cards: list[Card]
    run_id: uuid.UUID = Field(description="The generation run this story came from.")
    generated_at: str


class ShareRequest(BaseModel):
    card_type: str = Field(pattern=CARD_TYPE_PATTERN)


class ShareResponse(BaseModel):
    share_id: str
    card_type: str
    url: str = Field(description="Public page with link-preview tags.")
    image_url: str = Field(description="The rendered card. Versioned, so it can be cached forever.")
    created: bool = Field(description="False when this card had already been shared and the same share was returned.")


class PublicShare(BaseModel):
    share_id: str
    login: str
    year: int
    card: Card
    image_url: str


class SuperlativeRow(BaseModel):
    card_type: str
    family: str
    users: int
    share_of_users: float


class SuperlativeDistribution(BaseModel):
    run_id: uuid.UUID
    users: int
    cards: list[SuperlativeRow]


class ShareRateRow(BaseModel):
    card_type: str
    viewers: int
    sharers: int
    share_rate: float | None = Field(description="sharers / viewers; null when nobody has viewed the card yet.")


class PayloadSummary(BaseModel):
    user_id: int
    login: str
    tier: str


class PayloadPage(BaseModel):
    items: list[PayloadSummary]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page.")


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[dict[str, Any]] | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _error(status: int, code: str, message: str, details: list[dict[str, Any]] | None = None) -> JSONResponse:
    body = ErrorBody(error=ErrorDetail(code=code, message=message, request_id=correlation_id.get(), details=details))
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status)


def _errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {s: {"model": ErrorBody} for s in statuses}


# ---- app ------------------------------------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or config.load()
    config.setup_logging(settings.log_level)
    pool: ConnectionPool[Any] = ConnectionPool(
        settings.api_database_url,
        min_size=1,
        max_size=8,
        timeout=2,  # a request waits this long for a connection, then gets a 503 it can retry
        open=False,
        kwargs={"connect_timeout": 3, "row_factory": dict_row},
    )

    registry = CollectorRegistry()
    requests = Counter("wrapped_http_requests_total", "HTTP requests", ["route", "method", "status"], registry=registry)
    latency = Histogram("wrapped_http_request_seconds", "HTTP request latency", ["route"], registry=registry)
    shares_created = Counter("wrapped_shares_created_total", "New shares", ["card_type"], registry=registry)
    render_seconds = Histogram("wrapped_card_render_seconds", "Share card render time", registry=registry)
    db_unavailable = Counter(
        "wrapped_db_unavailable_total", "Requests failed because the database was unreachable", registry=registry
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open(wait=False)  # start even if the database is not up yet; /readyz says so, data routes return 503
        settings.card_dir.mkdir(parents=True, exist_ok=True)
        yield
        pool.close()

    app = FastAPI(
        title="Your App, Wrapped",
        version="1.0.0",
        description=__doc__,
        lifespan=lifespan,
    )

    @contextmanager
    def db() -> Iterator[psycopg.Connection[Any]]:
        try:
            with pool.connection() as conn:
                yield conn
        except (PoolTimeout, psycopg.OperationalError) as exc:
            db_unavailable.inc()
            warn(logger, "database unavailable", error=str(exc)[:200])
            raise ApiError(
                503,
                "database_unavailable",
                "The service is temporarily unable to reach its database. Try again shortly.",
                5,
            ) from exc

    # ---- cross-cutting ------------------------------------------------------------------------------------

    @app.middleware("http")
    async def observe(request: Request, call_next: Any) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied) else uuid.uuid4().hex
        correlation_id.set(request_id)
        started = time.perf_counter()
        response: Response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.perf_counter() - started
        requests.labels(route, request.method, response.status_code).inc()
        latency.labels(route).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        if route not in ("/healthz", "/metrics"):
            log(
                logger,
                "request",
                method=request.method,
                route=route,
                status=response.status_code,
                ms=round(elapsed * 1000, 1),
            )
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        response = _error(exc.status, exc.code, exc.message)
        if exc.retry_after:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()]
        return _error(422, "invalid_request", "The request did not match the contract. See details.", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=exc)
        return _error(
            500, "internal_error", "Something went wrong on our side. The request id identifies it in our logs."
        )

    def current_user(authorization: Annotated[str | None, Header()] = None) -> auth.Claims:
        scheme, _, token = (authorization or "").partition(" ")
        claims = auth.verify(settings.token_secret, token.strip()) if scheme.lower() == "bearer" else None
        if claims is None or claims.year != settings.year:
            raise ApiError(401, "invalid_token", "A valid personal link is required. It may have expired.")
        return claims

    def admin(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(401, "invalid_token", "An admin token is required.")
        if not auth.is_admin(settings.admin_token, token.strip()):
            raise ApiError(403, "forbidden", "This token does not grant admin access.")

    def image_url(share_id: str, run_id: uuid.UUID) -> str:
        return f"{settings.public_base_url}/v1/shares/{share_id}/card.png?v={run_id.hex[:8]}"

    def load_story(conn: psycopg.Connection[Any], user_id: int) -> dict[str, Any]:
        row = conn.execute(
            """SELECT p.payload, p.login, a.run_id, r.finished_at
               FROM active_runs a
               JOIN generation_runs r ON r.run_id = a.run_id
               JOIN wrapped_payloads p ON p.run_id = a.run_id
               WHERE a.year = %s AND p.user_id = %s""",
            [settings.year, user_id],
        ).fetchone()
        if row is None:
            raise ApiError(404, "wrapped_not_found", f"There is no {settings.year} story for this account.")
        return row

    # ---- operations ---------------------------------------------------------------------------------------

    @app.get("/healthz", tags=["operations"], summary="Liveness: the process is up")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["operations"], summary="Readiness: every dependency is exercised", responses=_errors(503))
    def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            with pool.connection(timeout=2) as conn:
                row = conn.execute("SELECT run_id FROM active_runs WHERE year = %s", [settings.year]).fetchone()
            checks["database"] = "ok"
            checks["active_run"] = str(row["run_id"]) if row else "missing: no run has been published"
        except Exception as exc:  # noqa: BLE001 - a health check reports, it does not raise
            checks["database"] = f"failed: {type(exc).__name__}"
        try:
            probe = settings.card_dir / f".probe-{os.getpid()}"
            probe.write_bytes(
                render_card({"headline": "probe", "value": "1", "family": "frame"}, "probe", settings.year)[:64]
            )
            probe.unlink()
            checks["card_store"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["card_store"] = f"failed: {type(exc).__name__}"
        ready = (
            checks.get("database") == "ok"
            and checks["card_store"] == "ok"
            and not checks["active_run"].startswith("missing")
        )
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503
        )

    @app.get("/metrics", tags=["operations"], summary="Prometheus metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    # ---- the user's story ---------------------------------------------------------------------------------

    @app.get("/v1/wrapped", tags=["story"], response_model=WrappedResponse, responses={304: {"description": "Not modified"}, **_errors(401, 404, 503)},
             summary="The story for the user the token names")  # fmt: skip
    def get_wrapped(
        response: Response,
        user: auth.Claims = Depends(current_user),
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Any:
        with db() as conn:
            row = load_story(conn, user.user_id)
        etag = f'"{row["run_id"].hex}-{user.user_id}"'
        # no-cache = "store it, but ask me before reusing it". With a max-age the browser would replay this
        # response to whoever opens the next personal link on the same device, whatever their token.
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        response.headers.update(headers)
        return row["payload"] | {"run_id": row["run_id"], "generated_at": row["finished_at"].isoformat()}

    @app.put("/v1/wrapped/views/{card_type}", tags=["story"], status_code=204, responses=_errors(401, 404, 422, 503),
             summary="Record that the user saw a card. Idempotent.")  # fmt: skip
    def record_view(
        card_type: Annotated[str, PathParam(pattern=CARD_TYPE_PATTERN)], user: auth.Claims = Depends(current_user)
    ) -> Response:
        with db() as conn:
            in_story = conn.execute(
                """SELECT 1 FROM active_runs a JOIN payload_cards c ON c.run_id = a.run_id
                   WHERE a.year = %s AND c.user_id = %s AND c.card_type = %s""",
                [settings.year, user.user_id, card_type],
            ).fetchone()
            if in_story is None:
                raise ApiError(404, "card_not_in_story", "That card is not part of this account's story.")
            conn.execute(
                "INSERT INTO card_views (user_id, year, card_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [user.user_id, settings.year, card_type],
            )
        return Response(status_code=204)

    @app.post("/v1/wrapped/shares", tags=["story"], response_model=ShareResponse,
              responses={200: {"description": "This card was already shared; the existing share"}, 201: {"model": ShareResponse, "description": "Share created"}, **_errors(401, 404, 409, 422, 503)},
              summary="Share one card. Retrying returns the same share.")  # fmt: skip
    def create_share(body: ShareRequest, response: Response, user: auth.Claims = Depends(current_user)) -> Any:
        with db() as conn:
            story = load_story(conn, user.user_id)
            card = next((c for c in story["payload"]["cards"] if c["type"] == body.card_type), None)
            if card is None:
                raise ApiError(404, "card_not_in_story", "That card is not part of this account's story.")
            if not card["shareable"]:
                raise ApiError(409, "card_not_shareable", "That card cannot be shared.")
            row = conn.execute(
                """INSERT INTO shares (share_id, user_id, year, card_type, login, card, source_run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, year, card_type) DO UPDATE
                       SET card = EXCLUDED.card, login = EXCLUDED.login, source_run_id = EXCLUDED.source_run_id,
                           updated_at = now()
                   RETURNING share_id, (xmax = 0) AS created""",
                [secrets.token_urlsafe(16), user.user_id, settings.year, body.card_type, story["login"],
                 Jsonb(card), story["run_id"]],
            ).fetchone()  # fmt: skip
            assert row is not None
            # Sharing implies having seen the card, so sharers are always a subset of viewers.
            conn.execute(
                "INSERT INTO card_views (user_id, year, card_type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [user.user_id, settings.year, body.card_type],
            )
        if row["created"]:
            shares_created.labels(body.card_type).inc()
        try:  # render now so the image exists before a link preview asks for it; GET renders again if this fails
            card_file(row["share_id"], story["run_id"], card, story["login"])
        except Exception as exc:  # noqa: BLE001
            warn(logger, "card render failed at share time", share_id=row["share_id"], error=str(exc)[:200])
        response.status_code = 201 if row["created"] else 200
        return ShareResponse(
            share_id=row["share_id"], card_type=body.card_type, url=f"{settings.public_base_url}/s/{row['share_id']}",
            image_url=image_url(row["share_id"], story["run_id"]), created=row["created"],
        )  # fmt: skip

    # ---- public share surface -----------------------------------------------------------------------------

    def load_share(share_id: str) -> dict[str, Any]:
        if not re.fullmatch(SHARE_ID_PATTERN, share_id):
            raise ApiError(404, "share_not_found", "No such share.")
        with db() as conn:
            row = conn.execute(
                "SELECT share_id, login, year, card, source_run_id FROM shares WHERE share_id = %s", [share_id]
            ).fetchone()
        if row is None:
            raise ApiError(404, "share_not_found", "No such share.")
        return row

    def card_file(share_id: str, run_id: uuid.UUID, card: dict[str, Any], login: str) -> Any:
        """Rendered card on disk, created on first need. Written to a temp name and renamed, so a reader never sees half."""
        path = settings.card_dir / f"{share_id}-{run_id.hex[:8]}.png"
        if not path.exists():
            with render_seconds.time():
                png = render_card(card, login, settings.year)
            tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_bytes(png)
            tmp.replace(path)
        return path

    @app.get("/v1/shares/{share_id}", tags=["shares"], response_model=PublicShare, responses=_errors(404, 503),
             summary="A shared card. Public; returns that one card and nothing else about the user.")  # fmt: skip
    def get_share(share_id: str) -> Any:
        row = load_share(share_id)
        return PublicShare(share_id=row["share_id"], login=row["login"], year=row["year"], card=row["card"],
                           image_url=image_url(row["share_id"], row["source_run_id"]))  # fmt: skip

    @app.get("/v1/shares/{share_id}/card.png", tags=["shares"], responses={200: {"content": {"image/png": {}}}, **_errors(404, 503)},
             summary="The shared card as a 1200x630 PNG", response_class=FileResponse)  # fmt: skip
    def get_share_image(share_id: str) -> Any:
        row = load_share(share_id)
        try:
            path = card_file(row["share_id"], row["source_run_id"], row["card"], row["login"])
        except OSError as exc:
            warn(logger, "card store unavailable", error=str(exc)[:200])
            raise ApiError(
                503, "card_unavailable", "The card image could not be produced right now. Try again shortly.", 5
            ) from exc
        # The URL carries the run version, so the bytes behind it never change: cache at the edge forever.
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @app.get("/s/{share_id}", tags=["shares"], response_class=HTMLResponse, responses=_errors(404, 503),
             summary="Share landing page with link-preview tags")  # fmt: skip
    def share_page(share_id: str) -> HTMLResponse:
        row = load_share(share_id)
        card, e = row["card"], html.escape
        title = e(f"{row['login']}'s {row['year']}: {card['headline']}")
        desc = e(" ".join(str(x) for x in (card["value"], card["unit"], "-", card["body"]) if x))
        image = e(image_url(row["share_id"], row["source_run_id"]))
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><meta name="description" content="{desc}">
<meta property="og:type" content="website"><meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}"><meta property="og:image" content="{image}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{image}">
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0b1e;color:#fff;
font-family:system-ui,sans-serif}}main{{width:min(92vw,900px);text-align:center}}img{{width:100%;height:auto;border-radius:16px}}
a{{color:#c8b6ff}}</style></head>
<body><main><img src="{image}" width="1200" height="630" alt="{title}. {desc}">
<p><a href="{e(settings.public_base_url)}/">Your App, Wrapped</a></p></main></body></html>"""
        return HTMLResponse(
            page, headers={"Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"}
        )

    # ---- admin analytics ----------------------------------------------------------------------------------

    @app.get("/v1/admin/analytics/superlatives", tags=["admin"], response_model=SuperlativeDistribution,
             dependencies=[Depends(admin)], responses=_errors(401, 403, 404, 503),
             summary="How the cards are distributed across users in the active run")  # fmt: skip
    def superlatives() -> Any:
        with db() as conn:
            run = conn.execute(
                "SELECT r.run_id, r.user_count FROM active_runs a JOIN generation_runs r USING (run_id) WHERE a.year = %s",
                [settings.year],
            ).fetchone()
            if run is None:
                raise ApiError(404, "no_active_run", "No run has been published yet.")
            rows = conn.execute(
                """SELECT c.card_type, t.family, count(*) AS users
                   FROM payload_cards c JOIN card_types t USING (card_type)
                   WHERE c.run_id = %s GROUP BY c.card_type, t.family ORDER BY users DESC, c.card_type""",
                [run["run_id"]],
            ).fetchall()
        total = max(run["user_count"], 1)
        return {"run_id": run["run_id"], "users": run["user_count"],
                "cards": [r | {"share_of_users": round(r["users"] / total, 4)} for r in rows]}  # fmt: skip

    @app.get("/v1/admin/analytics/share-rate", tags=["admin"], response_model=list[ShareRateRow], dependencies=[Depends(admin)],
             responses=_errors(401, 403, 503), summary="Of the users who saw each shareable card, how many shared it")  # fmt: skip
    def share_rate() -> Any:
        with db() as conn:
            rows = conn.execute(
                """SELECT t.card_type, coalesce(v.viewers, 0) AS viewers, coalesce(s.sharers, 0) AS sharers
                   FROM card_types t
                   LEFT JOIN (SELECT card_type, count(*) AS viewers FROM card_views WHERE year = %(y)s GROUP BY card_type) v
                       USING (card_type)
                   LEFT JOIN (SELECT card_type, count(*) AS sharers FROM shares WHERE year = %(y)s GROUP BY card_type) s
                       USING (card_type)
                   WHERE t.shareable
                   ORDER BY sharers DESC, viewers DESC, t.card_type""",
                {"y": settings.year},
            ).fetchall()
        return [r | {"share_rate": round(r["sharers"] / r["viewers"], 4) if r["viewers"] else None} for r in rows]

    @app.get("/v1/admin/payloads", tags=["admin"], response_model=PayloadPage, dependencies=[Depends(admin)],
             responses=_errors(401, 403, 422, 503), summary="Page through the active run's payloads, ordered by user id")  # fmt: skip
    def list_payloads(
        limit: Annotated[int, Query(ge=1, le=200)] = 50, cursor: Annotated[str | None, Query(max_length=64)] = None
    ) -> Any:
        try:
            after = int(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else 0
        except ValueError as exc:
            raise ApiError(422, "invalid_cursor", "The cursor is not one this API issued.") from exc
        with db() as conn:
            rows = conn.execute(
                """SELECT p.user_id, p.login, p.tier
                   FROM active_runs a JOIN wrapped_payloads p ON p.run_id = a.run_id
                   WHERE a.year = %s AND p.user_id > %s ORDER BY p.user_id LIMIT %s""",
                [settings.year, after, limit + 1],
            ).fetchall()
        page = rows[:limit]
        more = len(rows) > limit
        return {"items": page,
                "next_cursor": base64.urlsafe_b64encode(str(page[-1]["user_id"]).encode()).decode() if more else None}  # fmt: skip

    def openapi() -> dict[str, Any]:
        """The generated document, with validation failures described in the shape this API actually returns."""
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            for operations in schema["paths"].values():
                for operation in operations.values():
                    if "422" in operation["responses"]:
                        operation["responses"]["422"] = {
                            "description": "The request did not match the contract",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}},
                        }
            for unused in ("HTTPValidationError", "ValidationError"):
                schema["components"]["schemas"].pop(unused, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    assert all(re.fullmatch(CARD_TYPE_PATTERN, name) for name in CARD_TYPES)
    return app
