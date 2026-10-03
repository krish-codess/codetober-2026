"""API contract: every request and response shape. FastAPI renders these into the OpenAPI spec
(docs/api/openapi.json) so documentation is generated from code and cannot drift."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "partial", "insufficient"]
PriceStatus = Literal["ok", "thin", "rejected", "missing"]
SLUG = r"^[a-z0-9-]{1,40}$"


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str
    details: list[dict[str, object]] | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class Freshness(BaseModel):
    last_day: date | None
    age_hours: float | None
    stale: bool = Field(description="true when the newest published day is older than the freshness SLO")


class ServerOut(BaseModel):
    server_id: str
    name: str


class DivisionOut(BaseModel):
    division_id: str
    label: str


class ActivityOut(BaseModel):
    activity_id: str
    label: str


class ItemOut(BaseModel):
    item_id: int
    name: str
    division_id: str


class WorldOut(BaseModel):
    world_id: str
    name: str
    price_source: Literal["snapshots", "trade_history"]
    is_synthetic: bool
    currency: str
    servers: list[ServerOut]
    divisions: list[DivisionOut]
    activities: list[ActivityOut]
    items: list[ItemOut]
    first_day: date | None
    freshness: Freshness


class IndexPoint(BaseModel):
    day: date
    value: float | None = Field(description="index level; null when coverage is insufficient (never extrapolated)")
    coverage: float = Field(ge=0, le=1, description="share of basket weight with an observed price that day")
    status: Status
    vintage: int = Field(ge=1, description="1 = first publication; >1 = revised by late or corrected data")
    revised: bool


class SeriesRef(BaseModel):
    world_id: str
    server_id: str = Field(description="'all' for the cross-server index")
    division_id: str = Field(description="'all' for the headline index")
    series_id: int


class IndexResponse(BaseModel):
    series: SeriesRef
    method_version: str | None
    as_of: datetime | None = Field(description="when set, values as they were published at that instant")
    freshness: Freshness
    points: list[IndexPoint]


class Revision(BaseModel):
    vintage: int
    value: float | None
    coverage: float
    status: Status
    reason: Literal["initial", "late_data", "source_revision", "method_change"]
    input_hash: str
    computed_at: datetime


class RevisionsResponse(BaseModel):
    series: SeriesRef
    day: date
    revisions: list[Revision]


class InflationPoint(BaseModel):
    day: date
    rate: float = Field(description="change over the window, e.g. 0.02 = +2%")
    annualized: float


class InflationResponse(BaseModel):
    series: SeriesRef
    window_days: int
    points: list[InflationPoint]


class InflationCell(BaseModel):
    server_id: str
    division_id: str
    day: date
    rate: float
    annualized: float


class InflationMatrix(BaseModel):
    world_id: str
    window_days: int
    cells: list[InflationCell]


class PatchImpact(BaseModel):
    log_change: float
    robust_z: float


class PatchOut(BaseModel):
    world_id: str
    patch_id: str
    released_at: datetime
    version: str | None
    title: str
    tags: list[str]
    is_major: bool
    source: str
    notes_excerpt: str
    impact: PatchImpact | None = Field(description="headline index move in the 7 days after vs before release")


class Page(BaseModel):
    next_cursor: str | None = Field(description="opaque; pass back as ?cursor= to get the next page")
    limit: int


class PatchPage(BaseModel):
    items: list[PatchOut]
    page: Page


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    world_id: str = Field(pattern=SLUG)
    patch_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    released_at: datetime
    title: str = Field(min_length=1, max_length=300)
    notes: str = Field(default="", max_length=100_000)
    version: str | None = Field(default=None, max_length=40)
    is_major: bool = False


class Attribution(BaseModel):
    patch_id: str
    title: str
    released_at: datetime
    lag_hours: float
    relevance: float
    rank: int


class ShockOut(BaseModel):
    day: date
    log_change: float
    robust_z: float
    direction: Literal["up", "down"]
    persistence: float | None = Field(description="share of the move still present 3 days later; null = not yet known")
    attributions: list[Attribution] = Field(description="empty = unattributed: no patch in the lookback window")


class ShocksResponse(BaseModel):
    series: SeriesRef
    shocks: list[ShockOut]


class WageRate(BaseModel):
    effective_from: date
    isk_per_hour: float


class Yield(BaseModel):
    item_id: int
    name: str
    qty_per_hour: float


class ItemPower(BaseModel):
    item_id: int
    price: float | None
    units_per_hour: float | None
    hours_per_unit: float | None


class PowerPoint(BaseModel):
    day: date
    wage: float | None = Field(description="currency earned per hour (nominal bounty + yields at that day's prices)")
    wage_isk: float
    wage_goods: float | None
    basket_cost: float | None = Field(
        description="cost that day of the basket that cost 1,000,000 at the index reference"
    )
    hours_per_basket: float | None
    real_wage: float | None = Field(description="wage deflated by the server index (reference-period currency)")
    items: list[ItemPower]


class PurchasingPowerResponse(BaseModel):
    world_id: str
    server_id: str
    activity: ActivityOut
    rates: list[WageRate]
    yields: list[Yield]
    items: list[ItemOut]
    points: list[PowerPoint]


class ManipulationOut(BaseModel):
    server_id: str
    item_id: int
    item_name: str
    day: date
    kind: Literal["extreme_listing", "rejected_price", "thin_market_spike"]
    severity: float
    n_obs: int
    thin: bool
    detail: dict[str, object]


class ManipulationPage(BaseModel):
    items: list[ManipulationOut]
    page: Page


class FlowPoint(BaseModel):
    day: date
    sinks: float
    faucets: float | None = Field(description="null when the world has no faucet feed (real EVE)")
    net: float | None = Field(description="faucets - sinks: positive = currency supply growing")


class FlowsResponse(BaseModel):
    world_id: str
    server_id: str
    methods: dict[str, str]
    points: list[FlowPoint]


class HealthCheck(BaseModel):
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, HealthCheck]
    freshness: dict[str, Freshness]
