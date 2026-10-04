"""HTTP API. The OpenAPI document at /api/v1/openapi.json is generated from this file;
docs/API.md is rendered from that, so the reference cannot drift from the code."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import taxonomy
from .config import get_settings
from .db import get_engine
from .embed import Embedder, get_embedder
from .hier import HierModel
from .ingest import ingest_file
from .log import correlation_id, event, metrics, setup_logging
from .metrics import prf, wilson
from .store import JOB_COLS, StoreError, enqueue_job, queue_page, review_page, save_annotation, to_vecs

logger = logging.getLogger(__name__)
MAX_FEED_BYTES = 2_000_000
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", Path(__file__).resolve().parents[2] / "reports"))
ROLE_RANK = {"viewer": 0, "annotator": 1, "admin": 2}


# --- errors: one shape for everything -------------------------------------------------------------


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
    ):
        self.status, self.code, self.message, self.details, self.headers = status, code, message, details, headers


class ErrorBody(BaseModel):
    code: str = Field(examples=["validation_error"])
    message: str
    details: Any | None = None
    request_id: str


class ErrorOut(BaseModel):
    error: ErrorBody


def _error(
    status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details, "request_id": correlation_id.get()}}
    return JSONResponse(body, status_code=status, headers=headers)


ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorOut, "description": desc}
    for code, desc in {
        401: "Missing or invalid bearer token", 403: "Token lacks the required role",
        404: "Not found", 409: "Conflict", 422: "Validation error", 503: "A dependency is unavailable",
    }.items()
}  # fmt: skip


# --- state ---------------------------------------------------------------------------------------


class State:
    """Process-wide caches: the embedder (loaded once) and the active model (reloaded on change)."""

    def __init__(self) -> None:
        self.embedder: Embedder | None = None
        self.embedder_error: str | None = None
        self._model: tuple[int, HierModel] | None = None
        self._lock = threading.Lock()

    def load_embedder(self) -> None:
        try:
            self.embedder = get_embedder(get_settings())
            self.embedder_error = None
        except Exception as e:  # noqa: BLE001 - API must come up without it; /classify reports 503
            self.embedder_error = f"{type(e).__name__}: {e}"
            event(logger, "embedder_unavailable", logging.ERROR, error=self.embedder_error)

    def model(self, conn: Connection) -> tuple[int, HierModel] | None:
        active_id = conn.execute(text("SELECT id FROM model_versions WHERE status = 'active'")).scalar()
        if active_id is None:
            return None
        with self._lock:
            if self._model is None or self._model[0] != active_id:
                blob = conn.execute(
                    text("SELECT artifact FROM model_versions WHERE id = :id"), {"id": active_id}
                ).scalar_one()
                self._model = (active_id, HierModel.from_bytes(bytes(blob)))
                event(logger, "model_loaded", model_version=active_id)
            return self._model


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_level)
    state.load_embedder()
    yield


app = FastAPI(
    title="What are they actually complaining about",
    version="1.0.0",
    description="Hierarchical multilingual feedback classification with an active-learning labelling loop.\n\n"
    "All endpoints except `/health` need `Authorization: Bearer <token>`. Roles: viewer < annotator < admin.\n"
    'Errors always look like `{"error": {code, message, details, request_id}}`.\n'
    "List endpoints are cursor-paginated: pass `next_cursor` back as `cursor`.",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url=None,
    lifespan=lifespan,
)
if get_settings().cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=list(get_settings().cors_origins), allow_methods=["GET", "PUT", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )  # fmt: skip


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Any]) -> Response:
    supplied = request.headers.get("x-request-id", "")
    rid = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied) else uuid.uuid4().hex
    token = correlation_id.set(rid)
    start = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception:  # noqa: BLE001 - last line of defence: never leak a stack trace to a client
        logger.exception("unhandled_error")
        response = _error(500, "internal_error", "Unexpected server error. Quote the request_id when reporting it.")
    elapsed = time.perf_counter() - start
    route = getattr(request.scope.get("route"), "path", "unmatched")
    metrics.inc("http_requests_total", method=request.method, route=route, status=str(response.status_code))
    metrics.observe("http_request", elapsed, route=route)
    event(logger, "request", method=request.method, path=request.url.path, status=response.status_code,
          ms=round(elapsed * 1000, 1))  # fmt: skip
    response.headers["X-Request-ID"] = rid
    correlation_id.reset(token)
    return response


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError) -> JSONResponse:
    return _error(e.status, e.code, e.message, e.details, e.headers)


@app.exception_handler(StoreError)
async def _store_error(_: Request, e: StoreError) -> JSONResponse:
    return _error(e.status, {404: "not_found", 409: "conflict"}.get(e.status, "validation_error"), str(e))


@app.exception_handler(taxonomy.TaxonomyError)
async def _taxonomy_error(_: Request, e: taxonomy.TaxonomyError) -> JSONResponse:
    return _error(
        e.status, {404: "not_found", 409: "conflict", 503: "unavailable"}.get(e.status, "validation_error"), str(e)
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
    details = [{"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]} for err in e.errors()]
    return _error(422, "validation_error", "Request did not match the schema; see details.", details)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
    return _error(e.status_code, code, str(e.detail))


@app.exception_handler(OperationalError)
async def _db_down(_: Request, e: OperationalError) -> JSONResponse:
    event(logger, "database_unavailable", logging.ERROR, error=str(e.orig)[:300])
    return _error(503, "database_unavailable", "The database is not reachable right now. Retry shortly.",
                  headers={"Retry-After": "5"})  # fmt: skip


@app.exception_handler(DBAPIError)
async def _db_rejected(_: Request, e: DBAPIError) -> JSONResponse:
    # Constraint/trigger violations are the database refusing bad data: a client error, not a 500.
    message = str(e.orig).split("\n")[0][:300]
    return _error(409, "constraint_violation", message)


# --- dependencies ---------------------------------------------------------------------------------


def db() -> Any:
    with get_engine().begin() as conn:
        yield conn


# scope="function": commit BEFORE the response is sent, so a client that immediately reads its own
# write sees it, and a failed commit is reported as an error instead of a false success.
Db = Annotated[Connection, Depends(db, scope="function")]


class Principal(BaseModel):
    name: str
    role: Literal["viewer", "annotator", "admin"]


def require(role: str) -> Callable[..., Principal]:
    def dep(conn: Db, authorization: Annotated[str | None, Header()] = None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(
                401, "unauthenticated", "Send 'Authorization: Bearer <token>'.", headers={"WWW-Authenticate": "Bearer"}
            )
        row = conn.execute(
            text("SELECT name, role FROM api_tokens WHERE token_sha256 = :h AND revoked_at IS NULL"),
            {"h": hashlib.sha256(token.strip().encode()).hexdigest()},
        ).one_or_none()
        if row is None:
            raise ApiError(401, "unauthenticated", "Unknown or revoked token.", headers={"WWW-Authenticate": "Bearer"})
        if ROLE_RANK[row.role] < ROLE_RANK[role]:
            raise ApiError(403, "forbidden", f"This action needs the '{role}' role; your token has '{row.role}'.")
        return Principal(name=row.name, role=row.role)

    return dep


Viewer = Annotated[Principal, Depends(require("viewer"))]
Annotator = Annotated[Principal, Depends(require("annotator"))]
Admin = Annotated[Principal, Depends(require("admin"))]
IdemKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200, pattern=r"^[\x21-\x7e]+$",
                                description="Client-chosen key; repeating a request with the same key is a no-op.")]  # fmt: skip
Limit = Annotated[int, Query(ge=1, le=100)]


def encode_cursor(**parts: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()


def decode_cursor(cursor: str | None, *keys: str) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (
            not isinstance(parts, dict)
            or set(parts) != set(keys)
            or not all(isinstance(v, int | float) for v in parts.values())
        ):
            raise ValueError
    except (ValueError, binascii.Error) as e:
        raise ApiError(422, "validation_error", "Invalid cursor: pass back next_cursor exactly as received.") from e
    return parts


# --- health & ops ---------------------------------------------------------------------------------


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checks: dict[str, dict[str, Any]]


@app.get("/api/v1/health", response_model=HealthOut, tags=["ops"], responses={503: {"model": HealthOut}})
def health(response: Response) -> HealthOut:
    """Exercises each dependency: a query against the schema, deserialising the active model,
    and a real embedding call. `down` (503) only when the database is unreachable."""
    checks: dict[str, dict[str, Any]] = {}
    try:
        with get_engine().connect() as conn:
            t = time.perf_counter()
            row = conn.execute(
                text(
                    "SELECT (SELECT version_num FROM alembic_version) AS rev, (SELECT max(id) FROM taxonomy_versions) AS tv"
                )
            ).one()
            checks["database"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1), "schema": row.rev,
                                  "taxonomy_version": row.tv}  # fmt: skip
            current = state.model(conn)
            checks["model"] = {"ok": current is not None, "model_version": current[0] if current else None,
                               **({} if current else {"detail": "no active model yet; label items and retrain"})}  # fmt: skip
    except Exception as e:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    if state.embedder is None:
        checks["embedder"] = {"ok": False, "detail": state.embedder_error or "not loaded"}
    else:
        t = time.perf_counter()
        vec = state.embedder.embed(["health check"])
        checks["embedder"] = {"ok": bool(abs(float(np.linalg.norm(vec[0])) - 1.0) < 1e-3),
                              "ms": round((time.perf_counter() - t) * 1000, 1)}  # fmt: skip
    if not checks["database"]["ok"]:
        response.status_code = 503
        return HealthOut(status="down", checks=checks)
    return HealthOut(status="ok" if all(c["ok"] for c in checks.values()) else "degraded", checks=checks)


@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"], include_in_schema=False)
def prometheus() -> str:
    return metrics.render()


@app.get("/api/v1/me", response_model=Principal, tags=["ops"], responses=ERRORS)
def me(principal: Viewer) -> Principal:
    return principal


class StatsOut(BaseModel):
    pool: int
    test: int
    labelled: int
    needs_review: int
    quarantined: dict[str, int]
    late: int
    text_duplicates: int
    by_lang: dict[str, dict[str, int]]
    taxonomy_version: int | None
    active_model: int | None


@app.get("/api/v1/stats", response_model=StatsOut, tags=["ops"], responses=ERRORS)
def stats(conn: Db, _: Viewer) -> StatsOut:
    one = conn.execute(
        text(
            """SELECT count(*) FILTER (WHERE split = 'pool') AS pool, count(*) FILTER (WHERE split = 'test') AS test,
                      count(*) FILTER (WHERE is_late) AS late, count(*) FILTER (WHERE duplicate_of IS NOT NULL) AS dupes
               FROM feedback"""
        )
    ).one()
    by_lang = {
        r.lang: {"pool": r.pool, "labelled": r.labelled}
        for r in conn.execute(
            text(
                """SELECT COALESCE(f.lang, 'und') AS lang, count(*) AS pool, count(a.feedback_id) AS labelled
                   FROM feedback f LEFT JOIN annotations a ON a.feedback_id = f.id
                   WHERE f.split = 'pool' GROUP BY 1 ORDER BY 2 DESC"""
            )
        )
    }
    return StatsOut(
        pool=one.pool, test=one.test, late=one.late, text_duplicates=one.dupes,
        labelled=sum(v["labelled"] for v in by_lang.values()),
        needs_review=conn.execute(text("SELECT count(DISTINCT feedback_id) FROM labels WHERE review_reason IS NOT NULL")).scalar_one(),
        quarantined=dict(conn.execute(text("SELECT reason, count(*) FROM quarantine GROUP BY reason ORDER BY 2 DESC")).tuples().all()),
        by_lang=by_lang,
        taxonomy_version=conn.execute(text("SELECT max(id) FROM taxonomy_versions")).scalar(),
        active_model=conn.execute(text("SELECT id FROM model_versions WHERE status = 'active'")).scalar(),
    )  # fmt: skip


# --- taxonomy -------------------------------------------------------------------------------------


class NodeOut(BaseModel):
    id: int
    parent_id: int | None
    name: str
    title: str
    path: str
    depth: int
    n_labels: int = Field(description="Pool items labelled with this node")
    n_review: int = Field(
        description="Items (pool + evaluation) whose label on this node is flagged for targeted relabelling"
    )


class TaxonomyOut(BaseModel):
    version: int
    nodes: list[NodeOut]


@app.get("/api/v1/taxonomy", response_model=TaxonomyOut, tags=["taxonomy"], responses=ERRORS)
def get_taxonomy(conn: Db, _: Viewer) -> TaxonomyOut:
    rows = conn.execute(
        text(
            """SELECT n.id, n.parent_id, n.name, n.title, n.path, n.depth,
                      count(l.feedback_id) FILTER (WHERE f.split = 'pool') AS n_labels, count(l.review_reason) AS n_review
               FROM taxonomy_nodes n
               LEFT JOIN (labels l JOIN feedback f ON f.id = l.feedback_id) ON l.node_id = n.id
               WHERE n.retired_version IS NULL GROUP BY n.id ORDER BY n.path"""
        )
    ).all()
    return TaxonomyOut(version=taxonomy.current_version(conn), nodes=[NodeOut(**r._asdict()) for r in rows])


NodeName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,62}$", examples=["battery_life"])]
NodePath = Annotated[str, Field(min_length=1, max_length=400, examples=["hotel/rooms/comfort"])]
Title = Annotated[str, Field(min_length=1, max_length=120)]


class ChildSpec(BaseModel):
    name: NodeName
    title: Title


class AddOp(BaseModel):
    op: Literal["add"]
    parent: NodePath | None = Field(None, description="Path of the parent; null creates a new top-level domain")
    name: NodeName
    title: Title


class SplitOp(BaseModel):
    op: Literal["split"]
    path: NodePath
    children: list[ChildSpec] = Field(min_length=2, max_length=20)


class RenameOp(BaseModel):
    op: Literal["rename"]
    path: NodePath
    name: NodeName
    title: Title | None = None


class MergeOp(BaseModel):
    op: Literal["merge"]
    source: NodePath = Field(description="Leaf node to retire")
    target: NodePath = Field(description="Node that receives its labels")


class MoveOp(BaseModel):
    op: Literal["move"]
    path: NodePath
    new_parent: NodePath | None


class RetireOp(BaseModel):
    op: Literal["retire"]
    path: NodePath


ChangeIn = Annotated[AddOp | SplitOp | RenameOp | MergeOp | MoveOp | RetireOp, Field(discriminator="op")]


class ChangeOut(BaseModel):
    version: int
    op: str
    params: dict[str, Any]
    labels_remapped: int = Field(description="Labels carried over automatically")
    labels_flagged: int = Field(description="Labels queued for targeted human review")
    replayed: bool = Field(False, description="True if this Idempotency-Key had already been applied")
    actor: str | None = None
    created_at: datetime | None = None


@app.post("/api/v1/taxonomy/changes", response_model=ChangeOut, status_code=201, tags=["taxonomy"], responses=ERRORS)
def change_taxonomy(body: ChangeIn, conn: Db, who: Admin, key: IdemKey, response: Response) -> ChangeOut:
    """Apply one taxonomy change atomically. Existing labels are remapped or kept; only the items
    whose answer could differ are flagged for review. A retrain is queued automatically."""
    args = body.model_dump(exclude={"op"})
    result = getattr(taxonomy, body.op)(conn, **args, key=key, actor=who.name)
    if result["replayed"]:
        response.status_code = 200
    else:
        enqueue_job(conn, "retrain", f"taxonomy:v{result['version']}", who.name)
        event(
            logger, "taxonomy_changed", **{k: result[k] for k in ("version", "op", "labels_remapped", "labels_flagged")}
        )
    return ChangeOut(**result)


class ChangesPage(BaseModel):
    items: list[ChangeOut]
    next_cursor: str | None


@app.get("/api/v1/taxonomy/changes", response_model=ChangesPage, tags=["taxonomy"], responses=ERRORS)
def list_changes(conn: Db, _: Viewer, limit: Limit = 20, cursor: str | None = None) -> ChangesPage:
    c = decode_cursor(cursor, "id")
    rows = conn.execute(
        text(
            """SELECT id AS version, op, params, labels_remapped, labels_flagged, actor, created_at
               FROM taxonomy_versions WHERE (CAST(:after AS int) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"""
        ),
        {"after": c["id"] if c else None, "n": limit + 1},
    ).all()
    page = rows[:limit]
    return ChangesPage(
        items=[ChangeOut(**r._asdict()) for r in page],
        next_cursor=encode_cursor(id=page[-1].version) if len(rows) > limit else None,
    )


# --- labelling ------------------------------------------------------------------------------------


class Suggestion(BaseModel):
    node_id: int
    prob: float = Field(description="Calibrated marginal probability")
    selected: bool = Field(description="Part of the model's predicted label set (parent-consistent)")


class QueueItem(BaseModel):
    id: int
    text: str
    lang: str | None
    source: str
    created_at: datetime | None
    is_late: bool
    split: Literal["pool", "test"] = Field(
        description="'test' items are reference labels: admin-only, review mode only"
    )
    suggestions: list[Suggestion]
    confidence: float | None = Field(description="Calibrated P(the selected set is exactly right)")
    uncertainty: float | None
    scored_by_model: int | None = Field(description="Model version that produced the suggestions")
    current_node_ids: list[int] = Field(description="Existing labels (review mode)")
    review_reason: str | None


class QueuePage(BaseModel):
    items: list[QueueItem]
    next_cursor: str | None
    active_model: int | None
    remaining: int = Field(description="Items left in this queue")


def _suggest(model: HierModel, node_ids: list[int], probs: list[float]) -> list[Suggestion]:
    alive = set(model.tree.index)  # nodes retired after scoring are dropped
    chosen: set[int] = set()
    out = []
    for n, p in zip(node_ids, probs, strict=True):  # most probable first, so parents precede children
        if n not in alive:
            continue
        parent_pos = model.tree.parent[model.tree.index[n]]
        ok = p >= model.threshold and (parent_pos < 0 or int(model.tree.node_ids[parent_pos]) in chosen)
        if ok:
            chosen.add(n)
        out.append(Suggestion(node_id=n, prob=round(p, 4), selected=ok))
    return out


@app.get("/api/v1/queue", response_model=QueuePage, tags=["labelling"], responses=ERRORS)
def get_queue(
    conn: Db, _: Annotator, mode: Literal["uncertain", "review"] = "uncertain", limit: Limit = 20,
    cursor: str | None = None, lang: Annotated[str | None, Query(pattern="^[a-z]{2,3}$")] = None,
) -> QueuePage:  # fmt: skip
    """What to label next. `uncertain`: unlabelled items ordered by diversified model uncertainty.
    `review`: items a taxonomy change flagged, with fresh suggestions from the active model."""
    current = state.model(conn)
    model = current[1] if current else None
    items: list[QueueItem] = []
    next_cursor = None
    if mode == "uncertain":
        c = decode_cursor(cursor, "p", "id")
        rows = queue_page(conn, limit=limit + 1, after=(c["p"], int(c["id"])) if c else None, lang=lang)
        for r in rows[:limit]:
            items.append(QueueItem(
                id=r.id, text=r.text, lang=r.lang, source=r.source, created_at=r.created_at, is_late=r.is_late, split=r.split,
                suggestions=_suggest(model, r.node_ids, r.probs) if model else [],
                confidence=r.confidence, uncertainty=r.uncertainty, scored_by_model=r.model_version_id,
                current_node_ids=[], review_reason=None,
            ))  # fmt: skip
        if len(rows) > limit:
            next_cursor = encode_cursor(p=rows[limit - 1].priority, id=rows[limit - 1].id)
        remaining = conn.execute(
            text(
                """SELECT count(*) FROM predictions p JOIN feedback f ON f.id = p.feedback_id
                   WHERE NOT EXISTS (SELECT 1 FROM annotations a WHERE a.feedback_id = p.feedback_id)
                     AND f.duplicate_of IS NULL AND (CAST(:lang AS text) IS NULL OR f.lang = :lang)"""
            ),
            {"lang": lang},
        ).scalar_one()
    else:
        c = decode_cursor(cursor, "id")
        rows = review_page(conn, limit=limit + 1, after_id=int(c["id"]) if c else 0, lang=lang)
        page = rows[:limit]
        if page and model:
            p = model.marginals(to_vecs([bytes(r.vec) for r in page]))
            conf = model.confidence(p)
        for i, r in enumerate(page):
            suggestions: list[Suggestion] = []
            if model:
                top = [int(j) for j in np.argsort(-p[i], kind="stable")[:12] if p[i, j] >= 0.05]
                suggestions = _suggest(model, [int(model.tree.node_ids[j]) for j in top], [float(p[i, j]) for j in top])
            items.append(QueueItem(
                id=r.id, text=r.text, lang=r.lang, source=r.source, created_at=r.created_at, is_late=r.is_late, split=r.split,
                suggestions=suggestions, confidence=float(conf[i]) if model else None, uncertainty=None,
                scored_by_model=current[0] if current else None, current_node_ids=r.current_node_ids,
                review_reason=r.review_reason,
            ))  # fmt: skip
        if len(rows) > limit:
            next_cursor = encode_cursor(id=page[-1].id)
        remaining = conn.execute(
            text("SELECT count(DISTINCT feedback_id) FROM labels WHERE review_reason IS NOT NULL")
        ).scalar_one()
    return QueuePage(
        items=items, next_cursor=next_cursor, active_model=current[0] if current else None, remaining=remaining
    )


class AnnotationIn(BaseModel):
    node_ids: list[int] = Field(max_length=50, description="Most specific nodes; ancestors are added by the server. "
                                                           "An empty list means 'no category applies'.")  # fmt: skip


class AnnotationOut(BaseModel):
    feedback_id: int
    node_ids: list[int] = Field(description="Stored label set, including implied ancestors")


@app.put("/api/v1/items/{item_id}/annotation", response_model=AnnotationOut, tags=["labelling"], responses=ERRORS)
def put_annotation(item_id: int, body: AnnotationIn, conn: Db, who: Annotator) -> AnnotationOut:
    """Replace an item's label set (idempotent). Clears any review flag on the item."""
    out = save_annotation(conn, item_id, body.node_ids, annotator=who.name, allow_reference=who.role == "admin")
    metrics.inc("annotations_total")
    return AnnotationOut(**out)


# --- classification -------------------------------------------------------------------------------


class ClassifyIn(BaseModel):
    texts: list[Annotated[str, Field(min_length=1, max_length=4000)]] = Field(min_length=1, max_length=64)
    auto_threshold: float = Field(
        0.9, ge=0.5, le=1.0, description="Confidence at or above which a result is routed automatically"
    )


class LabelOut(BaseModel):
    node_id: int
    path: str
    prob: float


class ClassifyResult(BaseModel):
    labels: list[LabelOut] = Field(description="Predicted set; always closed under 'child implies parent'")
    confidence: float
    route: Literal["auto", "review"]


class ClassifyOut(BaseModel):
    model_version: int
    results: list[ClassifyResult]


@app.post("/api/v1/classify", response_model=ClassifyOut, tags=["classification"], responses=ERRORS)
def classify(body: ClassifyIn, conn: Db, _: Viewer) -> ClassifyOut:
    current = state.model(conn)
    if current is None:
        raise ApiError(503, "no_model", "No model has been trained yet. Label some items and run a retrain.")
    if state.embedder is None:
        raise ApiError(503, "embedder_unavailable", f"The embedding model could not be loaded: {state.embedder_error}")
    model_id, model = current
    t = time.perf_counter()
    p = model.marginals(state.embedder.embed(body.texts))
    pred, conf = model.decode(p.copy()), model.confidence(p)
    paths = dict(conn.execute(text("SELECT id, path FROM taxonomy_nodes WHERE id = ANY(:ids)"),
                              {"ids": [int(n) for n in model.tree.node_ids]}).tuples().all())  # fmt: skip
    metrics.observe("classify", time.perf_counter() - t)
    metrics.inc("classified_texts_total", len(body.texts))
    results = []
    for i in range(len(body.texts)):
        labels = [LabelOut(node_id=int(model.tree.node_ids[j]), path=paths[int(model.tree.node_ids[j])], prob=round(float(p[i, j]), 4))
                  for j in np.flatnonzero(pred[i])]  # fmt: skip
        results.append(ClassifyResult(labels=labels, confidence=round(float(conf[i]), 4),
                                      route="auto" if conf[i] >= body.auto_threshold else "review"))  # fmt: skip
    return ClassifyOut(model_version=model_id, results=results)


# --- models & performance -------------------------------------------------------------------------


class ModelOut(BaseModel):
    id: int
    status: str
    taxonomy_version: int
    n_labeled: int
    train_data_sha256: str
    code_version: str
    embed_model: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    gate: dict[str, Any]
    artifact_sha256: str
    created_at: datetime


class ModelsPage(BaseModel):
    items: list[ModelOut]
    next_cursor: str | None


MODEL_COLS = ("id, status, taxonomy_version, n_labeled, train_data_sha256, code_version, embed_model, params, metrics, "
              "gate, artifact_sha256, created_at")  # fmt: skip


@app.get("/api/v1/models", response_model=ModelsPage, tags=["models"], responses=ERRORS)
def list_models(conn: Db, _: Viewer, limit: Limit = 20, cursor: str | None = None) -> ModelsPage:
    c = decode_cursor(cursor, "id")
    rows = conn.execute(
        text(
            f"SELECT {MODEL_COLS} FROM model_versions WHERE (CAST(:a AS int) IS NULL OR id < :a) ORDER BY id DESC LIMIT :n"
        ),  # noqa: S608
        {"a": c["id"] if c else None, "n": limit + 1},
    ).all()
    page = rows[:limit]
    return ModelsPage(items=[ModelOut(**r._asdict()) for r in page],
                      next_cursor=encode_cursor(id=page[-1].id) if len(rows) > limit else None)  # fmt: skip


class NodeMetric(BaseModel):
    node_id: int
    path: str
    title: str
    depth: int
    parent_id: int | None
    support: int = Field(description="Held-out items that truly have this label")
    predicted: int
    precision: float | None = Field(description="null when the node was never predicted")
    recall: float | None = Field(description="null when the node has no held-out examples")
    f1: float
    precision_ci: tuple[float, float]
    recall_ci: tuple[float, float]


class NodeMetricsOut(BaseModel):
    model_version: int
    lang: str
    languages: list[str]
    overall: dict[str, Any]
    by_lang: dict[str, dict[str, Any]]
    calibration: dict[str, Any]
    nodes: list[NodeMetric]


@app.get("/api/v1/metrics/nodes", response_model=NodeMetricsOut, tags=["models"], responses=ERRORS)
def node_metrics(conn: Db, _: Viewer, model_version: int | None = None,
                 lang: Annotated[str, Query(pattern="^([a-z]{2,3}|all)$")] = "all") -> NodeMetricsOut:  # fmt: skip
    """Per-node precision/recall on the held-out split, with 95% Wilson intervals, for one language or all."""
    row = conn.execute(
        text(
            "SELECT id, metrics FROM model_versions WHERE (CAST(:id AS int) IS NULL AND status = 'active') OR id = :id"
        ),
        {"id": model_version},
    ).one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "No such model version (or no active model yet).")
    rows = conn.execute(
        text(
            """SELECT n.id AS node_id, n.path, n.title, n.depth, n.parent_id,
                      COALESCE(m.tp, 0) AS tp, COALESCE(m.fp, 0) AS fp, COALESCE(m.fn, 0) AS fn
               FROM taxonomy_nodes n LEFT JOIN node_metrics m
                    ON m.node_id = n.id AND m.model_version_id = :m AND m.lang = :lang
               WHERE n.retired_version IS NULL ORDER BY n.path"""
        ),
        {"m": row.id, "lang": lang},
    ).all()
    nodes = []
    for r in rows:
        p, rec, f1 = prf(r.tp, r.fp, r.fn)
        nodes.append(NodeMetric(
            node_id=r.node_id, path=r.path, title=r.title, depth=r.depth, parent_id=r.parent_id,
            support=r.tp + r.fn, predicted=r.tp + r.fp, precision=p if r.tp + r.fp else None,
            recall=rec if r.tp + r.fn else None, f1=f1, precision_ci=wilson(r.tp, r.tp + r.fp), recall_ci=wilson(r.tp, r.tp + r.fn),
        ))  # fmt: skip
    m = dict(row.metrics)
    by_lang = m.pop("by_lang", {})
    calibration = m.pop("calibration", {})
    if lang != "all" and lang not in by_lang:
        raise ApiError(404, "not_found", f"Model {row.id} has no held-out data for language '{lang}'.")
    return NodeMetricsOut(model_version=row.id, lang=lang, languages=sorted(by_lang), overall=m if lang == "all" else by_lang[lang],
                          by_lang=by_lang, calibration=calibration, nodes=nodes)  # fmt: skip


