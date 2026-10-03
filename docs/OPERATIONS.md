# Operations

Everything here was executed on the reference machine. The results are in `docs/perf/` and
`docs/explain/`, and the drills are recorded at the end of this file.

## Topology

| Service | Image | Port | Role |
|---|---|---|---|
| postgres | postgres:16-alpine | 5433 → 5432 | Published data plus Dagster run storage (separate `dagster` database) |
| migrate | goldstandard-pipeline | — | One-shot: `migrate up` as `gs_owner`, bootstraps the hashed admin API key |
| seed | goldstandard-pipeline | — | One-shot and idempotent: real EVE snapshot (+ live top-up), synthetic feed, full pipeline |
| api | goldstandard-api | 8000 | FastAPI as `gs_api`; healthcheck = `/health/ready` |
| dagster-webserver | goldstandard-pipeline | 3000 | Dagster UI (assets, runs, backfills) |
| dagster-daemon | goldstandard-pipeline | — | Schedules (ingestion) and sensors (changed partitions → targeted runs) |
| frontend | goldstandard-frontend (nginx) | 8080 | Static bundle; `/api/*` is reverse-proxied to the API on the same origin |

Volumes: `pgdata` (database) and `lake` (`/data`: raw store and Parquet lake). **The raw store is the
source of truth.** The lake and every published value can be rebuilt from it.

Schedules (UTC): synthetic feed tick at `:05` every 6 h; EVE order-book snapshot at `:15` every 6 h;
EVE history, patch notes and item metadata at 11:45 daily, after the 11:00 downtime.

## Configuration

All configuration comes from environment variables; see `.env.example` for the full list with
comments. Secrets are `POSTGRES_PASSWORD`, `GS_OWNER_PASSWORD`, `GS_PIPELINE_PASSWORD`,
`GS_API_PASSWORD` and `GS_BOOTSTRAP_ADMIN_KEY`. None is in the repository, and compose refuses to
start (`?set in .env`) if one is missing.

## Deploy (single VM, Docker Compose)

```bash
git pull && export GS_IMAGE_TAG=$(git rev-parse --short HEAD)
docker compose build                       # images tagged with the commit
docker compose up -d                       # migrate -> seed -> api/dagster -> frontend, in dependency order
```

Verify from outside the host, not from inside a container:

```bash
curl -fsS http://HOST:8000/health                        # liveness
curl -fsS http://HOST:8000/health/ready | jq .status     # "ok", or "degraded" if data is stale; 503 = database down
curl -fsS "http://HOST:8080/api/v1/index?world=eve" | jq '.points[-1]'
curl -fsS http://HOST:8000/metrics | grep http_requests_total
```

Migrations are applied by the `migrate` service before the API starts. Each migration runs in its
own transaction and is checksummed, so a failed migration leaves the previous version in place and
the stack does not start.

## Rollback

**Application rollback** (the normal case): images are tagged by commit, so redeploy the previous tag.

```bash
GS_IMAGE_TAG=<previous-sha> docker compose up -d api frontend dagster-webserver dagster-daemon
```

**Schema rollback**: every migration has a tested `down` (CI runs up, down and up again on every
push).

```bash
docker compose run --rm migrate goldstandard migrate down --target <N>
```

