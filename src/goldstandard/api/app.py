"""FastAPI service (role gs_api: read published tables; insert patch notes + idempotency records only).

Cross-cutting behaviour:
  * correlation id: X-Request-ID is accepted (from nginx) or generated, logged on every line and
    returned on every response, including errors
  * every error is {"error": {code, message, details, request_id}} - never a stack trace
  * GET responses carry an ETag; If-None-Match returns 304 (clients cache deliberately)
  * the database is optional at startup: if it is down, reads return 503 database_unavailable and
    /health/ready says so, instead of the process crash-looping
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from psycopg_pool import ConnectionPool, PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from goldstandard.api import routes
from goldstandard.api.errors import ApiError
from goldstandard.api.models import ErrorResponse
from goldstandard.config import settings
from goldstandard.obs import configure_logging, correlation_id, log, metrics, new_correlation_id

logger = logging.getLogger("goldstandard.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = settings()
    configure_logging(cfg.log_level, cfg.log_json)
    pool = ConnectionPool(
        cfg.api_database_url.get_secret_value(),
        min_size=1,
        max_size=10,
        timeout=3.0,
        open=False,
        kwargs={
            "options": "-c statement_timeout=5000 -c default_transaction_read_only=on",
            "connect_timeout": 3,
            "application_name": "goldstandard-api",
        },
    )
    pool.open(wait=False)  # do not block startup on the database; requests report 503 until it is up
    app.state.pool = pool
    log(logger, logging.INFO, "api started")
    yield
    pool.close()


app = FastAPI(
    title="GOLD STANDARD API",
    version="1.0.0",
    description="Consumer price index for video game economies: chain-linked Laspeyres indices per server, "
    "robust prices, patch shock attribution and purchasing power in labour-hours. "
    "All values are published append-only: a revised value is a new vintage, never an overwrite.",
    lifespan=lifespan,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in settings().cors_origins.split(",") if o],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-None-Match", "X-Request-ID"],
    expose_headers=["ETag", "X-Request-ID", "Idempotent-Replayed"],
)


@app.middleware("http")
async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    rid = request.headers.get("x-request-id", "")
    rid = rid if rid.isalnum() and len(rid) <= 64 else new_correlation_id()
    correlation_id.set(rid)
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    metrics.inc("http_requests", route=path, method=request.method, status=str(response.status_code))
    metrics.observe("http_request", elapsed, route=path)
    response.headers["X-Request-ID"] = rid
    log(
        logger,
        logging.INFO,
        "request",
        method=request.method,
        path=request.url.path,
        route=path,
        status=response.status_code,
        ms=round(elapsed * 1000, 1),
    )
    return response


def _error(status: int, code: str, message: str, details: list[dict[str, Any]] | None = None) -> JSONResponse:
    metrics.inc("api_errors", code=code)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details, "request_id": correlation_id.get()}},
    )


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError) -> JSONResponse:
    resp = _error(exc.status, exc.code, exc.message, exc.details)
    for k, v in exc.headers.items():
        resp.headers[k] = v
    return resp


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return _error(422, "validation_error", "request failed validation", details)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(PoolTimeout)
@app.exception_handler(psycopg.OperationalError)
async def db_down(_: Request, exc: Exception) -> JSONResponse:
    log(logger, logging.ERROR, "database unavailable", error=type(exc).__name__)
    return _error(503, "database_unavailable", "the database is unavailable; retry shortly")


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", extra={"fields": {"error": type(exc).__name__}})
    return _error(500, "internal_error", "unexpected error; quote the request_id when reporting it")


@app.middleware("http")
async def etag(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    if request.method != "GET" or response.status_code != 200 or not request.url.path.startswith("/v1/"):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
    tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    headers["ETag"] = tag
    headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, status_code=200, headers=headers, media_type=response.media_type)


@app.get("/health", tags=["ops"], summary="Liveness: the process is up")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/health/ready",
    tags=["ops"],
    summary="Readiness: exercises PostgreSQL and checks data freshness",
    responses={503: {"description": "database unreachable"}},
)
def ready(request: Request) -> JSONResponse:
    result = routes.readiness(request.app.state.pool)
    return JSONResponse(
        status_code=503 if result.status == "unavailable" else 200, content=json.loads(result.model_dump_json())
    )


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse, summary="Prometheus metrics")
def prom() -> str:
    return metrics.render_prometheus()


app.include_router(routes.router)