class EfficiencyPoint(BaseModel):
    model_version: int
    n_labeled: int
    hf1: float
    hf1_ci95: tuple[float, float] | None
    status: str
    created_at: datetime


class EfficiencyOut(BaseModel):
    live: list[EfficiencyPoint] = Field(description="One point per trained model version in this deployment")
    simulation: dict[str, Any] | None = Field(
        description="Committed offline experiment (reports/label_efficiency.json)"
    )


@app.get("/api/v1/metrics/efficiency", response_model=EfficiencyOut, tags=["models"], responses=ERRORS)
def efficiency(conn: Db, _: Viewer) -> EfficiencyOut:
    """Label-efficiency curve: held-out hF1 against number of labelled items."""
    rows = conn.execute(
        text(
            """SELECT id AS model_version, n_labeled, (metrics->>'hf1')::float AS hf1, metrics->'hf1_ci95' AS hf1_ci95,
                      status, created_at FROM model_versions ORDER BY id DESC LIMIT 200"""
        )
    ).all()
    report = REPORTS_DIR / "label_efficiency.json"
    return EfficiencyOut(
        live=[EfficiencyPoint(**r._asdict()) for r in reversed(rows)],
        simulation=json.loads(report.read_text(encoding="utf-8")) if report.exists() else None,
    )


