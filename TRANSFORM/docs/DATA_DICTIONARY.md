# Data dictionary

The currency is ISK (EVE's currency, which the simulated shard also uses) unless stated. Days are
UTC calendar days, and timestamps are `timestamptz` in UTC. "Null" means the column may be NULL and
says what NULL means. The schema is defined in `migrations/` (0001–0004), and every rule below is
a database constraint unless marked *(app)*.

## PostgreSQL: reference data

### `world`
| Column | Type | Null | Meaning |
|---|---|---|---|
| world_id | text PK | no | `eve` (real) or `synthetic` (simulated shard). `^[a-z0-9-]{1,32}$` |
| name | text | no | Display name |
| price_source | text | no | `trade_history` (ESI daily aggregates) or `snapshots` (order-book snapshots) |
| currency | text | no | Always `ISK` |
| is_synthetic | bool | no | True for the simulated shard |

### `server`
| Column | Type | Null | Meaning |
|---|---|---|---|
| server_id | text PK | no | e.g. `eve-the-forge`, `syn-aurora` |
| world_id | text FK→world | no | |
| name | text | no | Unique within a world |
| region_id | bigint | yes | EVE region id (real) or synthetic id; null if unknown |

### `division`
| Column | Type | Null | Meaning |
|---|---|---|---|
| division_id | text PK | no | One of the 7 CPI divisions |
| label | text | no | Display label |
| keywords | text[] | no | Words used to tag patch notes with this division |

### `item`
| Column | Type | Null | Meaning |
|---|---|---|---|
| item_id | bigint PK | no | EVE type id (>0) |
| name | text | no | From ESI `/universe/types` |
| division_id | text FK | no | Curated CPI division (DECISIONS D-07) |
| esi_group, esi_category | text | yes | ESI classification; null if metadata was never fetched |
| volume_m3 | float | yes | Packaged volume, m³ (≥0) |

### `activity`, `activity_rate`, `activity_yield` (labour-hour wages)
| Column | Type | Null | Meaning |
|---|---|---|---|
| activity.activity_id | text PK | no | e.g. `eve-ratting`, `syn-mining` |
| activity.world_id | text FK | no | |
| activity_rate.effective_from | date | no | Rate applies from this day (PK with activity_id) |
| activity_rate.isk_per_hour | float | no | Nominal currency earned per hour (≥0). A bounty patch is a new row |
| activity_yield.qty_per_hour | float | no | Units of `item_id` produced per hour (>0) |

### `index_series`
One row per published series: (world, server or all, division or all).
| Column | Type | Null | Meaning |
|---|---|---|---|
| series_id | serial PK | no | |
| world_id | text FK | no | |
| server_id | text FK | **yes** | **NULL = cross-server** |
| division_id | text FK | **yes** | **NULL = all divisions (headline)** |
Unique on `(world_id, server_id, division_id)` with `NULLS NOT DISTINCT`.

## PostgreSQL: provenance and quality

### `raw_manifest` (append-only)
| Column | Type | Null | Meaning |
|---|---|---|---|
| sha256 | char(64) PK | no | SHA-256 of the stored envelope; also part of the file name |
| source, kind, day, key | text/date | no | Location in the raw store (`<source>/<kind>/day=<day>/<key>__<sha>.json.gz`) |
| bytes | bigint | no | Compressed size (>0) |
| fetched_at | timestamptz | no | When we received it |
| observed_at | timestamptz | no | When the market state it describes was observed. A late payload has `fetched_at ≫ observed_at` |

### `ingest_run`
One row per ingestion job execution. `status` ∈ running/ok/degraded/failed:
`degraded` = some requests failed, `failed` = all failed.
`requested`/`stored`/`failed` are counts, and `detail` holds the first 20 failures as JSON.

### `data_quality_daily`
| Column | Type | Null | Meaning |
|---|---|---|---|
| world_id, day | PK | no | One row per processed (world, day) partition |
| n_payloads | int | no | Raw payloads read for the day |
| n_late_payloads | int | no | Payloads that arrived more than 12 h after their observation time |
| n_rows / n_valid / n_quarantined | bigint | no | Rows seen / accepted / quarantined. `n_valid + n_quarantined = n_rows` |
| reasons | jsonb | no | Quarantine counts by reason, plus `_gaps` (failed fetches), `_coerced`, `_rejected` |
| raw_fingerprint | char(64) | no | Fingerprint of the inputs the partition was computed from. A mismatch with the raw store means it must be recomputed |

## PostgreSQL: published values (append-only, vintaged)

### `item_price_daily` (view `item_price_current` = latest vintage, plus `revised`)
| Column | Type | Null | Meaning |
|---|---|---|---|
| server_id, item_id, day, vintage | PK | no | `vintage` = 1 on first publication, +1 per change |
| price | float | **yes** | Robust daily price, ISK per unit. **NULL unless status = ok** (constraint) |
| volume | float | yes | Units traded that day: inferred fills (snapshots) or ESI volume (history) |
| n_obs | int | no | Snapshots with a price (snapshots) or ESI order count (history) |
| status | text | no | `ok`, `thin` (listings seen but never ≥3), `rejected` (failed acceptance), `missing` (nothing observed) |
| method | text | no | `lowq_ask_consensus_v1` or `vwap_consensus_v1` |
| input_hash | char(16) | no | Prefix of the partition's input fingerprint |
| reason | text | no | `initial`, `late_data`, `source_revision` or `method_change` |
| computed_at | timestamptz | no | Publication time; `as_of` queries filter on it |

### `basket_period`, `basket_item`, `basket_link` (append-only, frozen at creation)
| Column | Type | Null | Meaning |
|---|---|---|---|
| period_id | text | no | `YYYYQn`, the quarter the basket is valid for |
| valid_from/valid_to | date | no | Validity window |
| ref_from/ref_to | date | no | Reference period for weights (must end before `valid_from`) |
| link_from/link_to | date | no | Window whose median prices are the base prices |
| basket_item.weight | float | no | Share of world expenditure (0,1], capped within server; sums to 1 per world-period |
| basket_item.base_price | float | no | p₀, ISK per unit (>0) |
| basket_item.expenditure | float | no | Σ price × volume over the reference period, ISK |
| basket_item.quantity | float | no | Fixed Laspeyres quantity: weight × 1,000,000 / p₀ (units) |
| basket_link.link_value | float | no | Index level L of the series at this period's link point (>0) |

### `index_value` (view `index_value_current`)
| Column | Type | Null | Meaning |
|---|---|---|---|
| series_id, day, vintage | PK | no | |
| value | float | **yes** | Index level (reference = 100). **NULL iff status = insufficient** (constraint) |
| coverage | float | no | Observed basket weight / total basket weight, in [0,1] |
| n_items | int | no | Basket cells with an observed price that day |
| status | text | no | `ok` (coverage ≥ 0.9), `partial` (≥ 0.5), `insufficient` |
| period_id | text | no | Basket period used |
| method_version | text | no | `gs-1.0` |
| input_hash | char(16) | no | Fingerprint of the day's price vector |
| reason, computed_at | | no | As for prices |

## PostgreSQL: patches and analytics (recomputable)

### `patch_event`
| Column | Type | Null | Meaning |
|---|---|---|---|
| world_id, patch_id | PK | no | Real: `YYYY-MM-DD.N` per dated deployment, or `exp-<name>` for expansions. Synthetic: `s1.NN` |
| released_at | timestamptz | no | Deployment time. 11:00 UTC on the date for real EVE (DECISIONS D-22) |
| version | text | yes | Release version, e.g. `24.01`; null when unknown |
| title, notes | text | no | `notes` is the plain text of the patch section (≤100k chars) |
| tags | text[] | no | Divisions mentioned, most-mentioned first |
| is_major | bool | no | Expansion or a release with economic changes |
| source | text | no | `rss`, `api` (posted through `POST /v1/patches`) or `synthetic` |

### `inflation_rate`
`(series_id, window_days ∈ {7,30,90,365}, day)` → `rate` (fraction, e.g. 0.02 = +2%), `annualized` (fraction).

### `shock` / `shock_attribution` / `patch_impact`
| Column | Meaning |
|---|---|
| shock.log_change | ln(I_t / I_{t−1}) |
| shock.robust_z | Standardised against the previous 60 days |
| shock.direction | up/down |
| shock.persistence | Share of the move retained 3 days later; **NULL = not yet known** (migration 0002) |
| shock_attribution.lag_hours | Hours from patch release to noon of the shock day (≥0) |
| shock_attribution.relevance | [0,1], topic match × recency |
| shock_attribution.rank | 1 = most likely |
| patch_impact.log_change / robust_z | Move of the 7-day median level after vs before release, and how unusual it is |

### `manipulation_event`
`kind` ∈ `extreme_listing` (a listing ≥5 robust deviations from its book), `rejected_price` (a daily
price refused by consensus or Hampel; migration 0004) and `thin_market_spike` (a published price in a
thin market that moved ≥75% from its trailing median). `severity` is a robust z, `thin` means fewer
than 10 listings or trades, and `detail` is JSON evidence (for example the naive mean a mean
estimator would have published).

### `money_flow`
`(server_id, day, kind)` → `amount` in ISK per day.
* `kind` ∈ `sink_sales_tax`, `sink_broker_fee`, `faucet_bounty`.
* `method` says how the amount was estimated.
* **There are no `faucet_bounty` rows for EVE:** no public feed exists, and the API reports that as
  unknown rather than 0.

### `api_key`, `idempotency_key`
API keys are stored as SHA-256 only. `scopes` ⊆ {`patches:write`}, and `revoked_at` set means
revoked. Idempotency records hold `(key_id, idempotency_key) → request_hash, status_code, response`.

## Lake (Parquet, `<GS_DATA_DIR>/lake/<dataset>/w=<world>/dt=<day>/part-0.parquet`)

| Dataset | Grain | Key columns |
|---|---|---|
| `listings` | one valid order per snapshot | server_id, snapshot_ts, item_id, order_id, is_buy, price (ISK), volume_remain, volume_total, issued, duration (days), raw_sha |
| `quarantine` | one rejected raw row | reason, raw_row (JSON, ≤2 kB), raw_sha |
| `gaps` | one failed fetch | server_id, item_id, snapshot_ts, status (HTTP status; −1 = unparseable body) |
| `history` | one ESI history row (latest fetch wins) | server_id, item_id, day, average, highest, lowest, volume, order_count, fetched_at |
| `fills` | inferred trades between consecutive snapshots | fill_qty (units), fill_value (ISK), new_listing_value (ISK) |
| `faucets` | daily faucet payouts (simulated) | ref_type, amount (ISK) |
| `daily_prices` | (server, item, day) | raw_price, price (accepted), status, reject_reason, cross_ref, hampel_ref, robust_z, and snapshot diagnostics (n_snapshots, max_listings, n_extreme, max_z, naive_mean) |
| `synthetic_truth` | generator ground truth (tests only; the pipeline never reads it) | fair prices, true fills, injected manipulations, faucets, patches |

## Raw store (`<GS_DATA_DIR>/raw/<source>/<kind>/day=<day>/<key>__<sha20>.json.gz`)
A gzip JSON envelope `{source, kind, key, fetched_at, observed_at, meta, body}`, where `body` is the
verbatim response.
* **Order bundles:** `body` is a JSON list of `{type_id, page, status, body}`, one per item request,
  with each page body verbatim. `status ≠ 200` means a failed fetch (recorded as a gap).
