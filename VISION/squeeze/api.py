"""squeeze API: the measured accuracy / latency / power tradeoff of every compressed model on
every hardware target, the deployment packages, and the endpoint devices upload results to.

Reads are public. Uploading needs the device token; the quarantine needs the admin token.
Every error has the same shape: `{"error": {"code", "message", "details"}, "request_id"}`.
"""

# No `from __future__ import annotations` here: FastAPI resolves the dependency aliases defined
# inside create_app from real annotation objects, not from strings.
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, log, schemas

logger = logging.getLogger("squeeze.api")
BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 1000)
ERRORS: dict[int | str, dict[str, Any]] = {code: {"model": schemas.Error} for code in (400, 401, 404, 409, 413, 422)}


class Metrics:
    """Counters and a latency histogram per route, in Prometheus text format. In-process: with
    more than one worker each keeps its own (see the limitations in the README)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[tuple[str, str, int], int] = {}
        self.buckets: dict[tuple[str, float], int] = {}
        self.sums: dict[str, float] = {}
        self.ingest: dict[str, int] = {}

    def observe(self, method: str, route: str, status: int, ms: float) -> None:
        with self.lock:
            key = (method, route, status)
            self.requests[key] = self.requests.get(key, 0) + 1
            self.sums[route] = self.sums.get(route, 0.0) + ms
            for le in (*BUCKETS_MS, float("inf")):
                if ms <= le:
                    self.buckets[(route, le)] = self.buckets.get((route, le), 0) + 1

    def render(self) -> str:
        with self.lock:
            lines = ["# TYPE squeeze_http_requests_total counter"]
            for (method, route, status), n in sorted(self.requests.items()):
                lines.append(f'squeeze_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {n}')
            lines.append("# TYPE squeeze_http_request_duration_ms histogram")
            for (route, le), n in sorted(self.buckets.items()):
                bound = "+Inf" if le == float("inf") else str(le)
                lines.append(f'squeeze_http_request_duration_ms_bucket{{route="{route}",le="{bound}"}} {n}')
            for route, total in sorted(self.sums.items()):
                lines.append(f'squeeze_http_request_duration_ms_sum{{route="{route}"}} {total:.3f}')
            lines.append("# TYPE squeeze_ingest_results_total counter")
            for outcome, n in sorted(self.ingest.items()):
                lines.append(f'squeeze_ingest_results_total{{outcome="{outcome}"}} {n}')
        return "\n".join(lines) + "\n"


def problem(status: int, code: str, message: str, details: list[Any] | None = None) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, "details": details or []})


def create_app(settings: config.Settings | None = None) -> FastAPI:
    cfg = settings or config.load()
    targets = config.targets()
    metrics = Metrics()
    append_lock = threading.Lock()
    app = FastAPI(title="squeeze", version="0.1.0", description=__doc__)
    app.state.cfg, app.state.metrics = cfg, metrics

    boot = db.connect(cfg.db_path)
    db.migrate(boot)
    boot.close()

    def con() -> Iterator[sqlite3.Connection]:
        connection = db.connect(cfg.db_path)
        try:
            yield connection
        finally:
            connection.close()

    Con = Annotated[sqlite3.Connection, Depends(con)]

    def require(expected: str, role: str) -> Callable[[Request], None]:
        def check(request: Request) -> None:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            # An unset token never matches: a server without configured secrets accepts nobody.
            if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
                raise problem(
                    401,
                    "unauthorized",
                    f"this endpoint needs the {role} token as 'Authorization: Bearer ...'",
                )

        return check

    # --- cross-cutting --------------------------------------------------------------------------

    def error_response(status: int, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"error": body, "request_id": log.corr_id.get()}, status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 304:
            return Response(status_code=304, headers=exc.headers)
        detail: Any = exc.detail
        if not isinstance(detail, dict):
            code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
            detail = {"code": code, "message": str(detail), "details": []}
        return error_response(exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return error_response(
            422, {"code": "invalid_request", "message": "the request is not valid", "details": details}
        )

    @app.middleware("http")
    async def correlate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        cid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        token = log.corr_id.set(cid)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:  # last resort: log the stack, return a shaped 500 without it
                logger.exception("unhandled error")
                response = error_response(
                    500,
                    {"code": "internal", "message": "unexpected error; quote the request_id", "details": []},
                )
            ms = (time.perf_counter() - start) * 1000
            route = getattr(request.scope.get("route"), "path", "unmatched")
            metrics.observe(request.method, route, response.status_code, ms)
            log.event(
                logger,
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=round(ms, 2),
            )
        finally:
            log.corr_id.reset(token)
        response.headers["x-request-id"] = cid
        return response

    def cached(request: Request, response: Response, connection: sqlite3.Connection) -> None:
        """Conditional GET: the tag changes only when ingest changes what a read can return."""
        tag = f'"{db.state_tag(connection)}"'
        if request.headers.get("if-none-match") == tag:
            raise HTTPException(304, headers={"etag": tag})
        response.headers["etag"] = tag
        response.headers["cache-control"] = "no-cache"

    # --- ops ------------------------------------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    def healthz() -> JSONResponse:
        """Exercises the real dependencies: a database query, the package store, the raw result log."""
        checks: dict[str, str] = {}
        try:
            connection = db.connect(cfg.db_path)
            connection.execute("SELECT COUNT(*) FROM run").fetchone()
            connection.close()
            checks["database"] = "ok"
        except sqlite3.Error as exc:
            checks["database"] = f"failed: {exc}"
        try:
            next(iter(cfg.packages_dir.iterdir()), None)
            checks["packages"] = "ok"
        except OSError as exc:
            checks["packages"] = f"failed: {exc.strerror}"
        try:
            cfg.raw_dir.mkdir(parents=True, exist_ok=True)
            checks["raw_log"] = "ok" if os.access(cfg.raw_dir, os.W_OK) else "failed: not writable"
        except OSError as exc:
            checks["raw_log"] = f"failed: {exc.strerror}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503
        )

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    def prometheus() -> str:
        return metrics.render()

    # --- reads ----------------------------------------------------------------------------------

    @app.get("/v1/targets", tags=["explore"], response_model=list[schemas.TargetInfo])
    def list_targets(request: Request, response: Response, connection: Con) -> list[dict[str, Any]]:
        """The hardware targets, their latency budgets, and how much has been measured on each."""
        cached(request, response, connection)
        results = dict(
            connection.execute("SELECT target, COUNT(*) FROM bench WHERE synthetic = 0 GROUP BY target").fetchall()
        )
        packages = dict(connection.execute("SELECT target, COUNT(*) FROM package GROUP BY target").fetchall())
        return [
            {**t, "results": results.get(name, 0), "packages": packages.get(name, 0)} for name, t in targets.items()
        ]

    @app.get("/v1/tradeoff", tags=["explore"], response_model=schemas.Tradeoff, responses=ERRORS)
    def tradeoff(
        request: Request,
        response: Response,
        connection: Con,
        target: Annotated[str, Query(max_length=64)],
        runtime: Annotated[str, Query(max_length=64)] = "onnxruntime",
        synthetic: Annotated[bool, Query(description="generated load-test data instead of measurements")] = False,
    ) -> dict[str, Any]:
        """Every variant of the latest pipeline run with its verified accuracy, joined to what was
        measured for it on `target` with `runtime`. Variants not measured there have `bench: null`."""
        if target not in targets:
            raise problem(404, "unknown_target", f"no target named {target!r}", sorted(targets))
        cached(request, response, connection)
        return db.tradeoff(connection, targets[target], runtime, synthetic)

    @app.get("/v1/results", tags=["explore"], response_model=schemas.ResultPage, responses=ERRORS)
    def results(
        connection: Con,
        target: Annotated[str | None, Query(max_length=64)] = None,
        cursor: Annotated[str | None, Query(max_length=400)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Raw device results, newest first. Keyset-paginated on (received_at, result_id)."""
        try:
            return db.results_page(connection, target, cursor, limit)
        except (ValueError, TypeError) as exc:
            raise problem(400, "bad_cursor", "cursor is not one this API issued") from exc

    @app.get("/v1/sensitivity", tags=["explore"], response_model=schemas.Sensitivity, responses=ERRORS)
    def sensitivity(request: Request, response: Response, connection: Con) -> dict[str, Any]:
        """Per-layer quantization sensitivity from the latest run, most sensitive first."""
        cached(request, response, connection)
        run = db.latest_run(connection)
        rows = (
            []
            if run is None
            else connection.execute(
                "SELECT * FROM sensitivity WHERE run_id = ? ORDER BY rank", (run["run_id"],)
            ).fetchall()
        )
        if run is None or not rows:
            raise problem(404, "no_run", "no pipeline run has been ingested yet")
        return {"run_id": run["run_id"], "variant": rows[0]["variant"], "rows": [dict(r) for r in rows]}

    @app.get("/v1/packages", tags=["packages"], response_model=list[schemas.Package])
    def packages(
        request: Request,
        response: Response,
        connection: Con,
        target: Annotated[str | None, Query(max_length=64)] = None,
    ) -> list[dict[str, Any]]:
        """Deployment packages: one per target and pipeline run."""
        cached(request, response, connection)
        rows = connection.execute(
            "SELECT * FROM package WHERE (? IS NULL OR target = ?) ORDER BY target, name", (target, target)
        ).fetchall()
        return [
            {
                **dict(r),
                "variants": json.loads(r["variants"]),
                "hosted": (cfg.packages_dir / r["filename"]).is_file(),
            }
            for r in rows
        ]

    @app.get("/v1/packages/{package_id}/download", tags=["packages"], response_class=FileResponse, responses=ERRORS)
    def download(package_id: str, connection: Con) -> FileResponse:
        """The archive itself. Its sha256 is the package id; verify it after download."""
        row = connection.execute("SELECT filename FROM package WHERE package_id = ?", (package_id,)).fetchone()
        if row is None:
            raise problem(404, "unknown_package", "no package with this id")
        path = (cfg.packages_dir / row["filename"]).resolve()
        if path.parent != cfg.packages_dir.resolve() or not path.is_file():
            raise problem(
                404,
                "package_not_hosted",
                f"this server does not hold {row['filename']}; rebuild it with `python -m squeeze package`",
            )
        return FileResponse(path, filename=row["filename"], media_type="application/gzip")

    # --- writes ---------------------------------------------------------------------------------

    @app.post(
        "/v1/results",
        tags=["devices"],
        response_model=schemas.Uploaded,
        status_code=201,
        responses={200: {"model": schemas.Uploaded, "description": "already stored (a retry)"}, **ERRORS},
        dependencies=[Depends(require(cfg.device_token, "device"))],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BenchResult"}}},
            }
        },
    )
    async def upload(request: Request, response: Response, connection: Con) -> dict[str, Any]:
        """Upload one benchmark result. Idempotent on `result_id`: repeating an upload returns 200,
        the same id with different content returns 409. Invalid results are quarantined, not dropped."""
        if int(request.headers.get("content-length") or 0) > cfg.max_body:
            raise problem(413, "too_large", f"body exceeds {cfg.max_body} bytes")
        raw = await request.body()
        if len(raw) > cfg.max_body:
            raise problem(413, "too_large", f"body exceeds {cfg.max_body} bytes")
        text = raw.decode("utf-8", errors="replace")
        status, errors, result_id = db.ingest_bench(connection, text, "api", set(targets))
        with metrics.lock:
            metrics.ingest[status] = metrics.ingest.get(status, 0) + 1
        log.event(logger, "ingest", outcome=status, result_id=result_id)
        if status == "quarantined":
            raise problem(422, "invalid_result", "the result failed validation and was quarantined", errors)
        if status == "conflict":
            raise problem(409, "result_conflict", "a different result with this result_id is already stored")
        if status == "created":
            # The raw log is the source of truth the database can be rebuilt from.
            cfg.raw_dir.mkdir(parents=True, exist_ok=True)
            with append_lock, (cfg.raw_dir / "uploaded.jsonl").open("a", encoding="utf-8") as sink:
                sink.write(db.canonical(json.loads(text)) + "\n")
        else:
            response.status_code = 200
        return {"status": status, "result_id": result_id}

    @app.get(
        "/v1/quarantine",
        tags=["devices"],
        response_model=schemas.QuarantinePage,
        responses=ERRORS,
        dependencies=[Depends(require(cfg.admin_token, "admin"))],
    )
    def quarantine(
        connection: Con,
        cursor: Annotated[str | None, Query(max_length=400)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Uploads that failed validation, newest first, with the reasons."""
        try:
            return db.quarantine_page(connection, cursor, limit)
        except (ValueError, TypeError) as exc:
            raise problem(400, "bad_cursor", "cursor is not one this API issued") from exc

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            spec = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            body = schemas.BenchResult.model_json_schema(ref_template="#/components/schemas/{model}")
            spec["components"]["schemas"].update(body.pop("$defs", {}))
            spec["components"]["schemas"]["BenchResult"] = body
            app.openapi_schema = spec
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    if cfg.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=cfg.web_dir, html=True), name="web")
    return app
