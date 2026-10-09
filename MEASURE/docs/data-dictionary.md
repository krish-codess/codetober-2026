# Data dictionary

Every table and meaningful column, in the order data flows. All timestamps are UTC. "Nullable: no"
on a Parquet or DuckDB table means a test fails the build if a null appears; on a PostgreSQL table it
is a `NOT NULL` constraint.

## 1. Raw files (immutable)

`$WRAPPED_DATA_DIR/raw/YYYY-MM-DD-H.json.gz`: one gzip of newline-delimited JSON per hour, exactly as
GH Archive publishes it (the hour is not zero-padded). Never modified, moved or deleted by the system.
Everything downstream can be rebuilt from this directory.

## 2. Bronze (Parquet, one file per ingest batch, zstd, 122,880-row groups, sorted by `created_at`)

### `bronze/batch_NNNNNN.parquet`: every line that passed validation

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `event_id` | BIGINT | no | Upstream event id. **Not unique here**: redeliveries are kept. |
| `event_type` | VARCHAR | no | Upstream type, e.g. `PushEvent`. Unknown types are accepted. |
| `actor_id` | BIGINT | no | Upstream account id. The identity of a user. |
| `actor_login` | VARCHAR | yes | Login at the time of the event. Changes over time. |
| `repo_id` | BIGINT | yes | Repository the event happened on. |
| `repo_name` | VARCHAR | yes | `owner/name` at the time of the event. |
| `action` | VARCHAR | yes | `payload.action` (`opened`, `closed`, `started`, ...). Present for some types only. |
| `ref_type` | VARCHAR | yes | `payload.ref_type` for create/delete events (`repository`, `branch`, `tag`). |
| `created_at` | TIMESTAMP | no | When the event happened. Any offset in the source is converted to UTC. |
| `event_date` | DATE | no | UTC date of `created_at`. |
| `source_file` | VARCHAR | no | Raw file the line came from. |
| `batch_id` | INTEGER | no | Ingest batch, increasing. The watermark for incremental models. |
| `ingested_at` | TIMESTAMP | no | When the batch was ingested. |

### `quarantine/batch_NNNNNN.parquet`: every line that failed validation

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `source_file` | VARCHAR | no | Raw file the line came from. |
| `reject_reason` | VARCHAR | no | One of `malformed_json`, `missing_event_id`, `missing_actor`, `missing_type`, `unparseable_timestamp`, `timestamp_out_of_range` (before 2008 or more than a day after ingest). First failing rule wins, in that order. |
| `raw_line` | VARCHAR | yes | First 4,000 characters of the line, as received. |
| `raw_length` | BIGINT | yes | Full length of the line in characters. |
| `batch_id`, `ingested_at` | | no | As in bronze. |

### `manifest/batch_NNNNNN.parquet`: one row per raw file per ingest attempt

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `source_file` | VARCHAR | no | File name. With `size_bytes`, the identity of a raw file. |
| `size_bytes` | BIGINT | no | Size in bytes. A file re-downloaded at a different size is a new file. |
| `status` | VARCHAR | no | `ingested`, or `rejected` when the file could not be read at all. |
| `error` | VARCHAR | yes | Why a rejected file was rejected. |
| `lines_read` | BIGINT | no | Lines in the file. Always `lines_accepted + lines_quarantined`. |
| `lines_accepted` | BIGINT | no | Lines written to bronze. |
| `lines_quarantined` | BIGINT | no | Lines written to the quarantine. |
| `batch_id`, `ingested_at` | | no | The manifest file is written last; its existence commits the batch. |

## 3. Warehouse (DuckDB, built by dbt)

### Staging

**`stg_gharchive__events`** (view): bronze with product names. `user_id` = `actor_id`,
`occurred_at` = `created_at`, `activity_date` = `event_date`, plus:

| Column | Meaning |
|---|---|
| `activity` | What the event means to the product: `push`, `pr_opened` (PullRequestEvent, action opened), `review` (review or review comment), `issue_opened`, `comment` (issue or commit comment), `star` (WatchEvent), `fork`, `release`, `repo_created` (CreateEvent of a repository), or `other`. Never null. |

### Intermediate

