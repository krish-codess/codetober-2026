"""Reference API: serves the tiles as static binary buffers and answers selection summaries.

The browser never needs this service for a selection (it has the tiles and DuckDB-WASM); it
exists for clients without a GPU, for local development, and as the independent implementation
the browser's selection code is tested against. It is read-only, so every request is safe to retry.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
import uuid
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import duckdb
import numpy as np
import pyarrow as pa
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, event, get_logger, read_json
from .tiles import COLUMNS, decode_tile, load_variant

MAX_VERTICES = 2048
BUCKETS = (0.005, 0.025, 0.1, 0.5, 2.5)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: object = None):
        self.status, self.code, self.message, self.details = status, code, message, details


class ErrorBody(BaseModel):
    code: str = Field(examples=["unknown_variant"])
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str


class SummaryRequest(BaseModel):
    variant: str = Field(examples=["umap-n15-d0.1"])
    polygon: list[tuple[float, float]] = Field(
        min_length=3,
        max_length=MAX_VERTICES,
        description="Lasso vertices in projection units. Closed implicitly; even-odd fill rule.",
    )
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Keep only points whose label is listed, per column.",
        examples=[{"script": ["Latin", "Cyrillic"]}],
    )


class CategoryCount(BaseModel):
    label: str
    count: int
    share: float = Field(description="count / n")
    lift: float | None = Field(
        description="share in the selection divided by share in the whole dataset; null when the label is absent overall"
    )


class ColumnSummary(BaseModel):
    name: str
    purity: float | None = Field(description="Share of the most common label; null for an empty selection")
    entropy: float | None = Field(
        description="Shannon entropy of the labels, normalised to 0..1 by log(number of labels present overall)"
    )
    counts: list[CategoryCount] = Field(description="Labels present in the selection, most common first")


class RankSummary(BaseModel):
    min: int
    q25: float
    median: float
    q75: float
    max: int


class TopToken(BaseModel):
    token: str
    rank: int


class SummaryResponse(BaseModel):
    variant: str
    build_id: str
    n: int = Field(description="Points selected")
    total: int = Field(description="Points in the variant")
    columns: list[ColumnSummary]
    rank: RankSummary | None = Field(description="Frequency rank of the selected tokens (1 = most frequent)")
    mean_chars: float | None
    top_tokens: list[TopToken] = Field(description="The ten most frequent selected tokens")


class Point(BaseModel):
    id: int
    token: str
    rank: int
    x: float
    y: float
    script: str | None
    kind: str
    cluster: str


class PointsPage(BaseModel):
    items: list[Point]
    next_cursor: str | None = Field(
        description="Pass back as `cursor` for the next page; null on the last page"
    )


class VariantInfo(BaseModel):
    name: str
    label: str
    params: dict
    n_points: int
    tiles: int
    bytes: int
    metrics: dict | None


class VariantsResponse(BaseModel):
    build_id: str
    source: str
    synthetic: bool
    variants: list[VariantInfo]


class Check(BaseModel):
    ok: bool
    detail: str


class Ready(BaseModel):
    status: str
    checks: dict[str, Check]


ERRORS = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def points_in_polygon(x: np.ndarray, y: np.ndarray, polygon: list[tuple[float, float]]) -> np.ndarray:
    """Even-odd ray casting in float64. web/src/lib/geometry.ts uses the same expression in the same order."""
    x, y = x.astype(np.float64), y.astype(np.float64)
    inside = np.zeros(len(x), dtype=bool)
    xj, yj = polygon[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        for xi, yi in polygon:
            straddles = (yi > y) != (yj > y)
            inside ^= straddles & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
            xj, yj = xi, yi
    return inside


def summarise_counts(columns: list[dict], cats: np.ndarray, total: int) -> list[ColumnSummary]:
    n = len(cats)
    out = []
    for ci, col in enumerate(columns):
        counts = np.bincount(cats[:, ci], minlength=len(col["labels"]))
        present_overall = sum(1 for c in col["counts"] if c > 0)
        items = [
            CategoryCount(
                label=col["labels"][code],
                count=int(c),
                share=c / n,
                lift=(c / n) / (col["counts"][code] / total) if col["counts"][code] else None,
            )
            for code, c in enumerate(counts)
            if c > 0
        ]
        items.sort(key=lambda i: (-i.count, i.label))
        p = counts[counts > 0] / n if n else np.zeros(0)
        entropy = (
            float(-(p * np.log(p)).sum() / math.log(present_overall))
            if n and present_overall > 1
            else (0.0 if n else None)
        )
        out.append(
            ColumnSummary(
                name=col["name"], purity=float(p.max()) if n else None, entropy=entropy, counts=items
            )
        )
    return out


class Store:
    """The published site directory, reloaded when manifest.json changes on disk."""

    def __init__(self, site: Path):
        self.site = site
        self._manifest: dict | None = None
        self._mtime = 0.0

    def manifest(self) -> dict:
        path = self.site / "data" / "manifest.json"
        try:
            mtime = path.stat().st_mtime
            if self._manifest is None or mtime != self._mtime:
                self._manifest, self._mtime = read_json(path), mtime
                self.points.cache_clear()
        except (OSError, ValueError) as e:
            raise ApiError(
                503, "no_data", "No dataset has been published yet. Run `python -m dots all`.", str(e)
            ) from e
        return self._manifest

    def variant(self, name: str) -> dict:
        for v in self.manifest()["variants"]:
            if v["name"] == name:
                return v
        raise ApiError(
            404,
            "unknown_variant",
            f"No variant named {name!r}.",
            {"available": [v["name"] for v in self.manifest()["variants"]]},
        )

    @lru_cache(maxsize=4)  # noqa: B019 (one Store per app; bounded: at most four variants decoded)
    def points(self, name: str) -> dict[str, np.ndarray]:
        try:
            return load_variant(self.site, self.manifest(), name)
        except (OSError, ValueError) as e:
            raise ApiError(
                503, "tiles_unreadable", "The published tiles are missing or corrupt.", str(e)
            ) from e

    def meta_path(self) -> str:
        return str(self.site / "data" / self.manifest()["meta"]["path"])


def encode_cursor(build_id: str, after: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"b": build_id, "a": after}).encode()).decode()


def decode_cursor(cursor: str, build_id: str) -> int:
    try:
        c = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        after, b = int(c["a"]), c["b"]
    except (ValueError, KeyError, TypeError, binascii.Error) as e:
        raise ApiError(400, "invalid_cursor", "The cursor is not one this API issued.") from e
    if b != build_id:
        raise ApiError(
            400,
            "stale_cursor",
            "The dataset was rebuilt since this cursor was issued; start again without a cursor.",
        )
    return after


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(
        title="A Million Dots: reference API",
        version="1.0.0",
        description="Tiles as binary buffers under /data, and selection summaries computed server-side. Read-only; no authentication because the data is public and there are no users or tenants.",
    )
    store = Store(s.site_dir)
    requests: dict[tuple[str, int], int] = defaultdict(int)
    latency: dict[str, list[float]] = defaultdict(lambda: [0.0] * (len(BUCKETS) + 2))  # buckets.., +Inf, sum

    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(s.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "x-request-id"],
        )

    def error(request: Request, status: int, code: str, message: str, details: object = None) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(code=code, message=message, details=details), request_id=request.state.request_id
        )
        return JSONResponse(body.model_dump(), status_code=status)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        given = request.headers.get("x-request-id", "")
        request.state.request_id = given if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", given) else uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 (last resort: never a stack trace to the client)
            get_logger().exception(
                "unhandled error",
                extra={"fields": {"request_id": request.state.request_id, "path": request.url.path}},
            )
            response = error(
                request, 500, "internal_error", "Unexpected error. Quote the request_id when reporting it."
            )
        secs = time.perf_counter() - t0
        route = getattr(
            request.scope.get("route"),
            "path",
            "/data" if request.url.path.startswith("/data") else "unmatched",
        )
        requests[(route, response.status_code)] += 1
        hist = latency[route]
        for i, edge in enumerate(BUCKETS):
            hist[i] += secs <= edge
        hist[-2] += 1
        hist[-1] += secs
        response.headers["x-request-id"] = request.state.request_id
        path = request.url.path
        if path.startswith(("/data/", "/assets/")):
            # manifest.json is the one mutable data file; tiles sit under an immutable build id and assets are content-hashed.
            response.headers["cache-control"] = (
                "no-cache" if path.endswith("manifest.json") else "public, max-age=31536000, immutable"
            )
        elif not path.startswith("/api/"):
            response.headers["cache-control"] = "no-cache"
        event(
            "request",
            request_id=request.state.request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(secs * 1000, 1),
        )
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, e: ApiError):
        return error(request, e.status, e.code, e.message, e.details)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(request: Request, e: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "problem": err["msg"]} for err in e.errors()
        ]
        return error(request, 422, "invalid_request", "The request does not match the schema.", details)

    @app.exception_handler(StarletteHTTPException)
    async def on_http(request: Request, e: StarletteHTTPException):
        return error(
            request, e.status_code, "not_found" if e.status_code == 404 else "http_error", str(e.detail)
        )

    @app.get("/healthz", tags=["ops"], summary="Liveness: the process answers")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["ops"],
        response_model=Ready,
        responses={503: {"model": Ready}},
        summary="Readiness: reads the manifest, decodes a tile, queries the metadata",
    )
    def readyz():
        checks: dict[str, Check] = {}

        def check(name: str, fn) -> None:
            try:
                checks[name] = Check(ok=True, detail=str(fn()))
            except Exception as e:  # noqa: BLE001 (a health check reports any failure)
                checks[name] = Check(ok=False, detail=f"{type(e).__name__}: {getattr(e, 'message', e)}")

        def tile() -> str:
            v = store.manifest()["variants"][0]
            z, x, y, n = v["tiles"][0]
            ids, _, _ = decode_tile((s.site_dir / "data" / v["tiles_path"] / f"{z}-{x}-{y}.bin").read_bytes())
            if len(ids) != n:
                raise ValueError(f"root tile has {len(ids)} points, manifest says {n}")
            return f"root tile of {v['name']}: {n} points"

        def meta() -> str:
            rows = (
                duckdb.connect()
                .execute("SELECT count(*) FROM read_parquet(?)", [store.meta_path()])
                .fetchall()[0][0]
            )
            if rows != store.manifest()["meta"]["rows"]:
                raise ValueError(
                    f"meta.parquet has {rows} rows, manifest says {store.manifest()['meta']['rows']}"
                )
            return f"{rows} rows"

        check("manifest", lambda: f"build {store.manifest()['build_id']}")
        check("tiles", tile)
        check("metadata", meta)
        ok = all(c.ok for c in checks.values())
        return JSONResponse(
            Ready(status="ok" if ok else "unavailable", checks=checks).model_dump(),
            status_code=200 if ok else 503,
        )

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse, summary="Prometheus text format")
    def metrics() -> str:
        lines = ["# TYPE dots_requests_total counter"]
        lines += [
            f'dots_requests_total{{route="{r}",status="{st}"}} {c}' for (r, st), c in sorted(requests.items())
        ]
        lines.append("# TYPE dots_request_seconds histogram")
        for route, hist in sorted(latency.items()):
            lines += [
                f'dots_request_seconds_bucket{{route="{route}",le="{edge}"}} {int(hist[i])}'
                for i, edge in enumerate(BUCKETS)
            ]
            lines += [
                f'dots_request_seconds_bucket{{route="{route}",le="+Inf"}} {int(hist[-2])}',
                f'dots_request_seconds_count{{route="{route}"}} {int(hist[-2])}',
                f'dots_request_seconds_sum{{route="{route}"}} {hist[-1]:.6f}',
            ]
        return "\n".join(lines) + "\n"

    @app.get(
        "/api/v1/variants",
        tags=["data"],
        response_model=VariantsResponse,
        responses={503: ERRORS[503]},
        summary="Projection variants in the published build",
    )
    def variants():
        m = store.manifest()
        return VariantsResponse(
            build_id=m["build_id"],
            source=m["source"]["name"],
            synthetic=m["source"]["synthetic"],
            variants=[
                VariantInfo(
                    name=v["name"],
                    label=v["label"],
                    params=v["params"],
                    n_points=v["n_points"],
                    tiles=len(v["tiles"]),
                    bytes=v["bytes"],
                    metrics=v["metrics"],
                )
                for v in m["variants"]
            ],
        )

    def filter_mask(m: dict, cats: np.ndarray, filters: dict[str, list[str]]) -> np.ndarray:
        keep = np.ones(len(cats), dtype=bool)
        for name, labels in filters.items():
            if name not in COLUMNS:
                raise ApiError(
                    422, "invalid_request", f"Cannot filter on {name!r}.", {"filterable": list(COLUMNS)}
                )
            col = m["columns"][COLUMNS.index(name)]
            unknown = [label for label in labels if label not in col["labels"]]
            if unknown:
                raise ApiError(422, "invalid_request", f"Unknown {name} labels.", {"unknown": unknown[:20]})
            keep &= np.isin(cats[:, COLUMNS.index(name)], [col["labels"].index(label) for label in labels])
        return keep

    @app.post(
        "/api/v1/summary",
        tags=["data"],
        response_model=SummaryResponse,
        responses={k: ERRORS[k] for k in (404, 422, 503)},
        summary="Statistics for the points inside a lasso",
    )
    def summary(body: SummaryRequest):
        m = store.manifest()
        store.variant(body.variant)
        if not all(math.isfinite(c) for p in body.polygon for c in p):
            raise ApiError(422, "invalid_request", "Polygon coordinates must be finite numbers.")
        pts = store.points(body.variant)
        (x0, y0), (x1, y1) = np.min(body.polygon, axis=0), np.max(body.polygon, axis=0)
        cand = np.flatnonzero(
            (pts["x"] >= x0)
            & (pts["x"] <= x1)
            & (pts["y"] >= y0)
            & (pts["y"] <= y1)
            & filter_mask(m, pts["cats"], body.filters)
        )
        sel = cand[points_in_polygon(pts["x"][cand], pts["y"][cand], body.polygon)]
        con = duckdb.connect()
        con.execute("SET memory_limit = '1GB'")
        con.register("sel", pa.table({"id": pts["id"][sel]}))
        # The path is a bound parameter and the ids arrive as a registered Arrow table: no value is ever spliced into SQL.
        row = con.execute(
            "SELECT min(rank), quantile_cont(rank, 0.25), median(rank), quantile_cont(rank, 0.75), max(rank), avg(n_chars) FROM read_parquet(?) SEMI JOIN sel USING (id)",
            [store.meta_path()],
        ).fetchall()[0]
        top = con.execute(
            "SELECT token, rank FROM read_parquet(?) SEMI JOIN sel USING (id) ORDER BY rank LIMIT 10",
            [store.meta_path()],
        ).fetchall()
        return SummaryResponse(
            variant=body.variant,
            build_id=m["build_id"],
            n=len(sel),
            total=len(pts["id"]),
            columns=summarise_counts(m["columns"], pts["cats"][sel], len(pts["id"])),
            rank=RankSummary(min=row[0], q25=row[1], median=row[2], q75=row[3], max=row[4])
            if len(sel)
            else None,
            mean_chars=row[5],
            top_tokens=[TopToken(token=t, rank=r) for t, r in top],
        )

    @app.get(
        "/api/v1/points",
        tags=["data"],
        response_model=PointsPage,
        responses={k: ERRORS[k] for k in (400, 404, 422, 503)},
        summary="Points inside a box, ordered by id, one page at a time",
    )
    def points(
        variant: str,
        bbox: Annotated[
            str,
            Query(
                description="x0,y0,x1,y1 in projection units",
                pattern=r"^-?[0-9.eE+-]+(,-?[0-9.eE+-]+){3}$",
                max_length=120,
            ),
        ],
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ):
        m = store.manifest()
        store.variant(variant)
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox.split(","))
        except ValueError as e:
            raise ApiError(422, "invalid_request", "bbox must be four numbers: x0,y0,x1,y1.") from e
        after = decode_cursor(cursor, m["build_id"]) if cursor else -1
        pts = store.points(variant)
        hit = np.flatnonzero(
            (pts["x"] >= min(x0, x1))
            & (pts["x"] <= max(x0, x1))
            & (pts["y"] >= min(y0, y1))
            & (pts["y"] <= max(y0, y1))
            & (pts["id"].astype(np.int64) > after)
        )
        hit = hit[np.argsort(pts["id"][hit], kind="stable")][
            : limit + 1
        ]  # one extra row tells us whether a next page exists
        page = hit[:limit]
        con = duckdb.connect()
        con.register("page", pa.table({"id": pts["id"][page], "x": pts["x"][page], "y": pts["y"][page]}))
        rows = con.execute(
            "SELECT m.id, m.token, m.rank, p.x, p.y, m.script, m.kind, m.cluster FROM read_parquet(?) m JOIN page p USING (id) ORDER BY m.id",
            [store.meta_path()],
        ).fetchall()
        items = [
            Point(id=r[0], token=r[1], rank=r[2], x=r[3], y=r[4], script=r[5], kind=r[6], cluster=r[7])
            for r in rows
        ]
        return PointsPage(
            items=items,
            next_cursor=encode_cursor(m["build_id"], int(pts["id"][page[-1]])) if len(hit) > limit else None,
        )

    (s.site_dir / "data").mkdir(parents=True, exist_ok=True)
    app.mount("/data", StaticFiles(directory=s.site_dir / "data"), name="data")
    if (s.site_dir / "index.html").exists():
        # The same directory a CDN would serve: the built page next to its data.
        app.mount("/", StaticFiles(directory=s.site_dir, html=True), name="site")
    return app
