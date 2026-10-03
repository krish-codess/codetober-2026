"""HTTP API and static viewer. Read endpoints are open; starting a run needs the API key."""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import duckdb
import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from tydlc import store
from tydlc.obs import correlation_id, setup_logging
from tydlc.runner import run_suite
from tydlc.subjects import get_subject

log = logging.getLogger("tydlc.api")

REQUESTS = Counter("tydlc_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("tydlc_http_request_seconds", "HTTP request latency", ["route"])
RUNS = Counter("tydlc_runs_total", "Property runs finished by this process", ["engine", "outcome"])
RUN_SECONDS = Histogram("tydlc_run_seconds", "Property run duration", ["engine"],
                        buckets=(5, 15, 30, 60, 120, 300, 600))

Engine = Literal["duckdb", "postgres"]
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=512)]


# --- contract -------------------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str
    details: list[dict[str, Any]] | None = None


class Error(BaseModel):
    error: ErrorBody


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, str]


class RunRequest(BaseModel):
    engine: Engine = "duckdb"
    seed: int = Field(28, ge=0, le=2**31 - 1)
    max_examples: int = Field(200, ge=1, le=2000)


class Run(BaseModel):
    id: int
    idempotency_key: str
    subject: str
    engine: Engine
    seed: int
    max_examples: int
    status: Literal["running", "completed", "failed"]
    git_sha: str | None
    started_at: datetime
    finished_at: datetime | None
    examples: int | None
    rows_generated: int | None
    duration_ms: int | None
    candidates: int | None
    candidates_held: int | None
    gate_ok: bool | None
    error: str | None


class RunPage(BaseModel):
    items: list[Run]
    next_cursor: str | None


class Invariant(BaseModel):
    name: str
    source: Literal["declared", "discovered"]
    description: str
    passed: int
    failed: int
    vacuous: int
    status: Literal["held", "falsified", "vacuous"]
    confidence: float = Field(description="1 - 3/passed (rule of three); 0 once falsified")
    known_bug: str | None
    failure_id: int | None
    minimal_rows: int | None
    history_runs: int = Field(description="runs on this engine considered, up to 20")
    history_falsified: int


class InvariantPage(BaseModel):
    items: list[Invariant]
    next_cursor: str | None


class FailureSummary(BaseModel):
    id: int
    run_id: int
    property: str
    source: Literal["declared", "discovered"]
    description: str
    minimal_rows: int
    shrunk: bool
    shrink_calls: int
    shrink_ms: int
    known_bug: str | None
    passed: int
    failed: int
    engine: Engine
    seed: int
    max_examples: int
    started_at: datetime


class FailurePage(BaseModel):
    items: list[FailureSummary]
    next_cursor: str | None


class FailureDetail(FailureSummary):
    minimal_dataset: dict[str, list[dict[str, Any]]]


class FailureFrequency(BaseModel):
    name: str
    source: Literal["declared", "discovered"]
    runs: int
    falsified_runs: int
    failed_examples: int
    judged_examples: int
    failure_frequency: float


class HitRate(BaseModel):
    run_id: int
    started_at: datetime
    seed: int
    max_examples: int
    candidates: int
    candidates_held: int
    hit_rate: float | None


ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": Error} for code in (400, 401, 404, 422, 503)}


def _error(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body = ErrorBody(code=code, message=message, correlation_id=correlation_id.get(),
                     details=details)
    return JSONResponse({"error": body.model_dump(exclude_none=True)}, status_code=status)


# --- dependencies ----------------------------------------------------------------------

def _dsn() -> str:
    return os.environ.get("DATABASE_URL", "")


def db() -> Iterator[psycopg.Connection[Any]]:
    # ponytail: one connection per request; add psycopg_pool when connection setup
    # shows up in latency (first ceiling at ~10x load, docs/performance.md).
    conn = store.connect(_dsn(), attempts=1)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[psycopg.Connection[Any], Depends(db)]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("TYDLC_API_KEY", "")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "a valid X-API-Key header is required")


def execute_run(run: dict[str, Any]) -> None:
    """Background task: run the suite and record it. Any failure lands on the run row."""
    token = correlation_id.set(run["idempotency_key"])
    started = time.perf_counter()
    conn = store.connect(_dsn())
    try:
        report = run_suite(
            get_subject(run["subject"]), run["engine"], seed=run["seed"],
            max_examples=run["max_examples"], run_key=run["idempotency_key"], dsn=_dsn(),
            data_dir=Path(os.environ.get("TYDLC_DATA_DIR", "data")),
            workers=int(os.environ.get("TYDLC_WORKERS", "0")) or None)
        store.finish_run(conn, run["id"], report)
        RUNS.labels(run["engine"], "completed").inc()
    except Exception as exc:
        log.exception("run failed", extra={"run_id": run["id"]})
        store.fail_run(conn, run["id"], f"{type(exc).__name__}: {exc}")
        RUNS.labels(run["engine"], "failed").inc()
    finally:
        RUN_SECONDS.labels(run["engine"]).observe(time.perf_counter() - started)
        conn.close()
        correlation_id.reset(token)


# --- app -------------------------------------------------------------------------------

