# Data dictionary

## Source tables (the pipeline's input; generated, or ingested from CSV)

Defined in `src/tydlc/subjects/jaffle/__init__.py` (`SCHEMA`). Constraints come from
jaffle's dbt tests; types from `docs/profile.md`.

| Table | Column | Type | Null | Constraint | Meaning |
|---|---|---|---|---|---|
| raw_customers | id | integer | no | unique | Customer key |
| | first_name | text | yes | | Free text, PII |
| | last_name | text | yes | | Free text (an initial in the seeds), PII |
| raw_orders | id | integer | no | unique | Order key |
| | user_id | integer | no | → raw_customers.id | The ordering customer |
| | order_date | date | yes | | Date the order was placed (UTC) |
| | status | text | no | one of placed, shipped, completed, return_pending, returned | |
| raw_payments | id | integer | no | unique | Payment key |
| | order_id | integer | no | → raw_orders.id | The order paid for |
| | payment_method | text | no | one of credit_card, coupon, bank_transfer, gift_card | |
| | amount | integer | no | | **Cents** (AUD). May be zero or negative |

## Pipeline output (jaffle's models, as views)

| Model | Column | Unit / meaning |
|---|---|---|
| stg_customers | customer_id, first_name, last_name | Renamed pass-through |
| stg_orders | order_id, customer_id, order_date, status | Renamed pass-through |
| stg_payments | payment_id, order_id, payment_method | Renamed pass-through |
| | amount | **Dollars** = cents / 100. DOUBLE on DuckDB, truncated INTEGER on PostgreSQL (finding 2) |
| orders | order_id, customer_id, order_date, status | One row per raw order |
| | credit_card_amount, coupon_amount, bank_transfer_amount, gift_card_amount | Dollars paid by that method. NULL when the order has no payments (finding 1) |
| | amount | Dollars, all methods. NULL when the order has no payments |
| customers | customer_id, first_name, last_name | One row per raw customer |
| | first_order, most_recent_order | Dates; NULL if the customer never ordered |
| | number_of_orders | Count; NULL (not 0) if the customer never ordered |
| | customer_lifetime_value | Dollars across all the customer's payments; NULL if none |

## Results database (PostgreSQL, `src/tydlc/migrations/`)

### runs: one execution of the suite

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | bigint identity | no | Primary key; newer runs have larger ids |
| idempotency_key | text | no | Unique. The caller's key; replays return the same run |
| subject | text | no | Pipeline under test (`jaffle`) |
| engine | text | no | `duckdb` or `postgres` (CHECK) |
| seed | bigint | no | Hypothesis seed; same seed, same datasets |
| max_examples | integer | no | Datasets requested, > 0 |
| status | text | no | `running`, `completed` or `failed` (CHECK) |
| git_sha | text | yes | Commit under test, from `GIT_SHA` |
| started_at / finished_at | timestamptz | no / yes | `finished_at` is NULL exactly while running |
| examples | integer | yes | Datasets actually generated |
| rows_generated | bigint | yes | Input rows across all datasets |
| duration_ms | integer | yes | Wall time, milliseconds |
| candidates | integer | yes | Invariant candidates inferred from seed data |
| candidates_held | integer | yes | Candidates not falsified; 0 ≤ held ≤ candidates |
| gate_ok | boolean | yes | CI gate verdict; required once completed |
| error | text | yes | Required once failed |

### properties: every property ever evaluated

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | bigint identity | no | Primary key |
| subject, name | text | no | Unique together |
| source | text | no | `declared` (hand-written) or `discovered` (inferred) |
| description | text | no | One sentence, shown in the catalog |

### property_results: one property in one run

Primary key `(run_id, property_id)`; both are foreign keys.

| Column | Type | Null | Meaning |
|---|---|---|---|
| passed | integer | no | Datasets on which it held |
| failed | integer | no | Datasets that violated it |
| vacuous | integer | no | Datasets with nothing to judge (e.g. an empty table) |
| status | text | no | `falsified` iff failed > 0 (CHECK); else `held`, or `vacuous` if never exercised |
| confidence | real | no | 0..1. `1 − 3/passed`, 0 if falsified (CHECK) |
| known_bug | text | yes | Why it is expected to fail, if baselined |

### failures: the minimal counterexample for a falsified result

| Column | Type | Null | Meaning |
|---|---|---|---|
| id | bigint identity | no | Primary key; pagination key |
| run_id, property_id | bigint | no | Unique together; FK to property_results, cascades |
| minimal_dataset | json | no | `{table: [row, ...]}` in schema order. Dates as ISO strings |
| minimal_rows | integer | no | Total rows in it |
| shrunk | boolean | no | False if the shrink search could not rediscover the failure and this is the unshrunk first failure |
| shrink_calls | integer | no | Pipeline evaluations spent shrinking |
| shrink_ms | integer | no | Milliseconds spent shrinking |

Index `failures_property_id_idx (property_id, id DESC)`: see `explain_analyze.md`.

### schema_migrations

`version` (integer, PK), `applied_at`. Maintained by `tydlc migrate`.

## Files under `TYDLC_DATA_DIR`

| Path | Content |
|---|---|
| `<subject>/staged/<table>.parquet` | Rows that passed boundary validation, typed |
| `<subject>/quarantine/<table>.parquet` | Rejected rows, raw text, plus `_line` and `_reason` |
| `<subject>/manifest.json` | SHA-256 of each raw input file |
| `runs/<run_key>/outcomes.parquet` | `example, property, outcome (pass/fail/vacuous), input_rows`; one row per (example, property), sorted by property |
| `runs/<run_key>/report.json` | The full report, including every minimal dataset |