**`int_events__deduped`** (incremental): one row per `event_id` (unique), all years. Same columns as
staging. An incremental run rebuilds every UTC day touched by a newer batch.

**`int_user_days`** (incremental): one row per (`user_id`, `activity_date`), all years.

| Column | Unit | Meaning |
|---|---|---|
| `events` | events | All events that day. At least 1. |
| `pushes`, `prs_opened`, `reviews`, `issues_opened`, `comments`, `stars`, `forks`, `releases`, `repos_created` | events | Events of that activity that day. |
| `max_batch_id` | | Newest batch that contributed. The watermark. |

**`int_users`**: one row per account active in the Wrapped year.

| Column | Meaning |
|---|---|
| `login` | Most recent login in the year. Never null (falls back to `user-<id>`). |
| `events` | Events in the year. |
| `max_daily_events` | Most events on any one UTC day. |
| `is_labelled_bot` | Login ends in `[bot]`. |
| `is_automated` | `is_labelled_bot`, or `max_daily_events` over 3,000. Automated accounts are excluded from every mart. |

**`int_user_streaks`**: `longest_streak_days` (days, 1 to 366), `streak_started_on`, `streak_ended_on`
(dates). Clipped to the year. Earliest streak on a tie.

**`int_user_favourites`**: `distinct_repos` (count), `top_repo_name` and `top_repo_events` (nullable:
events can lack a repository), `peak_hour_utc` (0 to 23) and `peak_hour_events`, `first_event_at`,
`last_event_at`, `first_activity`, `first_repo_name` (nullable).

### Marts

**`mart_user_year`**: one row per person. Primary key `user_id`. Everything a card can say.

| Column | Type | Unit | Nullable | Meaning |
|---|---|---|---|---|
| `user_id` | BIGINT | | no | Account id. |
| `login` | VARCHAR | | no | Most recent login in the year. |
| `events` | BIGINT | events | no | Events in the year, deduplicated. At least 1. |
| `active_days` | BIGINT | days | no | Distinct UTC days with an event. 1 to 366. |
| `longest_streak_days` | BIGINT | days | no | Longest run of consecutive active days. |
| `streak_started_on`, `streak_ended_on` | DATE | | no | That run's first and last day. |
| `pushes` ... `repos_created` | BIGINT | events | no | Yearly totals per activity. Zero when none. |
| `distinct_repos` | BIGINT | repositories | no | Repositories with at least one event. Can be 0. |
| `top_repo_name` | VARCHAR | | yes | Repository with the most events; lowest id on a tie. |
| `top_repo_events` | BIGINT | events | yes | Events on it. |
| `peak_hour_utc` | BIGINT | hour 0-23 | no | Hour of day with the most events; earliest on a tie. |
| `peak_hour_events` | BIGINT | events | no | Events in that hour of day, across the year. |
| `busiest_date` | DATE | | no | Day with the most events; earliest on a tie. |
| `busiest_day_events` | BIGINT | events | no | Events on it. |
| `busiest_month` | BIGINT | month 1-12 | no | Month with the most events; earliest on a tie. |
| `busiest_month_events` | BIGINT | events | no | Events in it. |
| `weekend_events` | BIGINT | events | no | Events on a Saturday or Sunday (UTC). |
| `weekend_share` | DOUBLE | fraction 0-1 | yes | `weekend_events / events`. Null under 20 events. |
| `first_event_at`, `last_event_at` | TIMESTAMP | | no | First and last event of the year. |
| `first_activity` | VARCHAR | | no | Activity of the first event. |
| `first_repo_name` | VARCHAR | | yes | Repository of the first event. |

**`mart_metric_values`**: one row per (`user_id`, `metric`) with a non-zero `value` (DOUBLE). The 14
rankable metrics: `events`, `active_days`, `longest_streak_days`, `busiest_day_events`,
`distinct_repos`, `pushes`, `prs_opened`, `reviews`, `issues_opened`, `comments`, `stars`, `forks`,
`releases`, `weekend_share`.

**`mart_metric_cutpoints`**: the published thresholds. Key (`metric`, `cut_value`).

