# Operations

## Behaviour when a dependency fails

| dependency down | what users see | what the system does | recovery |
|---|---|---|---|
| **PostgreSQL** | `503 DEPENDENCY_UNAVAILABLE` + `Retry-After: 2` on anything that sells; UI says nothing was reserved and to retry. Pool waits are capped at 2 s, so requests fail fast instead of hanging. | Readiness goes down; pods leave rotation. Sweeper, relay and consumer log and retry on their next tick. Overdue holds cannot be confirmed in the meantime (the deadline is data, not a timer). | Automatic when the database returns. |
| **Redis** | Availability keeps working (read straight from Postgres). Queue join/poll returns `503 WAITING_ROOM_UNAVAILABLE` + `Retry-After: 5`; the UI keeps the last known position on screen, marked stale. People already admitted can still buy (admission is a signed token). | Nobody new is admitted (fail closed). Commands time out at 500 ms. | Automatic. If Redis came back **empty**, clients are told `NOT_IN_QUEUE` and rejoin by themselves; queue order is lost. |
| **Kafka** | Nothing. | Outbox rows accumulate in Postgres; the relay retries with exponential backoff capped at 30 s (`lastticket_outbox_failures_total`). Analytics go stale. | Automatic; the backlog drains in order. Duplicates are absorbed by the idempotent consumer. |
| **An application pod** dies mid-request | A 5xx or dropped connection; a retry with the same `Idempotency-Key` returns the original reservation if it had committed, or creates it if not. | Uncommitted work rolls back. Holds being expired by that pod are still due and are taken by another pod. | Kubernetes restarts it. |

Every retry in the system has a backoff and a cap: optimistic write (20 attempts, ≤ 32 ms jitter, then `503`),
aggregate queue (3 s, then `503`), Kafka consumer (3 attempts, then `dead_letter`), outbox relay (backoff ≤ 30 s, never
drops), UI polling (interval set by the server).

## Observability

- **Logs**: JSON (ECS) on stdout. Every line of a request carries `correlationId` = the `X-Request-Id` response header
  (send your own, 8–64 chars of `[A-Za-z0-9._-]`, to trace across services). Every error body carries it too, and every
  inventory event stores it.
- **Metrics**: `:8081/actuator/prometheus`. The ones that matter:
  `lastticket_reserve_seconds` (histogram, critical path), `lastticket_reserve_outcome_total{outcome}` (held, sold_out,
  contention, replayed, …), `lastticket_version_conflicts_total`, `lastticket_sweeper_expired_total`,
  `lastticket_waitingroom_admitted_total`, `lastticket_outbox_published_total`, `lastticket_outbox_failures_total`,
  plus `http_server_requests_seconds` and `hikaricp_connections_pending`.
- **Health**: `:8081/actuator/health` runs a query against Postgres, a `PING` against Redis and a cluster describe
  against Kafka. `…/health/liveness` is process-only; `…/health/readiness` is process + Postgres (Redis and Kafka are
  excluded on purpose: losing them must not take every pod out of rotation).
- **Is anything oversold?** `GET /api/admin/events/{id}/analytics` → `oversoldTickets` and `snapshotDrift` must be 0.

Alert on: `oversoldTickets > 0` or `snapshotDrift > 0` (page), outbox backlog growing
(`SELECT count(*) FROM outbox WHERE published_at IS NULL`), any row in `dead_letter` with `replayed_at IS NULL`,
`reserve_outcome{outcome="contention"}` rate, `hikaricp_connections_pending > 0`.

## Runbook

**Before an on-sale**
1. Create the event: `PUT /api/admin/events/{uuid}` (idempotent).
2. Set `admissionRatePerSec` to what the write path commits within budget on this deployment (`docs/performance.md`).
   It cannot be changed afterwards (event rows are immutable); to change it, create a new event.
3. **Warm the instances.** A cold JVM is ~20x slower for its first seconds. Run the load test's warm-up against a
   throwaway event, and do not deploy in the minutes before the opening.

**Stock changes during a sale**: `POST /api/admin/inventory/{id}/adjustments` with `delta`, the `expectedVersion` you
last read, and a reason. A correction below what is held or sold is refused; wait for holds to expire or reduce by less.

**Dead letters**: `GET /api/admin/dead-letters`, fix the cause, `POST /api/admin/dead-letters/{id}/replay`.

**Quarantined feed lines**: `GET /api/admin/attempts/quarantine` shows the original line and the reason. Fix at the
source and send a new batch; outcomes are append-only.

**What was the position at 10:00:07?** `GET /api/admin/events/{id}/position?at=2026-10-10T10:00:07Z`.

## Deploy (Kubernetes, managed Postgres / Kafka / Redis)

Prerequisites: a cluster with an ingress controller; a PostgreSQL 16 database and an **owner** role for it; a Redis 7
endpoint with `maxmemory-policy noeviction`; a Kafka cluster; a container registry.