# --- jobs -----------------------------------------------------------------------------------------


class JobOut(BaseModel):
    id: int
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: float = Field(description="0..1, updated by the worker at each stage")
    stage: str
    attempts: int
    error: str | None
    result: dict[str, Any] | None
    requested_by: str
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobIn(BaseModel):
    kind: Literal["retrain"] = "retrain"


class JobsPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


@app.post("/api/v1/jobs", response_model=JobOut, status_code=202, tags=["jobs"], responses=ERRORS)
def create_job(body: JobIn, conn: Db, who: Admin, key: IdemKey) -> JobOut:
    """Queue a retrain. Safe to retry: the same Idempotency-Key returns the same job."""
    return JobOut(**enqueue_job(conn, body.kind, f"api:{key}", who.name))


@app.get("/api/v1/jobs", response_model=JobsPage, tags=["jobs"], responses=ERRORS)
def list_jobs(conn: Db, _: Viewer, limit: Limit = 10, cursor: str | None = None) -> JobsPage:
    c = decode_cursor(cursor, "id")
    rows = conn.execute(
        text(f"SELECT {JOB_COLS} FROM jobs WHERE (CAST(:a AS int) IS NULL OR id < :a) ORDER BY id DESC LIMIT :n"),  # noqa: S608
        {"a": c["id"] if c else None, "n": limit + 1},
    ).all()
    page = rows[:limit]
    return JobsPage(items=[JobOut(**r._asdict()) for r in page],
                    next_cursor=encode_cursor(id=page[-1].id) if len(rows) > limit else None)  # fmt: skip


