# API reference: GOLD STANDARD API 1.0.0

Generated from `docs/api/openapi.json`, which is generated from the FastAPI code by `goldstandard openapi`. CI fails if either file is out of date. Interactive version: `/docs` on a running API.

Consumer price index for video game economies: chain-linked Laspeyres indices per server, robust prices, patch shock attribution and purchasing power in labour-hours. All values are published append-only: a revised value is a new vintage, never an overwrite.

**Errors** always have the shape `{"error": {"code", "message", "details", "request_id"}}`. Every response carries `X-Request-ID`. GET responses under `/v1` carry an `ETag` and honour `If-None-Match` (304).

## `GET /health`

Liveness: the process is up

| Status | Meaning |
|---|---|
| 200 | Successful Response - object |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /health/ready`

Readiness: exercises PostgreSQL and checks data freshness

| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | database unreachable |

## `GET /metrics`

Prometheus metrics

| Status | Meaning |
|---|---|
| 200 | Successful Response |
| 400 | Bad Request |
| 404 | Not Found |
| 422 | Unprocessable Entity |
| 503 | Service Unavailable |

## `GET /v1/index`

Index series (latest vintage, or as of a time)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | no | string `^[a-z0-9-]{1,40}$` | server id, or 'all' |
| `division` | query | no | string `^[a-z_]{1,32}$` | division id, or 'all' |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |
| `as_of` | query | no | string (date-time) or null | ISO time: reproduce what was published then |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `IndexResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/index/revisions`

Every published vintage of one index value (audit trail)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `day` | query | yes | string (date) |  |
| `server` | query | no | string `^[a-z0-9-]{1,40}$` | server id, or 'all' |
| `division` | query | no | string `^[a-z_]{1,32}$` | division id, or 'all' |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `RevisionsResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/inflation`

Inflation rate over a rolling window

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | no | string `^[a-z0-9-]{1,40}$` | server id, or 'all' |
| `division` | query | no | string `^[a-z_]{1,32}$` | division id, or 'all' |
| `window` | query | no | integer | 7, 30, 90 or 365 days |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `InflationResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/inflation/matrix`

Latest inflation rate for every server x division

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `window` | query | no | integer |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `InflationMatrix` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/manipulation`

Manipulation / thin-market events for a server, newest first (keyset-paginated)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `kind` | query | no | string `^(extreme_listing|rejected_price|thin_market_spike)$` or null |  |
| `min_severity` | query | no | number |  |
| `cursor` | query | no | string or null |  |
| `limit` | query | no | integer |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `ManipulationPage` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/money-flows`

Currency sinks and faucets per day

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `FlowsResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/patches`

Patch timeline (keyset-paginated)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |
| `major_only` | query | no | boolean |  |
| `cursor` | query | no | string or null |  |
| `limit` | query | no | integer |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `PatchPage` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `POST /v1/patches`

Record a patch note (auth: patches:write; Idempotency-Key required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `authorization` | header | no | string or null |  |
| `idempotency-key` | header | no | string or null |  |

Request body: `PatchIn`

| Status | Meaning |
|---|---|
| 200 | idempotent replay of an earlier identical request |
| 201 | Successful Response - `PatchOut` |
| 400 | Bad Request - `ErrorResponse` |
| 401 | missing/invalid token |
| 403 | missing scope |
| 404 | Not Found - `ErrorResponse` |
| 409 | idempotency key reused with a different body, or patch exists |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/purchasing-power`

What an hour of an activity buys over time (labour-hours)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `activity` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `items` | query | no | string `^\d{1,12}(,\d{1,12}){0,9}$` or null | up to 10 item ids |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `PurchasingPowerResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/shocks`

Detected shocks with patch attribution

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `world` | query | yes | string `^[a-z0-9-]{1,40}$` |  |
| `server` | query | no | string `^[a-z0-9-]{1,40}$` | server id, or 'all' |
| `division` | query | no | string `^[a-z_]{1,32}$` | division id, or 'all' |
| `from` | query | no | string (date) or null |  |
| `to` | query | no | string (date) or null |  |

| Status | Meaning |
|---|---|
| 200 | Successful Response - `ShocksResponse` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## `GET /v1/worlds`

Worlds, servers, divisions, activities

| Status | Meaning |
|---|---|
| 200 | Successful Response - array of `WorldOut` |
| 400 | Bad Request - `ErrorResponse` |
| 404 | Not Found - `ErrorResponse` |
| 422 | Unprocessable Entity - `ErrorResponse` |
| 503 | Service Unavailable - `ErrorResponse` |