| Column | Type | Meaning |
|---|---|---|
| `cut_value` | DOUBLE | Threshold, proposed by the quantile sketch. Never leaves the warehouse. |
| `users_in_bucket` | BIGINT | Users from this threshold up to the next. Exact. |
| `users_at_or_above` | BIGINT | Users with a value >= `cut_value`. Exact. Always at least k. |
| `population` | BIGINT | Users the metric is ranked among: everyone, or for `weekend_share` those it is defined for. |
| `top_fraction` | DOUBLE | `users_at_or_above / population`. In (0, 1]. |

**`mart_user_ranks`**: key (`user_id`, `metric`). The highest published threshold the user clears,
with that threshold's `users_at_or_above`, `population` and `top_fraction`. No row means the user is
below the lowest published threshold for that metric.

**`mart_population`**: one row. `year`, `users` (people), `events`, `pushes`, `prs_opened`, `reviews`,
`stars`, `active_days` (sums over people), `automated_accounts`, `automated_events`.

## 4. Payload run (`payloads/<run_id>/`)

`payloads.parquet` (zstd, 20,000-row groups, sorted by `user_id`): `user_id`, `login`, `tier`,
`archetype`, `card_types` (list, in story order), `payload` (the JSON below, as text).
`run.json`: run id, source fingerprint, counts per tier, duration, and the superlative distribution.

### Payload JSON

| Field | Meaning |
|---|---|
| `version` | Catalogue version that wrote it. |
| `year`, `user.id`, `user.login` | Whose story, for which year. |
| `tier` | `full` (20+ events on 3+ days), `minimal` (under 5 events), otherwise `light`. |
| `archetype` | Label derived from the user's strongest comparison. |
| `population.users`, `population.basis` | Size and definition of the comparison population. |
| `cards[]` | `intro`, then the selected cards, then `summary`. |
| `cards[].type`, `.family`, `.shareable` | Catalogue entry (see `card_types`). |
| `cards[].headline`, `.value`, `.unit`, `.body` | What is shown. |
| `cards[].claim` | Null, or `metric`, `top_permille` (tenths of a percent: 50 = "top 5%"), `text`, `basis`. |
| `cards[].stats` | Summary card only: up to three `{label, value}`. |
| `cards[].facts` | Every number in the card, keyed by the `mart_user_year` column it came from (`population_*` for `mart_population`). Checked by the audit. Not returned by the API. |

## 5. Serving database (PostgreSQL)

| Table | Key | Columns | Notes |
|---|---|---|---|
| `generation_runs` | `run_id` uuid | `year` smallint 2008-2100; `status` `loading` or `ready`; `source_fingerprint` text; `population` int >= 0; `user_count` int >= 0; `started_at`; `finished_at` nullable | Check: `finished_at` is set exactly when `status = 'ready'`. |
| `active_runs` | `year` | `run_id` FK; `previous_run_id` FK nullable; `activated_at` | Which run is served. Check: previous differs from current. |
| `card_types` | `card_type` text | `family` text; `shareable` boolean | Written from the code's catalogue at publish. |
| `wrapped_payloads` | (`run_id`, `user_id`) | `login` text non-empty; `tier` `full`/`light`/`minimal`; `payload` jsonb object | `run_id` FK, cascade. `user_id` > 0. |
| `payload_cards` | (`run_id`, `user_id`, `position`) | `card_type` FK | FK to the payload, cascade. Unique (`run_id`, `user_id`, `card_type`). No secondary index (one was measured, found unused, and dropped in migration 0003). |
| `card_views` | (`user_id`, `year`, `card_type`) | `first_viewed_at` | The key makes recording a view idempotent. |
| `shares` | `share_id` text, 22+ chars | `user_id`; `year`; `card_type` FK; `login`; `card` jsonb object (the snapshot); `source_run_id` uuid; `created_at`; `updated_at` | Unique (`user_id`, `year`, `card_type`). `source_run_id` is deliberately not a foreign key: a share outlives its run. |

All columns are `NOT NULL` unless marked nullable. Roles: `wrapped_api_role` may read payloads and
write `card_views` and `shares`; `wrapped_batch_role` may write runs and payloads and cannot read
`card_views` or `shares`.

Index evidence: [evidence/explain-analyze.md](evidence/explain-analyze.md).
