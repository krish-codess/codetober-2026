"""Read-only HTTP API over the results, plus the cost model. Serves the built UI at /.

There is no user or tenant in this system and nothing an HTTP caller can mutate, so there is
no authentication. Benchmarks are started from the CLI only: a benchmark sharing a process
with a web server measures the web server.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, correlation_id, log, new_correlation_id
from .report import SCHEMA_VERSION, Workload, WorkloadError, pricing, recommend

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
MAX_BODY = 64 * 1024


class ErrorDetail(BaseModel):
    field: str
    problem: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = []
    request_id: str


class ErrorBody(BaseModel):
    error: ErrorInfo


class Check(BaseModel):
    ok: bool
    detail: str


class Health(BaseModel):
    status: str
    checks: dict[str, Check]


class Measurement(BaseModel):
    seq: int
    variant: str
    query: str
    kind: str
    ts: str
    seconds: float | None
    cpu_s: float | None
    bytes_read: int | None
    reads: int | None
    ok: bool | None
    error: str | None


class MeasurementPage(BaseModel):
    items: list[Measurement]
    next_cursor: int | None


class Ranked(BaseModel):
    variant: str
    format: str
    codec: str
    storage: float
    scan: float
    compute: float
    requests: float
    write: float
    total: float
    latency_s: float
    score: float


class Excluded(BaseModel):
    variant: str
    reason: str


class Recommendation(BaseModel):
    workload: dict[str, Any]
    price: dict[str, Any]
    pick: str
    why: list[str]
    ranked: list[Ranked]
    excluded: list[Excluded]
    assumptions: list[str]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list[dict[str, str]] | None = None) -> None:
        self.status, self.code, self.message, self.details = status, code, message, details or []


def _error(status: int, code: str, message: str, details: list[dict[str, str]] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or [],
                                   "request_id": correlation_id.get()}}, status_code=status)


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="The Compression Bake-Off", version="0.1.0", description=__doc__)
    errors: dict[int | str, dict[str, Any]] = {422: {"model": ErrorBody}, 503: {"model": ErrorBody}}
    requests_total: Counter[tuple[str, int]] = Counter()
    seconds_total: dict[str, float] = {}
    cache: dict[str, Any] = {}

    def results_path() -> Path | None:
        """A local run wins over the published results baked into the image."""
        for origin in (s.data_dir / "results", s.published_dir):
            if (origin / "results.json").exists():
                return origin
        return None

    def load() -> dict[str, Any]:
        """Parsed results, re-read only when the file changes on disk."""
        origin = results_path()
        if origin is None:
            raise ApiError(404, "no_results", "No benchmark results yet. Run `bakeoff all`, then reload.")
        path = origin / "results.json"
        stamp = (str(path), path.stat().st_mtime_ns)
        if cache.get("stamp") != stamp:
            raw = path.read_bytes()
            try:
                doc = json.loads(raw)
                if doc.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError(f"schema_version {doc.get('schema_version')!r}, expected {SCHEMA_VERSION}")
            except ValueError as e:
                raise ApiError(503, "results_unreadable", f"{path.name} cannot be used ({e}). "
                               "Re-run `bakeoff report`.") from e
            doc["origin"] = "local" if origin != s.published_dir else "published"
            cache.update(stamp=stamp, doc=doc, dir=origin,
                         etag='"' + hashlib.sha256(raw).hexdigest()[:32] + '"')
        cached: dict[str, Any] = cache["doc"]
        return cached

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        cid = new_correlation_id()
        t0 = time.perf_counter()
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            response: Response = _error(413, "body_too_large", f"Request bodies are limited to {MAX_BODY} bytes.")
        else:
            response = await call_next(request)
        seconds = time.perf_counter() - t0
        fallback = "static" if response.status_code < 400 else "unmatched"
        route: str = getattr(request.scope.get("route"), "path", fallback)
        requests_total[(route, response.status_code)] += 1
        seconds_total[route] = seconds_total.get(route, 0.0) + seconds
        response.headers.update({"X-Request-ID": cid, "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'"})
        if route not in ("/healthz", "/metrics"):
            log("http", method=request.method, path=request.url.path, status=response.status_code,
                ms=round(seconds * 1000, 1))
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, e: ApiError) -> JSONResponse:
        return _error(e.status, e.code, e.message, e.details)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(map(str, err["loc"])), "problem": err["msg"]} for err in e.errors()]
        return _error(422, "validation_failed", "The request did not match the schema. See details.", details)

    @app.exception_handler(StarletteHTTPException)
    async def on_http(_: Request, e: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
        return _error(e.status_code, code, str(e.detail))

    @app.exception_handler(Exception)
    async def on_crash(request: Request, e: Exception) -> JSONResponse:
        log("http.unhandled", logging.ERROR, path=request.url.path, error=repr(e))
        return _error(500, "internal", "Unexpected server error. Quote request_id when reporting it.")

    @app.get("/healthz", response_model=Health, responses={503: {"model": Health}}, tags=["ops"])
    def healthz() -> JSONResponse:
        """Exercises every dependency: results parse, measurements scan, and a real Parquet round trip."""
        checks: dict[str, dict[str, Any]] = {}

        def check(name: str, fn: Callable[[], str]) -> None:
            try:
                checks[name] = {"ok": True, "detail": fn()}
            except ApiError as e:
                checks[name] = {"ok": False, "detail": e.message}
            except Exception as e:
                checks[name] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}

        def engine() -> str:
            with tempfile.TemporaryDirectory() as tmp:
                path = (Path(tmp) / "probe.parquet").as_posix()
                con = duckdb.connect()
                con.sql("SELECT 42 AS answer").write_parquet(path)
                row = con.execute("SELECT answer FROM read_parquet(?)", [path]).fetchone()
                if row != (42,):
                    raise RuntimeError(f"round trip returned {row}")
            return "wrote and read a Parquet file"

        def measurements() -> str:
            load()
            row = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)",
                                           [str(cache["dir"] / "measurements.parquet")]).fetchone()
            return f"{row[0] if row else 0} executions readable"

        check("results", lambda: f"{len(load()['variants'])} variants, generated {load()['generated_at']}")
        check("measurements", measurements)
        check("engine", engine)
        healthy = all(c["ok"] for c in checks.values())
        return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks},
                            status_code=200 if healthy else 503)

    @app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
    def metrics() -> str:
        """Prometheus text format."""
        lines = ["# TYPE bakeoff_http_requests_total counter"]
        lines += [f'bakeoff_http_requests_total{{route="{r}",status="{c}"}} {n}'
                  for (r, c), n in sorted(requests_total.items())]
        lines += ["# TYPE bakeoff_http_request_seconds_total counter"]
        lines += [f'bakeoff_http_request_seconds_total{{route="{r}"}} {v:.6f}'
                  for r, v in sorted(seconds_total.items())]
        try:
            generated = calendar.timegm(time.strptime(load()["generated_at"], "%Y-%m-%dT%H:%M:%SZ"))
            lines += ["# TYPE bakeoff_results_age_seconds gauge",
                      f"bakeoff_results_age_seconds {time.time() - generated:.0f}"]
        except ApiError:
            lines += ["# TYPE bakeoff_results_available gauge", "bakeoff_results_available 0"]
        return "\n".join(lines) + "\n"

    @app.get("/api/results", responses=errors | {404: {"model": ErrorBody}}, tags=["results"])
    def get_results(request: Request) -> Response:
        """The whole published result set: dataset, environment, every variant and query cell,
        pushdown analysis, column lab. Supports `If-None-Match`; clients should revalidate, not refetch."""
        doc = load()
        headers = {"ETag": cache["etag"], "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == cache["etag"]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(doc, headers=headers)

    @app.get("/api/measurements", response_model=MeasurementPage, responses=errors, tags=["results"])
    def get_measurements(
        cursor: int = Query(0, ge=0, description="`next_cursor` from the previous page; 0 starts."),
        limit: int = Query(100, ge=1, le=500),
        variant: str | None = Query(None, max_length=64),
        query: str | None = Query(None, max_length=64),
    ) -> dict[str, Any]:
        """Raw executions, ordered by `seq`. Keyset pagination: stable under concurrent reads."""
        load()
        cur = duckdb.connect().execute(
            "SELECT * FROM read_parquet($path) WHERE seq > $cursor AND ($variant IS NULL OR variant = $variant) "
            "AND ($query IS NULL OR query = $query) ORDER BY seq LIMIT $n",
            {"path": str(cache["dir"] / "measurements.parquet"), "cursor": cursor, "variant": variant,
             "query": query, "n": limit + 1})
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        more = len(rows) > limit
        return {"items": rows[:limit], "next_cursor": rows[limit - 1]["seq"] if more else None}

    @app.get("/api/pricing", tags=["cost"])
    def get_pricing() -> dict[str, Any]:
        """The list prices the cost model uses, with sources and how each was verified."""
        return pricing()

    @app.post("/api/recommend", response_model=Recommendation, responses=errors | {404: {"model": ErrorBody}},
              tags=["cost"])
    def post_recommend(workload: Workload) -> dict[str, Any]:
        """Monthly cost of every variant for a described workload, ranked, with the pick explained.
        Pure function of the request and the results: safe to retry."""
        try:
            return recommend(load(), workload)
        except WorkloadError as e:
            raise ApiError(422, "invalid_workload", str(e), [{"field": "body.mix", "problem": str(e)}]) from e

    @app.get("/api/{rest:path}", include_in_schema=False)  # or unknown API paths would fall through to the UI
    def api_not_found(rest: str) -> None:
        raise HTTPException(404, f"No such endpoint: /api/{rest}")

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