## Schemas

### `ActivityOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `activity_id` | string | yes |  |
| `label` | string | yes |  |

### `Attribution`

| Field | Type | Required | Description |
|---|---|---|---|
| `patch_id` | string | yes |  |
| `title` | string | yes |  |
| `released_at` | string (date-time) | yes |  |
| `lag_hours` | number | yes |  |
| `relevance` | number | yes |  |
| `rank` | integer | yes |  |

### `DivisionOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `division_id` | string | yes |  |
| `label` | string | yes |  |

### `ErrorBody`

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | yes |  |
| `message` | string | yes |  |
| `details` | array of object or null | no |  |
| `request_id` | string | yes |  |

### `ErrorResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | `ErrorBody` | yes |  |

### `FlowPoint`

| Field | Type | Required | Description |
|---|---|---|---|
| `day` | string (date) | yes |  |
| `sinks` | number | yes |  |
| `faucets` | number or null | yes | null when the world has no faucet feed (real EVE) |
| `net` | number or null | yes | faucets - sinks: positive = currency supply growing |

### `FlowsResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `server_id` | string | yes |  |
| `methods` | object | yes |  |
| `points` | array of `FlowPoint` | yes |  |

### `Freshness`

| Field | Type | Required | Description |
|---|---|---|---|
| `last_day` | string (date) or null | yes | newest day with published prices |
| `age_hours` | number or null | yes |  |
| `stale` | boolean | yes | true when the newest published day is older than the freshness SLO |

### `IndexPoint`

| Field | Type | Required | Description |
|---|---|---|---|
| `day` | string (date) | yes |  |
| `value` | number or null | yes | index level; null when coverage is insufficient (never extrapolated) |
| `coverage` | number | yes | share of basket weight with an observed price that day |
| `status` | string (`ok`, `partial`, `insufficient`) | yes |  |
| `vintage` | integer | yes | 1 = first publication; >1 = revised by late or corrected data |
| `revised` | boolean | yes |  |

### `IndexResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `series` | `SeriesRef` | yes |  |
| `method_version` | string or null | yes |  |
| `as_of` | string (date-time) or null | yes | when set, values as they were published at that instant |
| `freshness` | `Freshness` | yes |  |
| `points` | array of `IndexPoint` | yes |  |

### `InflationCell`

| Field | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | yes |  |
| `division_id` | string | yes |  |
| `day` | string (date) | yes |  |
| `rate` | number | yes |  |
| `annualized` | number | yes |  |

### `InflationMatrix`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `window_days` | integer | yes |  |
| `cells` | array of `InflationCell` | yes |  |

### `InflationPoint`

| Field | Type | Required | Description |
|---|---|---|---|
| `day` | string (date) | yes |  |
| `rate` | number | yes | change over the window, e.g. 0.02 = +2% |
| `annualized` | number | yes |  |

### `InflationResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `series` | `SeriesRef` | yes |  |
| `window_days` | integer | yes |  |
| `points` | array of `InflationPoint` | yes |  |

### `ItemOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `item_id` | integer | yes |  |
| `name` | string | yes |  |
| `division_id` | string | yes |  |

### `ItemPower`

| Field | Type | Required | Description |
|---|---|---|---|
| `item_id` | integer | yes |  |
| `price` | number or null | yes |  |
| `units_per_hour` | number or null | yes |  |
| `hours_per_unit` | number or null | yes |  |

### `ManipulationOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | yes |  |
| `item_id` | integer | yes |  |
| `item_name` | string | yes |  |
| `day` | string (date) | yes |  |
| `kind` | string (`extreme_listing`, `rejected_price`, `thin_market_spike`) | yes |  |
| `severity` | number | yes |  |
| `n_obs` | integer | yes |  |
| `thin` | boolean | yes |  |
| `detail` | object | yes |  |

### `ManipulationPage`

| Field | Type | Required | Description |
|---|---|---|---|
| `items` | array of `ManipulationOut` | yes |  |
| `page` | `Page` | yes |  |

### `Page`

| Field | Type | Required | Description |
|---|---|---|---|
| `next_cursor` | string or null | yes | opaque; pass back as ?cursor= to get the next page |
| `limit` | integer | yes |  |

### `PatchImpact`

