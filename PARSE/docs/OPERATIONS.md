# Operations

Deployment target: containerised services on one Docker host — `db`, `api`, `worker` (scheduled
retraining), `web` (labelling frontend), plus two one-shot containers, `migrate` and `seed`.
Everything below was executed against the local stack; the transcript is in
[`docs/evidence/operations.txt`](evidence/operations.txt).

## Services

| Service | Image | Role | DB credential | Exposed |
|---|---|---|---|---|
| db | postgres:16-alpine | storage | — | 127.0.0.1:`DB_PORT` |
| migrate | parse-backend | `alembic upgrade head`, then exits | **owner** | — |
| seed | parse-backend | tokens, feed, ingest, embed, first models; idempotent; exits | app | — |
| api | parse-backend | HTTP API | app | 127.0.0.1:`API_PORT` |
| worker | parse-backend | runs jobs, schedules retraining | app | — |
| web | parse-web (nginx, unprivileged) | static UI, proxies `/api/` | — | 127.0.0.1:`WEB_PORT` |

Ports bind to loopback. To publish, put a TLS-terminating reverse proxy in front of `web` only.

## Deploy

```bash
cp .env.example .env            # then change every password and token
export GIT_SHA=$(git rev-parse --short HEAD) IMAGE_TAG=$GIT_SHA
docker compose build            # images are tagged parse-backend:$IMAGE_TAG, parse-web:$IMAGE_TAG
docker compose up -d --wait     # db -> migrate -> seed -> api, worker -> web
scripts/smoke.sh http://127.0.0.1:8141
```

First boot with `SEED_SOURCE=real` downloads the corpus (38 MB) and the embedding model (118 MB)
and embeds 56k texts: allow ~15 minutes on a laptop CPU. Later boots skip all of that (each stage
is idempotent). `SEED_SOURCE=synthetic EMBED_BACKEND=hash` boots in under a minute with generated
data and is what CI uses; it is not meaningful for accuracy.

Upgrade = the same three commands with a new `IMAGE_TAG`. `migrate` runs before anything else
starts, and `seed` is a no-op on an existing database.

## Health and observability

- `GET /api/v1/health` runs a query against the schema, deserialises the active model and embeds a
  string. `503` only when the database is unreachable; `degraded` when the model or embedder is
  missing (labelling still works; `/classify` returns a structured 503).
- Logs: one JSON object per line on stdout, with `correlation_id` — the request id for API calls
  (also returned as `X-Request-ID` and shown in UI error messages), `job-<id>` for worker runs.
  `docker compose logs api | grep <request id>` reconstructs a request.
- Metrics: `GET :8000/metrics` on the api container (Prometheus text; not proxied by nginx):
  `http_requests_total{method,route,status}`, `http_request_seconds_{sum,count}{route}`,
  `classify_seconds_*`, `classified_texts_total`, `annotations_total`,
  `ingest_accepted_total`, `ingest_quarantined_total{reason}`. The worker logs
  `train_done` / `job_finished` events with durations.

## Behaviour under partial failure

| Dependency fails | What happens | What the user sees |
|---|---|---|
| Hugging Face at seed time | 5 attempts, exponential backoff 1→30 s; then the seed falls back to synthetic data and logs `seed_degraded_to_synthetic` at ERROR with the fix | A working stack with generated data, and a log line saying so |
| Hugging Face at API start (model not cached) | API starts without the embedder | Health `degraded`; `/classify` → 503 `embedder_unavailable`; labelling unaffected |
| Database down | `pool_pre_ping` discards dead connections; requests fail fast | 503 `database_unavailable` with `Retry-After: 5`; health `down`; UI shows "Try again" |
| A training job raises | Retried after 30 s, then 120 s; after 3 attempts marked `failed` with the error | Job list shows the failure and reason; the previous model keeps serving |
| Worker crashes mid-job | On restart, `running` jobs are re-queued | Job shows "re-queued after restart" |
| New model is worse | Regression gate marks it `rejected` | Active model unchanged; job result says "rejected by the regression gate" |
| A feed line is invalid | Quarantined with a reason; the rest of the batch is ingested | Counts by reason in the response and on the Taxonomy page |

## Rollback

**Application** (no schema change): redeploy the previous tag.

```bash
IMAGE_TAG=<previous> docker compose up -d --no-build --wait api worker web
```

**Model**: every version stays in `model_versions` with its artifact. To serve an earlier one:

```sql
BEGIN;
UPDATE model_versions SET status = 'archived' WHERE status = 'active';
UPDATE model_versions SET status = 'active'   WHERE id = <id>;
COMMIT;
```

The API notices the new active id on its next request; queue a retrain job (or wait for the
schedule) to rescore the pool with it.

**Schema**: each migration has a tested `downgrade`.

```bash
docker compose run --rm migrate alembic downgrade -1
```

`0001` is the only migration, so `downgrade -1` drops everything: take a backup first.

**Taxonomy**: changes are not undone by rollback; apply the inverse operation (merge after a
split, rename back, move back). Retired nodes are never deleted, so history stays resolvable.

## Backup and restore

The database is the whole state: data, labels, taxonomy history, model artifacts. The `appdata`
volume holds only re-downloadable inputs and caches.

```bash
# backup (custom format, compressed)
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > backup-$(date +%F).dump

# restore into an empty database
docker compose stop api worker
docker compose exec -T db dropdb   -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner < backup-YYYY-MM-DD.dump
docker compose start api worker
```

Schedule the backup with the host's cron; keep at least one copy off the host. Point-in-time
recovery (WAL archiving) is not configured.

## Scale: what breaks first at 10×

Measured volumes and timings are in [PERFORMANCE.md](PERFORMANCE.md).

1. **Embedding at ingest (first to break).** ~100 texts/s on one CPU process. 10× the corpus is
   ~1.5 hours of embedding. Change needed: run several embedding workers over disjoint id ranges
   (the stage is already idempotent and chunked), or move the same ONNX file to a GPU provider.
2. **Training loads every labelled embedding into memory and refits all nodes.** Fine to ~50k
   labelled items (≈ 5 min). Beyond that: warm-start from the previous weights and refit only
   nodes whose training set changed.
3. **Evaluation scores the whole test split on every retrain** (45k items, ~10 s) and rescoring
   the pool is O(pool). At 10× pool: rescore only the top-uncertainty slice plus a random sample.
4. **Ingest keeps dedupe maps in memory** (ids and hashes of every row). At ~10M rows move
   duplicate resolution to a temp table and `COPY`.
5. The API itself is stateless apart from a cached model; add replicas behind nginx. In-process
   metrics are then per replica.