@app.get("/api/v1/jobs/{job_id}", response_model=JobOut, tags=["jobs"], responses=ERRORS)
def get_job(job_id: int, conn: Db, _: Viewer) -> JobOut:
    row = conn.execute(text(f"SELECT {JOB_COLS} FROM jobs WHERE id = :id"), {"id": job_id}).one_or_none()  # noqa: S608
    if row is None:
        raise ApiError(404, "not_found", f"Job {job_id} does not exist.")
    return JobOut(**row._asdict())


# --- ingestion ------------------------------------------------------------------------------------


class IngestOut(BaseModel):
    batch_id: int
    n_records: int
    n_accepted: int
    n_quarantined: int
    n_repaired: int
    n_late: int
    already_ingested: bool
    quarantined_by_reason: dict[str, int] = {}
    job: JobOut | None = Field(description="Embedding + scoring job for the new rows")


@app.post("/api/v1/feedback", response_model=IngestOut, tags=["ingestion"], responses={**ERRORS, 413: {"model": ErrorOut}},
          openapi_extra={"requestBody": {"required": True, "content": {"application/x-ndjson": {"schema": {"type": "string"}}}}})  # fmt: skip
async def post_feedback(request: Request, who: Admin) -> IngestOut:
    """Ingest a batch of feedback as newline-delimited JSON (max 2 MB). Invalid lines are
    quarantined, not dropped and not fatal. Re-posting identical bytes is a no-op."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
        raise ApiError(413, "payload_too_large", f"Body exceeds {MAX_FEED_BYTES} bytes; split the batch.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FEED_BYTES:
            raise ApiError(413, "payload_too_large", f"Body exceeds {MAX_FEED_BYTES} bytes; split the batch.")
    if not body.strip():
        raise ApiError(422, "validation_error", "Body is empty; send one JSON object per line.")
    sha = hashlib.sha256(body).hexdigest()
    path = get_settings().data_dir / "feeds" / f"api-{sha[:16]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():  # raw input is kept on disk as well as in raw_feedback
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.part")
        tmp.write_bytes(body)
        tmp.replace(path)
    result = await run_in_threadpool(ingest_file, get_engine(), path, "api")
    with get_engine().begin() as conn:
        job = enqueue_job(conn, "embed_score", f"ingest:{sha}", who.name) if result["n_accepted"] else None
    return IngestOut(
        **{k: result[k] for k in IngestOut.model_fields if k in result}, job=JobOut(**job) if job else None
    )


class QuarantineItem(BaseModel):
    batch_id: int
    line_no: int
    reason: str
    detail: str
    payload_preview: str = Field(description="First 300 bytes of the raw line, lossily decoded")
    created_at: datetime


class QuarantinePage(BaseModel):
    items: list[QuarantineItem]
    next_cursor: str | None


@app.get("/api/v1/quarantine", response_model=QuarantinePage, tags=["ingestion"], responses=ERRORS)
def list_quarantine(conn: Db, _: Admin, limit: Limit = 20, cursor: str | None = None,
                    reason: Annotated[str | None, Query(pattern="^[a-z_]{1,40}$")] = None) -> QuarantinePage:  # fmt: skip
    c = decode_cursor(cursor, "b", "n")
    rows = conn.execute(
        text(
            """SELECT q.batch_id, q.line_no, q.reason, q.detail, q.created_at, substring(r.payload FROM 1 FOR 300) AS head
               FROM quarantine q JOIN raw_feedback r ON r.batch_id = q.batch_id AND r.line_no = q.line_no
               WHERE (CAST(:reason AS text) IS NULL OR q.reason = :reason)
                 AND (CAST(:b AS int) IS NULL OR (q.batch_id, q.line_no) > (:b, :n))
               ORDER BY q.batch_id, q.line_no LIMIT :lim"""
        ),
        {"reason": reason, "b": c["b"] if c else None, "n": c["n"] if c else None, "lim": limit + 1},
    ).all()
    page = rows[:limit]
    return QuarantinePage(
        items=[QuarantineItem(batch_id=r.batch_id, line_no=r.line_no, reason=r.reason, detail=r.detail, created_at=r.created_at,
                              payload_preview=bytes(r.head).decode("utf-8", "replace")) for r in page],
        next_cursor=encode_cursor(b=page[-1].batch_id, n=page[-1].line_no) if len(rows) > limit else None,
    )  # fmt: skip
