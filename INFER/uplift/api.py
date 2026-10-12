"""HTTP API: model comparison, segments, scores, online prediction, and budgeted policies.

Contract, for every endpoint:
  * `Authorization: Bearer <token>`. The read token may read; only the admin token may create or export a policy.
  * Errors are always `{"error": {"code", "message", "request_id", "details"?}}` - never a stack trace.
  * Every response carries `X-Request-ID` (yours if you sent one), and the same id is on every log line.
  * Lists are cursor-paginated with a stable sort. POST /v1/policies is idempotent on `Idempotency-Key`.
"""

from __future__ import annotations

import base64
import binascii
import csv
import functools
import hmac
import io
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from uplift import config, db, policy
from uplift.config import correlation_id, log

logger = logging.getLogger("uplift.api")
settings = config.load()

REQUESTS = Counter("uplift_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("uplift_http_request_seconds", "HTTP request duration", ["route"])
POLICIES = Counter("uplift_policies_created_total", "Policies created (replays excluded)")
PREDICTED = Counter("uplift_units_predicted_total", "Units scored through the online endpoint")
DB_UNAVAILABLE = Counter("uplift_db_unavailable_total", "Requests that failed because the database was unreachable")

pool = ConnectionPool(
    settings.api_database_url,
    min_size=1,
    max_size=8,
    timeout=3,
    open=False,
    kwargs={"row_factory": dict_row, "connect_timeout": 3},
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config.setup_logging(settings.log_level)
    pool.open(wait=False)  # start even if the database is down: /readyz says so, and it recovers on its own
    yield
    pool.close()


app = FastAPI(title="Who actually needed the discount", version="0.1.0", description=__doc__, lifespan=lifespan)


@contextmanager
def connection() -> Iterator[psycopg.Connection[Any]]:
    with pool.connection() as conn:
        yield conn


# ---------------------------------------------------------------- errors, request ids, metrics


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(status, {"code": code, "message": message}, headers)


def _error(
    status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": correlation_id.get()}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return _error(exc.status_code, exc.detail["code"], exc.detail["message"], headers=exc.headers)
    return _error(exc.status_code, "not_found" if exc.status_code == 404 else "http_error", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()]
    return _error(422, "invalid_request", "The request did not match the schema; see details.", details)


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(PoolTimeout)
async def _database_down(_: Request, exc: Exception) -> JSONResponse:
    DB_UNAVAILABLE.inc()
    logger.error("database unavailable: %s", str(exc).strip().splitlines()[0] if str(exc) else type(exc).__name__)
    return _error(
        503,
        "database_unavailable",
        "The database cannot be reached right now. Retry shortly.",
        headers={"Retry-After": "5"},
    )


@app.middleware("http")
async def _observe(request: Request, call_next: Any) -> Response:
    supplied = request.headers.get("x-request-id", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", supplied) else uuid.uuid4().hex
    correlation_id.set(request_id)
    started = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception:  # anything a handler above did not turn into a response
        logger.exception("unhandled error")
        response = _error(
            500, "internal_error", "Something went wrong on our side. Quote the request_id if you report it."
        )
    seconds = time.perf_counter() - started
    route = getattr(request.scope.get("route"), "path", "unmatched")
    REQUESTS.labels(request.method, route, response.status_code).inc()
    LATENCY.labels(route).observe(seconds)
    response.headers["X-Request-ID"] = request_id
    if route not in ("/healthz", "/readyz", "/metrics"):
        log(
            logger,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(seconds * 1000, 1),
        )
    return response


# ---------------------------------------------------------------- auth


def role(authorization: Annotated[str | None, Header()] = None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token and hmac.compare_digest(token.encode(), settings.admin_token.encode()):
        return "admin"
    if token and hmac.compare_digest(token.encode(), settings.read_token.encode()):
        return "read"
    raise ApiError(
        401, "unauthorized", "Send a valid token as 'Authorization: Bearer <token>'.", {"WWW-Authenticate": "Bearer"}
    )


def admin(who: Annotated[str, Depends(role)]) -> str:
    if who != "admin":
        raise ApiError(403, "forbidden", "This token can read results but cannot create or export policies.")
    return who


Reader = Annotated[str, Depends(role)]
Admin = Annotated[str, Depends(admin)]

# ---------------------------------------------------------------- schemas


class Health(BaseModel):
    status: str
    checks: dict[str, str]


class DatasetOut(BaseModel):
    name: str
    outcome: str
    units: int
    quarantined: int
    quarantine_reasons: dict[str, int]
    experiment_id: uuid.UUID | None = Field(description="The experiment being served, if one has completed")
    trained_at: str | None
    stale: bool = Field(description="True when files were ingested after the served experiment was trained")


class RunOut(BaseModel):
    id: uuid.UUID
    learner: str
    status: Literal["complete", "failed"]
    error: str | None
    is_champion: bool
    scored: bool = Field(description="Whether batch scoring has run for this model; policies need it")
    fit_seconds: float | None
    metrics: dict[str, Any] | None
    curves: dict[str, list[list[float]]] | None = Field(
        description="Qini and uplift curves on the test split, 101 [fraction, value] points each"
    )


class ExperimentOut(BaseModel):
    id: uuid.UUID
    dataset: str
    outcome: str
    status: str
    stale: bool
    split_strategy: str | None
    data_version: str
    code_version: str
    finished_at: str | None
    params: dict[str, Any]
    audit: dict[str, Any] | None
    timings: dict[str, float] | None
    runs: list[RunOut]


class DecileOut(BaseModel):
    decile: int
    n_treated: int
    n_control: int
    mean_predicted: float
    observed_uplift: float
    ci_low: float
    ci_high: float
    true_uplift: float | None


class SegmentOut(BaseModel):
    definition: str
    n_treated: int
    n_control: int
    uplift: float
    ci_low: float
    ci_high: float
    p_adjusted: float
    verdict: Literal["helps", "hurts", "unclear"]


class SegmentPage(BaseModel):
    items: list[SegmentOut]
    next_cursor: str | None


class ScoreOut(BaseModel):
    unit_id: str
    split: str
    uplift: float


class ScorePage(BaseModel):
    items: list[ScoreOut]
    next_cursor: str | None


class UnitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recency: int | None = Field(None, ge=1, le=12, description="Months since last purchase")
    history: float | None = Field(None, ge=0, description="Dollars spent in the past year")
    mens: bool | None = None
    womens: bool | None = None
    newbie: bool | None = None
    zip_code: Literal["Surburban", "Urban", "Rural"] | None = None
    channel: Literal["Web", "Phone", "Multichannel"] | None = None


class PredictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: list[UnitIn] = Field(min_length=1, max_length=1000)


class PredictOut(BaseModel):
    run_id: uuid.UUID
    learner: str
    uplift: list[float] = Field(
        description="Predicted change in the outcome if treated, one per unit, in request order"
    )


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    budget: Decimal = Field(ge=0, le=Decimal("1e11"), description="Total money available for treatment")
    cost_per_treatment: Decimal = Field(gt=0, le=Decimal("1e5"))
    value_per_outcome: Decimal = Field(
        gt=0, le=Decimal("1e7"), description="Money one outcome (e.g. one visit) is worth"
    )


class PolicyOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    budget: Decimal
    cost_per_treatment: Decimal
    value_per_outcome: Decimal
    population: int = Field(description="Held-out units the policy chooses among")
    n_targeted: int
    spend: Decimal = Field(description="n_targeted * cost_per_treatment; never more than the budget")
    min_uplift: float | None = Field(description="Predicted uplift of the last unit targeted")
    evaluation: dict[str, Any] = Field(
        description="Estimated value against treating everyone and no one, from the experiment"
    )
    created_at: str


# ---------------------------------------------------------------- helpers

_SERVED = (
    "SELECT e.*, d.name AS dataset, d.outcome FROM experiment e JOIN dataset d ON d.id = e.dataset_id"
    " WHERE d.name = %s AND e.status = 'complete' ORDER BY e.finished_at DESC LIMIT 1"
)


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
            or not all(isinstance(v, k) for v, k in zip(values, kinds, strict=True))
        ):
            raise ValueError
    except (ValueError, binascii.Error) as exc:
        raise ApiError(400, "invalid_cursor", "The cursor is not one this API issued. Start again without it.") from exc
    return values


def _run(conn: psycopg.Connection[Any], run_id: uuid.UUID) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM model_run WHERE id = %s", [run_id]).fetchone()
    if run is None:
        raise ApiError(404, "run_not_found", f"No model run {run_id}.")
    return dict(run)


def _policy_out(p: dict[str, Any]) -> PolicyOut:
    return PolicyOut(
        **{**p, "spend": p["n_targeted"] * p["cost_per_treatment"], "created_at": p["created_at"].isoformat()}
    )


@functools.lru_cache(maxsize=4)
def _model(artifact_file: str, sha256: str) -> dict[str, Any]:
    from uplift.train import load_artifact  # the learners (and EconML) load on first prediction, not at startup

    return load_artifact(settings, artifact_file, sha256)


# ---------------------------------------------------------------- operational endpoints


@app.get("/healthz", response_model=Health, tags=["operations"], summary="Liveness: the process is up")
def healthz() -> Health:
    return Health(status="ok", checks={})


@app.get(
    "/readyz",
    response_model=Health,
    tags=["operations"],
    summary="Readiness: exercises the database and the artifact store",
)
def readyz(response: Response) -> Health:
    checks: dict[str, str] = {}
    try:
        with connection() as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            checks["database"] = (
                f"ok (schema {row['version_num']})" if row else "failed: no schema version; run migrations"
            )
    except (psycopg.Error, PoolTimeout) as exc:
        checks["database"] = f"failed: {type(exc).__name__}"
    try:
        checks["artifacts"] = (
            "ok"
            if settings.artifact_dir.is_dir() and any(settings.artifact_dir.iterdir())
            else "failed: no model artifacts found"
        )
    except OSError as exc:
        checks["artifacts"] = f"failed: {type(exc).__name__}"
    ok = all(v.startswith("ok") for v in checks.values())
    if not ok:
        response.status_code = 503
    return Health(status="ok" if ok else "unavailable", checks=checks)


@app.get("/metrics", include_in_schema=False)
def prometheus() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- read endpoints


@app.get(
    "/v1/datasets",
    response_model=list[DatasetOut],
    tags=["results"],
    summary="Datasets, what was quarantined, and what is being served",
)
def datasets(_: Reader) -> list[DatasetOut]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT d.id, d.name, d.outcome, (SELECT count(*) FROM unit u WHERE u.dataset_id = d.id) AS units,"
            " (SELECT coalesce(sum(rows_quarantined), 0) FROM raw_file f WHERE f.dataset_id = d.id) AS quarantined"
            " FROM dataset d ORDER BY d.name"
        ).fetchall()
        out = []
        for r in rows:
            reasons = conn.execute(
                "SELECT q.reason, count(*) AS n FROM quarantine q JOIN raw_file f ON f.sha256 = q.raw_sha"
                " WHERE f.dataset_id = %s GROUP BY q.reason ORDER BY n DESC",
                [r["id"]],
            ).fetchall()
            served = conn.execute(_SERVED, [r["name"]]).fetchone()
            out.append(
                DatasetOut(
                    name=r["name"], outcome=r["outcome"], units=r["units"], quarantined=r["quarantined"],
                    quarantine_reasons={q["reason"]: q["n"] for q in reasons},
                    experiment_id=served["id"] if served else None,
                    trained_at=served["finished_at"].isoformat() if served else None,
                    stale=served is not None and served["data_version"] != db.data_version(conn, r["id"]),
                )
            )  # fmt: skip
    return out


@app.get(
    "/v1/datasets/{name}/experiment", response_model=ExperimentOut, tags=["results"],
    summary="The served experiment: every learner's metrics and curves on identical data",
)  # fmt: skip
def experiment(name: str, _: Reader) -> ExperimentOut:
    with connection() as conn:
        e = conn.execute(_SERVED, [name]).fetchone()
        if e is None:
            raise ApiError(404, "no_experiment", f"Dataset {name!r} has no completed experiment yet.")
        runs = conn.execute(
            "SELECT r.*, EXISTS (SELECT 1 FROM score s WHERE s.run_id = r.id) AS scored FROM model_run r"
            " WHERE r.experiment_id = %s ORDER BY r.learner",
            [e["id"]],
        ).fetchall()
        stale = e["data_version"] != db.data_version(conn, e["dataset_id"])
    return ExperimentOut(
        **{**e, "finished_at": e["finished_at"].isoformat(), "stale": stale, "runs": [RunOut(**r) for r in runs]}
    )


@app.get(
    "/v1/runs/{run_id}/deciles",
    response_model=list[DecileOut],
    tags=["results"],
    summary="Predicted vs observed uplift by score decile (test split)",
)
def deciles(run_id: uuid.UUID, _: Reader) -> list[DecileOut]:
    with connection() as conn:
        _run(conn, run_id)
        rows = conn.execute("SELECT * FROM decile WHERE run_id = %s ORDER BY decile", [run_id]).fetchall()
    return [DecileOut(**r) for r in rows]


@app.get(
    "/v1/runs/{run_id}/profiles",
    tags=["results"],
    summary="Who is in the model's high-uplift group and in its predicted-harm group",
)
def profiles(run_id: uuid.UUID, _: Reader) -> dict[str, Any]:
    with connection() as conn:
        run = _run(conn, run_id)
    if run["profiles"] is None:
        raise ApiError(404, "no_profiles", "This run failed, so it has no profiles.")
    return run["profiles"]


@app.get(
    "/v1/experiments/{experiment_id}/segments",
    response_model=SegmentPage,
    tags=["results"],
    summary="Model-free segment scan, most harmful first",
)
def segment_scan(
    experiment_id: uuid.UUID,
    _: Reader,
    verdict: Annotated[Literal["helps", "hurts", "unclear"] | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=600)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SegmentPage:
    after = _uncursor(cursor, (float, str))
    with connection() as conn:
        if conn.execute("SELECT 1 FROM experiment WHERE id = %s", [experiment_id]).fetchone() is None:
            raise ApiError(404, "experiment_not_found", f"No experiment {experiment_id}.")
        rows = conn.execute(
            "SELECT * FROM segment WHERE experiment_id = %s AND (%s::text IS NULL OR verdict = %s)"
            " AND (%s::float8 IS NULL OR (uplift, definition) > (%s, %s)) ORDER BY uplift, definition LIMIT %s",
            [experiment_id, verdict, verdict, after and after[0], after and after[0], after and after[1], limit + 1],
        ).fetchall()
    page = rows[:limit]
    more = len(rows) > limit
    return SegmentPage(
        items=[SegmentOut(**r) for r in page],
        next_cursor=_cursor([page[-1]["uplift"], page[-1]["definition"]]) if more else None,
    )


@app.get(
    "/v1/runs/{run_id}/scores",
    response_model=ScorePage,
    tags=["results"],
    summary="Scored units, highest predicted uplift first",
)
def scores(
    run_id: uuid.UUID,
    _: Reader,
    split: Annotated[Literal["train", "val", "test"], Query()] = "test",
    cursor: Annotated[str | None, Query(max_length=600)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ScorePage:
    after = _uncursor(cursor, (float, str))
    with connection() as conn:
        _run(conn, run_id)
        rows = conn.execute(
            "SELECT unit_id, split, uplift FROM score WHERE run_id = %s AND split = %s"
            " AND (%s::float8 IS NULL OR (uplift, unit_id) < (%s, %s)) ORDER BY uplift DESC, unit_id DESC LIMIT %s",
            [run_id, split, after and after[0], after and after[0], after and after[1], limit + 1],
        ).fetchall()
    page = rows[:limit]
    more = len(rows) > limit
    return ScorePage(
        items=[ScoreOut(**r) for r in page],
        next_cursor=_cursor([page[-1]["uplift"], page[-1]["unit_id"]]) if more else None,
    )


# ---------------------------------------------------------------- inference and policies


@app.post(
    "/v1/runs/{run_id}/predict",
    response_model=PredictOut,
    tags=["inference"],
    summary="Predicted uplift for up to 1000 units",
)
def predict(run_id: uuid.UUID, body: PredictIn, _: Reader) -> PredictOut:
    import pandas as pd

    from uplift.learners import LEARNERS, featurize

    with connection() as conn:
        run = _run(conn, run_id)
    if run["status"] != "complete":
        raise ApiError(409, "run_failed", "This run failed during training and cannot predict.")
    if not LEARNERS[run["learner"]].estimates_uplift:
        raise ApiError(
            409, "not_an_uplift_model", f"'{run['learner']}' is a baseline; its score is not an uplift estimate."
        )
    try:
        artifact = _model(run["artifact_file"], run["artifact_sha256"])
    except (OSError, ValueError) as exc:
        logger.error("model artifact unavailable: %s", exc)
        raise ApiError(503, "model_unavailable", "This model's artifact cannot be loaded on this server.") from exc
    X, _medians = featurize(pd.DataFrame([u.model_dump() for u in body.units]), artifact["medians"])
    PREDICTED.inc(len(body.units))
    return PredictOut(run_id=run_id, learner=run["learner"], uplift=[float(v) for v in artifact["model"].predict(X)])


@app.post(
    "/v1/policies", response_model=PolicyOut, status_code=201, tags=["policy"],
    summary="Create a budget-constrained targeting policy (idempotent on Idempotency-Key)",
    responses={200: {"model": PolicyOut, "description": "This Idempotency-Key already created this policy; here it is again"}},
)  # fmt: skip
def create_policy(
    body: PolicyIn,
    response: Response,
    _: Admin,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")],
) -> PolicyOut:
    with connection() as conn:
        run = _run(conn, body.run_id)
        if not run["is_champion"]:
            raise ApiError(
                409, "not_champion", "Policies are built from the champion model, the only one that is batch-scored."
            )
        replay = (
            conn.execute("SELECT 1 FROM policy WHERE idempotency_key = %s", [idempotency_key]).fetchone() is not None
        )
        try:
            p = policy.create(
                conn, body.run_id, body.budget, body.cost_per_treatment, body.value_per_outcome, idempotency_key
            )
        except policy.NotScored as exc:
            raise ApiError(
                409, "not_scored", "This model has not been batch-scored yet. Run `uplift score` and retry."
            ) from exc
        except policy.PolicyConflict as exc:
            raise ApiError(409, "idempotency_conflict", str(exc)) from exc
    if replay:
        response.status_code = 200
    else:
        POLICIES.inc()
    return _policy_out(p)


@app.get(
    "/v1/policies/{policy_id}", response_model=PolicyOut, tags=["policy"], summary="A policy and its estimated value"
)
def get_policy(policy_id: uuid.UUID, _: Reader) -> PolicyOut:
    with connection() as conn:
        p = conn.execute("SELECT * FROM policy WHERE id = %s", [policy_id]).fetchone()
    if p is None:
        raise ApiError(404, "policy_not_found", f"No policy {policy_id}.")
    return _policy_out(p)


@app.get(
    "/v1/policies/{policy_id}/export", tags=["policy"], summary="The activation file: unit ids to treat, best first (CSV)",
    response_class=StreamingResponse, responses={200: {"content": {"text/csv": {}}}},
)  # fmt: skip
def export_policy(policy_id: uuid.UUID, _: Admin) -> StreamingResponse:
    with connection() as conn:
        p = conn.execute("SELECT * FROM policy WHERE id = %s", [policy_id]).fetchone()
        if p is None:
            raise ApiError(404, "policy_not_found", f"No policy {policy_id}.")
        rows = conn.execute(policy.EXPORT, [p["run_id"], p["n_targeted"]]).fetchall()

    def lines() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["unit_id", "predicted_uplift"])
        for i, r in enumerate(rows, start=1):
            writer.writerow([r["unit_id"], f"{r['uplift']:.6f}"])
            if i % 2000 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    return StreamingResponse(
        lines(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="policy-{policy_id}.csv"'},
    )