```bash
# 1. Images (tag = git SHA)
docker build -t REGISTRY/last-ticket-backend:$SHA backend   && docker push REGISTRY/last-ticket-backend:$SHA
docker build -t REGISTRY/last-ticket-frontend:$SHA frontend && docker push REGISTRY/last-ticket-frontend:$SHA

# 2. Secrets, once, from your secrets manager (never from git)
kubectl create namespace last-ticket
kubectl -n last-ticket create secret generic last-ticket-app \
  --from-literal=DB_URL='jdbc:postgresql://HOST:5432/lastticket?sslmode=require' \
  --from-literal=DB_APP_USER=lastticket_app --from-literal=DB_APP_PASSWORD=... \
  --from-literal=REDIS_HOST=... --from-literal=REDIS_PORT=6379 --from-literal=REDIS_PASSWORD=... \
  --from-literal=KAFKA_BOOTSTRAP_SERVERS=... \
  --from-literal=JWT_SECRET="$(openssl rand -base64 48)" --from-literal=ADMIN_API_KEY="$(openssl rand -hex 32)"
kubectl -n last-ticket create secret generic last-ticket-migrator \
  --from-literal=DB_OWNER_USER=... --from-literal=DB_OWNER_PASSWORD=...

# 3. Point deploy/k8s/overlays/production/kustomization.yaml at your registry and tag, then
kubectl apply -k deploy/k8s/overlays/production
kubectl -n last-ticket rollout status deployment/backend

# 4. Verify from outside
curl -fsS https://YOUR_HOST/api/events
```

Managed services usually need TLS and auth. They are plain Spring properties, set as extra keys in `last-ticket-app`:
`SPRING_DATA_REDIS_SSL_ENABLED=true`; for Kafka `SPRING_KAFKA_PROPERTIES_SECURITY_PROTOCOL=SASL_SSL`,
`SPRING_KAFKA_PROPERTIES_SASL_MECHANISM=…`, `SPRING_KAFKA_PROPERTIES_SASL_JAAS_CONFIG=…`.
The owner role must be allowed to `CREATE ROLE` (migration V1 creates the app role); on providers where it cannot,
create `lastticket_app` by hand first and V1 will only set its password.

Migrations run in the `migrate` init container with the owner credentials; the serving container starts with
`SPRING_FLYWAY_ENABLED=false` and only the DML role. Flyway holds a lock, so replicas starting together migrate once.

### Verify locally on kind (what was actually run: `docs/deploy-verification.txt`)

```bash
kind create cluster --name last-ticket
docker build -t last-ticket-backend:v1 backend && docker build -t last-ticket-frontend:v1 frontend
kind load docker-image --name last-ticket last-ticket-backend:v1 last-ticket-frontend:v1   # see note
kubectl apply -k deploy/k8s/overlays/local
kubectl -n last-ticket rollout status deployment/backend
kubectl -n last-ticket port-forward service/frontend 13080:80 &
cd frontend && BASE_URL=http://localhost:13080 ADMIN_API_KEY=local-admin-key npx playwright test
```

Note: with Docker Desktop's containerd image store, `kind load` fails with "content digest not found". Load each image
with `docker save --platform linux/amd64 IMAGE | docker exec -i last-ticket-control-plane ctr -n k8s.io images import -`.

## Rollback

**Application** (no schema change in the release):
```bash
kubectl -n last-ticket rollout undo deployment/backend          # previous revision
kubectl -n last-ticket rollout status deployment/backend
kubectl -n last-ticket rollout history deployment/backend       # 5 revisions kept
```
A release that cannot start never takes traffic: `maxUnavailable: 0` keeps the old pods serving until new ones pass
readiness. Both the stuck release and the undo were exercised: `docs/deploy-verification.txt`.

**Schema**: every `V<n>` has a `U<n>` in `backend/src/main/resources/db/rollback`, exercised by
`IntakeAndMigrationTest.migrationsRollBackAndReapply`. Roll the application back first, then, newest first:
```bash
psql "$OWNER_URL" -v ON_ERROR_STOP=1 -f <(sed 's/${app_user}/lastticket_app/g' U3__drop_unused_as_of_index.sql)
```
`U2` drops every table: take a backup first. Prefer forward fixes for anything that has shipped data.

## Backup and restore

- **PostgreSQL is the only system of record.** Use the managed service's continuous backup with point-in-time recovery;
  set retention to cover at least the longest refund window. Before any schema rollback, and before an on-sale:
  `pg_dump --format=custom --no-owner "$OWNER_URL" > lastticket-$(date +%F).dump`.
  Restore: `pg_restore --clean --if-exists --no-owner -d "$OWNER_URL" lastticket-DATE.dump`, then restart the backend.
  After a restore, check `snapshotDrift` and `oversoldTickets` are 0 for live events.
- **Redis: no backup.** It holds the queue and a 1-second cache; the system is correct when it is empty.
- **Kafka: no backup.** The outbox table is the source; anything unpublished is re-sent. To rebuild `sale_stat` after
  losing the topic *and* the table, set `published_at = NULL` on the outbox rows (owner role) and let the relay re-send;
  consumers deduplicate.
