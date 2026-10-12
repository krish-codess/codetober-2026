# Operations: deploy, roll back, back up, and what breaks how

Target: one cloud VM running Docker Compose (Kafka, PostGIS, the services), with the UI either served by
the API container or from a CDN.

> **Status.** The procedures below are written and scripted but have **not been run against a real VM**:
> no cloud account was available for this build, and Docker would not start on the development machine.
> The CI workflow has an `images` job that validates the compose file and builds the image, but CI has not
> run yet either. The application itself was run and tested natively. Treat the first deployment as a rehearsal.

## Deploy

One-time, on a fresh Ubuntu 24.04 VM (2 vCPU, 4 GB is enough for the default simulator feed):

```bash
curl -fsSL https://get.docker.com | sh
git clone https://github.com/krish-codess/codetober-2026 ~/ghostfleet
cd ~/ghostfleet/INGEST && cp .env.example .env
# edit .env: set every password; set GHOST_SOURCES and AISSTREAM_API_KEY for real feeds
```

Put a TLS-terminating reverse proxy (Caddy, or the cloud load balancer) in front of port 8000. Only 8000
needs to be reachable; Prometheus and Grafana bind to localhost on the VM (reach them with `ssh -L`).

Every release, from your own machine:

```bash
git tag v1.0.0 && git push origin v1.0.0
ops/deploy.sh user@vm v1.0.0 https://fleet.example.com
```

The script checks out the tag on the VM, runs `docker compose up --build --wait` (migrations run first, in
the `init` container), then verifies **from outside** that `/readyz` and `/api/v1/stats` answer. If they do
not within five minutes, it redeploys the previous tag and exits non-zero.

### UI on a CDN

```bash
cd web && VITE_API_BASE=https://api.fleet.example.com npm run build
# upload web/dist/ to the bucket behind the CDN; index.html: no-cache, assets/*: immutable
```

and set `GHOST_CORS_ORIGINS=https://fleet.example.com` in `.env` so the API accepts the CDN's origin.

## Roll back

```bash
ops/deploy.sh user@vm --rollback https://fleet.example.com
```

Redeploys the previously deployed tag and verifies it. **Schema policy that makes this safe:** a migration
may add tables, nullable columns and indexes; it may not drop or rename anything the previous release
reads. Destructive changes ship one release after the code stops using the thing. Under that policy the
old code runs on the new schema and no schema rollback is needed. If one is needed anyway:

```bash
docker compose run --rm init migrate --to <version>     # runs the .down.sql files in reverse
```

Both directions of every migration run in CI (`test_migrations_roll_back_and_forward`).

## Back up and restore

`ops/backup.sh /var/backups/ghostfleet` (nightly from cron) writes a `pg_dump` in custom format, verifies it
is readable, copies new raw-archive files, and prunes dumps older than 14 days. Copy that directory off the VM.

Restore onto a fresh stack:

```bash
docker compose up -d postgis
docker compose exec -T postgis pg_restore -U ghost_owner -d ghost --clean --if-exists < ghost-<stamp>.dump
docker compose up -d
```

If the dump is lost but the archive survives, positions and quarantine can be rebuilt (derived tables
follow on the next detector pass):

```bash
docker compose run --rm collector replay /app/data/raw/<source> --source <source>
```

## Behaviour under partial failure

| What fails | What the system does | What a user sees | Recovery |
|---|---|---|---|
| One provider drops or goes silent | Reader reconnects with full-jitter backoff, 1 s doubling to a 60 s cap, forever; a connection silent for 30 s is treated as dead | Nothing, if another provider overlaps; otherwise fewer updates in that area | Automatic. Alert `ProviderDown` after 3 min |
| All providers down | As above | Map banner: "The feed itself is stale: nothing new for N min". The detector does **not** mark the fleet dark (it reasons from data time) | Automatic. Alert `AllProvidersDown` |
| Kafka down | Readers keep archiving. The pump fails, retries with backoff, and resumes from its checkpoint | Map goes stale, with the same banner | Automatic; backlog drains at about 2,000 rows/s. Alert `ArchiveBacklogGrowing` |
| PostGIS down | Sink tears down and retries with backoff; offsets are not committed, so nothing is skipped. Kafka retains 72 h | API answers 503 with `Retry-After: 5` and a structured error; the UI keeps the last data and says updates are failing | Automatic when the database returns |
| Sink crashes mid-batch | Batch is redelivered; the primary key makes it a no-op | Nothing | Automatic (container restart) |
| Collector crashes | On restart the pump resumes from the checkpoint; fragments in flight are re-read | A gap in live data for the restart's duration | Automatic. Data broadcast while it was down is not recoverable from that provider |
| Detector down | Events stop updating; ingestion is unaffected | Event list stops changing | Automatic; the next pass recomputes the whole window |
| A poison message | Dead-lettered with payload and reason; the rest of the batch is stored | Nothing | `SELECT reason, count(*) FROM quarantine GROUP BY 1`, fix, then `ghostfleet replay-dlq --reason <reason>` |
| Disk full | Archive writes fail and the collector exits; no acknowledgement is given for unwritten data | Stale map | Free space. Closed hour files older than the backup can be deleted |
| API overloaded | Pool of 8 connections; 3 s acquire timeout, 10 s statement timeout, then 503 | Slow, then a clear error with a request id | Scale API replicas; they are stateless |

Every retry in the system is the same `Backoff` class: exponential, jittered, capped.

## Observability

- **Logs:** JSON on stderr, one object per line, with a `cid` correlation id: the archive position of the
  batch in the collector, the Kafka coordinates in the sink, the request id in the API (also returned as
  `X-Request-ID` and inside every error body). A quarantined row's `cid` leads back to the raw line.
- **Metrics:** each worker exposes Prometheus metrics on port 9100, the API on `/metrics`. Definitions are in `ghostfleet/obs.py`.
- **Dashboard:** `ops/grafana/dashboards/ingest.json`, generated by `ops/make_dashboard.py`, provisioned automatically.
- **Alerts:** `ops/alerts.yml`. Routing to a pager or chat is not configured (no Alertmanager in the stack).
- **Health:** workers are healthy only if `ghost_last_success_timestamp_seconds` is recent, meaning they completed a
  round trip to Kafka or PostGIS; the API's `/readyz` runs a query and reports ingest staleness. `/healthz` is liveness only.

## Routine tasks

| Task | Command |
|---|---|
| Replay an archived window | `docker compose run --rm collector replay /app/data/raw/<source>/<file>.nmea --source <source>` |
| Resubmit quarantined input after a fix | `docker compose run --rm sink replay-dlq --reason <reason>` (uses the ingest role) |
| One detector pass now | `docker compose run --rm detector detect --once` |
| Profile a capture | `docker compose run --rm collector profile /app/data/raw/<source>/<file>.nmea` |
| Add sink capacity | `docker compose up -d --scale sink=4` (at most one per partition is useful) |