def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(title="tydlc", version="0.1.0",
                  description="Property-based testing for data pipelines: runs, invariant "
                              "catalog, minimal failing datasets.")

    @app.middleware("http")
    async def observe(request: Request,
                      call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        cid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        token = correlation_id.set(cid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed = time.perf_counter() - started
            correlation_id.reset(token)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        REQUESTS.labels(request.method, route, response.status_code).inc()
        LATENCY.labels(route).observe(elapsed)
        response.headers["x-request-id"] = cid
        correlation_id.set(cid)
        log.info("request", extra={"method": request.method, "path": request.url.path,
                                   "status": response.status_code,
                                   "ms": round(elapsed * 1000, 1)})
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {400: "bad_request", 401: "unauthorized", 404: "not_found"}
        return _error(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]}
                   for e in exc.errors()]
        return _error(422, "validation_error", "the request did not match the schema", details)

    @app.exception_handler(store.BadCursor)
    async def bad_cursor(_: Request, exc: store.BadCursor) -> JSONResponse:
        return _error(400, "bad_cursor", "cursor is not valid; restart from the first page")

    @app.exception_handler(psycopg.OperationalError)
    async def db_down(_: Request, exc: psycopg.OperationalError) -> JSONResponse:
        log.error("database unavailable", extra={"error": str(exc).strip()})
        return _error(503, "database_unavailable",
                      "the results database is unreachable; retry shortly")

    @app.exception_handler(Exception)
    async def crash(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _error(500, "internal_error", "unexpected error; quote the correlation id")

    @app.get("/healthz", response_model=Health, responses={503: {"model": Health}}, tags=["ops"])
    def health() -> JSONResponse:
        """Exercises both dependencies: a query against PostgreSQL's schema and a DuckDB scan."""
        checks = {}
        try:
            conn = store.connect(_dsn(), attempts=1)
            try:
                conn.execute("SELECT count(*) FROM runs").fetchone()
            finally:
                conn.close()
            checks["postgres"] = "ok"
        except psycopg.Error as exc:
            checks["postgres"] = f"failed: {type(exc).__name__}"
        try:
            duckdb.connect().execute("SELECT sum(i) FROM range(10) t(i)").fetchone()
            checks["duckdb"] = "ok"
        except duckdb.Error as exc:
            checks["duckdb"] = f"failed: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                            status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/runs", response_model=RunPage, responses=ERRORS, tags=["runs"])
    def list_runs(conn: Db, cursor: Cursor = None, limit: Limit = 20,
                  engine: Engine | None = None) -> Any:
        """Runs, newest first."""
        return store.list_runs(conn, cursor, limit, engine)

    @app.post("/api/runs", response_model=Run, status_code=202, tags=["runs"],
              responses={**ERRORS, 200: {"model": Run, "description": "replayed request"}},
              dependencies=[Depends(require_api_key)])
    def start_run(body: RunRequest, tasks: BackgroundTasks, conn: Db,
                  idempotency_key: Annotated[str, Header(min_length=8, max_length=128)]) -> Any:
        """Start a run. Safe to retry: the same Idempotency-Key returns the same run."""
        run, created = store.create_run(conn, idempotency_key, "jaffle", body.engine, body.seed,
                                        body.max_examples, os.environ.get("GIT_SHA"))
        if created:
            tasks.add_task(execute_run, run)
        payload = Run(**run).model_dump(mode="json")
        return JSONResponse(payload, status_code=202 if created else 200)

    @app.get("/api/runs/{run_id}", response_model=Run, responses=ERRORS, tags=["runs"])
    def get_run(run_id: int, conn: Db) -> Any:
        if (run := store.get_run(conn, run_id)) is None:
            raise HTTPException(404, f"run {run_id} does not exist")
        return run

    @app.get("/api/runs/{run_id}/invariants", response_model=InvariantPage, responses=ERRORS,
             tags=["invariants"])
    def invariants(run_id: int, conn: Db, cursor: Cursor = None, limit: Limit = 100) -> Any:
        """The invariant catalog for one run, ordered by name."""
        if store.get_run(conn, run_id) is None:
            raise HTTPException(404, f"run {run_id} does not exist")
        return store.catalog(conn, run_id, cursor, limit)

    @app.get("/api/failures", response_model=FailurePage, responses=ERRORS, tags=["failures"])
    def list_failures(conn: Db, cursor: Cursor = None, limit: Limit = 50,
                      run_id: int | None = None,
                      property: Annotated[str | None, Query(max_length=200)] = None) -> Any:
        """Falsified properties, newest first, without the dataset payload."""
        return store.list_failures(conn, cursor, limit, run_id, property)

    @app.get("/api/failures/{failure_id}", response_model=FailureDetail, responses=ERRORS,
             tags=["failures"])
    def get_failure(failure_id: int, conn: Db) -> Any:
        """One failure with its minimal reproducing dataset."""
        if (failure := store.get_failure(conn, failure_id)) is None:
            raise HTTPException(404, f"failure {failure_id} does not exist")
        return failure

    @app.get("/api/analytics/failure-frequency", response_model=list[FailureFrequency],
             responses=ERRORS, tags=["analytics"])
    def failure_frequency(conn: Db, engine: Engine = "duckdb",
                          runs: Annotated[int, Query(ge=1, le=100)] = 20) -> Any:
        """Share of generated examples violating each property over the last `runs` runs."""
        return store.failure_frequency(conn, engine, runs)

    @app.get("/api/analytics/discovery-hit-rate", response_model=list[HitRate],
             responses=ERRORS, tags=["analytics"])
    def discovery_hit_rate(conn: Db, engine: Engine = "duckdb",
                           runs: Annotated[int, Query(ge=1, le=100)] = 20) -> Any:
        """Per run: the share of seed-inferred candidates that survived adversarial data."""
        return store.discovery_hit_rate(conn, engine, runs)

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), "static")
    return app