| Field | Type | Required | Description |
|---|---|---|---|
| `log_change` | number | yes |  |
| `robust_z` | number | yes |  |

### `PatchIn`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string `^[a-z0-9-]{1,40}$` | yes |  |
| `patch_id` | string `^[A-Za-z0-9._-]{1,64}$` | yes |  |
| `released_at` | string (date-time) | yes |  |
| `title` | string | yes |  |
| `notes` | string | no |  |
| `version` | string or null | no |  |
| `is_major` | boolean | no |  |

### `PatchOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `patch_id` | string | yes |  |
| `released_at` | string (date-time) | yes |  |
| `version` | string or null | yes |  |
| `title` | string | yes |  |
| `tags` | array of string | yes |  |
| `is_major` | boolean | yes |  |
| `source` | string | yes |  |
| `notes_excerpt` | string | yes |  |
| `impact` | `PatchImpact` or null | yes | headline index move in the 7 days after vs before release |

### `PatchPage`

| Field | Type | Required | Description |
|---|---|---|---|
| `items` | array of `PatchOut` | yes |  |
| `page` | `Page` | yes |  |

### `PowerPoint`

| Field | Type | Required | Description |
|---|---|---|---|
| `day` | string (date) | yes |  |
| `wage` | number or null | yes | currency earned per hour (nominal bounty + yields at that day's prices) |
| `wage_isk` | number | yes |  |
| `wage_goods` | number or null | yes |  |
| `basket_cost` | number or null | yes | cost that day of the basket that cost 1,000,000,000 at the index reference |
| `hours_per_basket` | number or null | yes |  |
| `real_wage` | number or null | yes | wage deflated by the server index (reference-period currency) |
| `items` | array of `ItemPower` | yes |  |

### `PurchasingPowerResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `server_id` | string | yes |  |
| `activity` | `ActivityOut` | yes |  |
| `rates` | array of `WageRate` | yes |  |
| `yields` | array of `Yield` | yes |  |
| `items` | array of `ItemOut` | yes |  |
| `points` | array of `PowerPoint` | yes |  |

### `Revision`

| Field | Type | Required | Description |
|---|---|---|---|
| `vintage` | integer | yes |  |
| `value` | number or null | yes |  |
| `coverage` | number | yes |  |
| `status` | string (`ok`, `partial`, `insufficient`) | yes |  |
| `reason` | string (`initial`, `late_data`, `source_revision`, `method_change`) | yes |  |
| `input_hash` | string | yes |  |
| `computed_at` | string (date-time) | yes |  |

### `RevisionsResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `series` | `SeriesRef` | yes |  |
| `day` | string (date) | yes |  |
| `revisions` | array of `Revision` | yes |  |

### `SeriesRef`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `server_id` | string | yes | 'all' for the cross-server index |
| `division_id` | string | yes | 'all' for the headline index |
| `series_id` | integer | yes |  |

### `ServerOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | yes |  |
| `name` | string | yes |  |

### `ShockOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `day` | string (date) | yes |  |
| `log_change` | number | yes |  |
| `robust_z` | number | yes |  |
| `direction` | string (`up`, `down`) | yes |  |
| `persistence` | number or null | yes | share of the move still present 3 days later; null = not yet known |
| `attributions` | array of `Attribution` | yes | empty = unattributed: no patch in the lookback window |

### `ShocksResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `series` | `SeriesRef` | yes |  |
| `shocks` | array of `ShockOut` | yes |  |

### `WageRate`

| Field | Type | Required | Description |
|---|---|---|---|
| `effective_from` | string (date) | yes |  |
| `isk_per_hour` | number | yes |  |

### `WorldOut`

| Field | Type | Required | Description |
|---|---|---|---|
| `world_id` | string | yes |  |
| `name` | string | yes |  |
| `price_source` | string (`snapshots`, `trade_history`) | yes |  |
| `is_synthetic` | boolean | yes |  |
| `currency` | string | yes |  |
| `servers` | array of `ServerOut` | yes |  |
| `divisions` | array of `DivisionOut` | yes |  |
| `activities` | array of `ActivityOut` | yes |  |
| `items` | array of `ItemOut` | yes |  |
| `first_day` | string (date) or null | yes |  |
| `freshness` | `Freshness` | yes |  |

### `Yield`

| Field | Type | Required | Description |
|---|---|---|---|
| `item_id` | integer | yes |  |
| `name` | string | yes |  |
| `qty_per_hour` | number | yes |  |