Rolling back a migration can drop data (0001's down drops everything). Prefer a forward fix, and
take a backup first. Published values are append-only, so an application rollback never needs a data
rollback: a bad publication is corrected by publishing a new vintage (`reason = method_change`), and
the old values stay auditable.

## Backup and restore

```bash
# backup: database (custom format) + raw store (the source of truth); the lake is rebuildable
docker compose exec -T postgres pg_dump -U postgres -d goldstandard -Fc > backup/goldstandard.dump
docker run --rm -v goldstandard_lake:/data -v "$PWD/backup":/b alpine tar -czf /b/raw.tgz -C /data raw

# restore into a fresh stack
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U postgres -d goldstandard --clean --if-exists < backup/goldstandard.dump
docker run --rm -v goldstandard_lake:/data -v "$PWD/backup":/b alpine tar -xzf /b/raw.tgz -C /data
docker compose up -d
```

If only the raw store survives, start a fresh stack and run the seed. The index is rebuilt bit for
bit (`test_index_is_reproducible_from_raw_into_a_fresh_database`). Vintage *history*, meaning what
was published when, is the one thing only the database backup holds.

## Failure behaviour (every external dependency)

| Failure | What happens | What a user sees | Recovery |
|---|---|---|---|
| ESI slow or 5xx/420/429 | Retries with full-jitter exponential backoff (base 0.5 s, cap 30 s, max 5 retries); `Retry-After` and ESI's error budget are honoured | Nothing; data arrives later | Automatic |
| ESI down for a whole run | Each failed request is recorded; the run is `degraded` (partial) or `failed` (all); nothing is published for missing items | Readiness `degraded` (ingestion check); a **stale-data banner** after 48 h; thin/missing coverage marked on the chart | Next schedule; sensors pick up the late raw files |
| Some (region, item) fetches fail | Kept in the raw bundle as gaps; prices for those cells are `missing`, never carried forward | Partial coverage (dashed line, banner) | The next snapshot fills them |
| Late or revised raw data | The sensor sees the day's fingerprint change and reprocesses that day plus its dependents only; changed values become new vintages (`late_data` / `source_revision`) | Values marked **revised (vN)**; `as_of` shows the old ones | Automatic |
| Corrupt raw file | The hash check fails on read and the partition run fails loudly; nothing is published from it | Day stays at its previous published values | Restore the file from backup, or delete it and refetch |
| Malformed rows | Quarantined with a reason and kept in `lake/quarantine`; counts in `data_quality_daily` | Nothing | Inspect the quarantine and fix the parser if it was a format change |
| PostgreSQL down | API: requests return **503 `database_unavailable`**, the process stays up, readiness is 503. Pipeline: runs fail and Dagster retries (3×, exponential) | Error panel: *"temporarily unavailable"* with a Retry button; cached data stays on screen | Automatic when the DB returns |
| A pipeline crash mid-day | Partition fingerprints are written last, so the day is detected as unprocessed and redone; republishing identical values writes nothing | Nothing | Automatic (next sensor tick) |

## Observability

* **Logs:** JSON lines on stderr with `ts, level, logger, msg, correlation_id` plus structured fields.
  * The API takes `X-Request-ID` from nginx (or generates one), logs it on every line and returns it.
    Error bodies carry it as `request_id`.
  * Pipeline runs use the Dagster run id.
* **Metrics** (`GET /metrics`, Prometheus text):

  | Metric | What it shows |
  |---|---|
  | `http_requests_total{route,method,status}` | API traffic |
  | `http_request_seconds{route,quantile}` | API latency |
  | `api_errors_total{code}` | Errors by code |
  | `esi_requests_total{status}` | ESI outcomes |
  | `esi_request_seconds` | ESI latency |
  | `ingest_payloads_total` | Payloads ingested |
  | `days_processed_total` | Pipeline progress |
  | `stage_seconds` / `analytics_seconds` | Stage timings |

* **Health:**
  * `/health` is liveness.
  * `/health/ready` runs a real query, reports per-world freshness and the last ingest run status,
    and returns 503 if PostgreSQL is unreachable.
* **Dagster UI:** per-partition materialisation history, run logs and backfills.

## Security

* Every SQL statement is parameterised. Dynamic identifiers go through `psycopg.sql.Identifier`.
* Inputs are validated at the boundary: Pydantic models with patterns and limits on every query
  parameter and body field, `extra="forbid"` on writes, and parse validation with quarantine for
  all external data.
* There are three least-privilege database roles (DECISIONS D-15). The API's sessions default to
  read-only transactions.
* API keys are stored only as SHA-256 and compared in constant time. Write endpoints need a scope
  and an `Idempotency-Key`.
* Errors never include stack traces. Unhandled exceptions return a generic message with the request
  id, and the trace goes to the log only.
* nginx sends CSP, `nosniff` and `no-referrer`; the API is same-origin through nginx, and CORS is
  limited to the configured origins.
* Containers run as non-root (uid 10001 / nginx-unprivileged), and runtime images contain no
  compiler, pip or uv.
* **Rotate the admin key:** set a new `GS_BOOTSTRAP_ADMIN_KEY`, then run
  `docker compose run --rm migrate goldstandard create-api-key --key-id admin`.
