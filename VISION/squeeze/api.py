"""HTTP API. The contract is the code: /openapi.json and docs/openapi.json are generated from it."""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import config, log

logger = logging.getLogger("squeeze.api")


def create_app(settings: config.Settings | None = None) -> FastAPI:
    cfg = settings or config.load()
    app = FastAPI(title="squeeze", version="0.1.0", description=__doc__)
    app.state.cfg = cfg

    @app.middleware("http")
    async def correlate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        cid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        token = log.corr_id.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            log.corr_id.reset(token)
        response.headers["x-request-id"] = cid
        ms = (time.perf_counter() - start) * 1000
        log.event(
            logger,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(ms, 2),
        )
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz() -> JSONResponse:
        """Exercises the real dependencies: a database query and a listing of the package store."""
        checks: dict[str, str] = {}
        try:
            with sqlite3.connect(cfg.db_path) as con:
                con.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
        except sqlite3.Error as exc:
            checks["database"] = f"failed: {exc}"
        try:
            next(iter(cfg.packages_dir.iterdir()), None)
            checks["packages"] = "ok"
        except OSError as exc:
            checks["packages"] = f"failed: {exc.strerror}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503
        )

    return app
