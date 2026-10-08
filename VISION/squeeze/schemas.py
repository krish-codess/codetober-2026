"""The wire contract. Requests are validated against these at the boundary; responses are built
from them; the API reference is generated from them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Finite = Annotated[float, Field(allow_inf_nan=False)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")]
Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Latency(BaseModel):
    n: int = Field(gt=0, le=10_000_000, description="timed single-image inferences")
    p50: Finite = Field(gt=0, lt=600_000, description="milliseconds")
    p95: Finite = Field(gt=0, lt=600_000)
    p99: Finite = Field(gt=0, lt=600_000)
    mean: Finite = Field(gt=0, lt=600_000)

    @model_validator(mode="after")
    def ordered(self) -> Latency:
        if not self.p50 <= self.p95 <= self.p99:
            raise ValueError("percentiles must satisfy p50 <= p95 <= p99")
        return self


class Accuracy(BaseModel):
    n: int = Field(gt=0, description="evaluation images run on the device")
    top1: Fraction
    agree_host: Fraction = Field(description="share of predictions equal to the build host's")


class Power(BaseModel):
    """Watts from a real sensor. `unit: mW` is accepted and converted; nothing else is guessed."""

    source: str = Field(min_length=1, max_length=200)
    unit: Literal["W", "mW"] = "W"
    idle_w: Finite = Field(ge=0, le=2000)
    load_w: Finite = Field(gt=0, le=2000)
    energy_mj: Finite = Field(ge=0, description="millijoules per inference, net of idle")
    samples: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def to_watts(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("unit") == "mW":
            value = {**value, "unit": "W"}
            for key in ("idle_w", "load_w"):
                if isinstance(value.get(key), int | float):
                    value[key] = value[key] / 1000
        return value


class Device(BaseModel):
    machine: str = Field(min_length=1, max_length=40)
    cpu_model: str = Field(min_length=1, max_length=200)
    cores: int = Field(ge=1, le=4096)
    os: str = Field(max_length=200)
    board: str | None = Field(default=None, max_length=200)


class BenchResult(BaseModel):
    """One model benchmarked on one device, as written by device/edge_bench.py."""

    model_config = ConfigDict(extra="ignore")  # a newer harness may send more than this server knows

    schema_: Literal[1] = Field(alias="schema")
    result_id: str = Field(pattern=r"^[0-9a-f]{16,64}$", description="idempotency key: hash of the result")
    target: Slug
    variant: Slug
    model_sha256: Sha256
    run_id: str | None = Field(default=None, max_length=64)
    runtime: Slug
    runtime_version: str = Field(default="", max_length=60)
    provider: str = Field(default="", max_length=60)
    threads: int = Field(ge=0, le=4096)
    measured_at: datetime = Field(description="ISO 8601 or Unix seconds; a naive time is taken as UTC")
    device: Device
    latency_ms: Latency
    accuracy: Accuracy
    power: Power | None = None
    power_unavailable: str | None = Field(default=None, max_length=300)
    synthetic: bool = Field(default=False, description="true for generated load-test data")

    @field_validator("measured_at")
    @classmethod
    def plausible_time(cls, value: datetime) -> datetime:
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if value > datetime.now(UTC) + timedelta(days=1):
            raise ValueError("measured_at is more than a day in the future; check the device clock")
        if value.year < 2020:
            raise ValueError("measured_at is before 2020; the device clock was probably never set")
        return value


# --- responses --------------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[Any] = []


class Error(BaseModel):
    error: ErrorBody
    request_id: str


class Uploaded(BaseModel):
    status: Literal["created", "duplicate"]
    result_id: str


class BenchSummary(BaseModel):
    runs: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    top1_device: float
    agree_host: float
    load_w: float | None
    idle_w: float | None
    energy_mj: float | None
    power_source: str | None
    power_unavailable: str | None
    cpu_model: str
    threads: int
    last_measured_at: str


class Point(BaseModel):
    name: str
    parent: str | None
    technique: str
    arch: str
    precision: str
    params: int
    size_bytes: int
    top1: float
    top1_lo: float
    top1_hi: float
    n_eval: int
    top1_deploy: float
    parent_agree: float | None
    parent_delta: float | None
    parent_delta_lo: float | None
    parent_delta_hi: float | None
    gate: Literal["pass", "fail"]
    gate_reason: str
    bench: BenchSummary | None = Field(description="null: not measured on this target and runtime yet")
    pareto: bool = Field(description="no other measured, gate-passing variant is both faster and more accurate")
    meets_budget: bool | None = Field(description="p95 within the target's latency budget; null if unknown")


class Tradeoff(BaseModel):
    target: dict[str, Any]
    runtime: str
    runtimes_measured: list[str]
    run: dict[str, Any] | None
    baselines: list[dict[str, Any]]
    points: list[Point]


class ResultRow(BaseModel):
    result_id: str
    received_at: str
    measured_at: str
    target: str
    variant: str
    runtime: str
    threads: int
    cpu_model: str
    board: str | None
    p50_ms: float
    p95_ms: float
    p99_ms: float
    top1_device: float
    agree_host: float
    load_w: float | None
    energy_mj: float | None
    power_source: str | None
    known_model: bool = Field(description="false: no pipeline run with this model has been ingested (yet)")
    synthetic: bool


class ResultPage(BaseModel):
    items: list[ResultRow]
    next_cursor: str | None


class SensitivityRow(BaseModel):
    rank: int
    node: str
    op_type: str
    kl: float = Field(description="mean KL(float || this layer quantized) on the dev split, nats")
    top1_drop: float
    kept_float: bool


class Sensitivity(BaseModel):
    run_id: str
    variant: str
    rows: list[SensitivityRow]


class Package(BaseModel):
    package_id: str = Field(description="sha256 of the archive")
    name: str
    target: str
    run_id: str
    filename: str
    size_bytes: int
    git_commit: str
    dataset_version: str
    hosted: bool = Field(description="false: this server knows the package but does not hold the file")
    variants: list[dict[str, Any]]


class TargetInfo(BaseModel):
    name: str
    label: str
    arch: str
    runtimes: list[str]
    threads: int
    budget_p95_ms: float | None
    power_sensor: str
    results: int = Field(description="benchmark results received for this target")
    packages: int


class QuarantineRow(BaseModel):
    id: int
    received_at: str
    source: str
    errors: list[Any]
    body: str


class QuarantinePage(BaseModel):
    items: list[QuarantineRow]
    next_cursor: str | None
