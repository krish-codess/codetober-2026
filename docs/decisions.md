# Decisions log

What was chosen, what was rejected, and why. Newest last. "Not built" entries are deliberate.

## 1. One deployable (modular monolith), not microservices
**Chose** a single Spring Boot service with package boundaries (`inventory`, `waitingroom`, `messaging`, `intake`, `api`, `config`).
**Rejected** separate reservation / waiting-room / sweeper services. They would share one database and one release cadence,
so splitting them buys network hops and distributed failure modes, not independence. The sweeper and the outbox relay are
safe to run in every replica (guarded writes, advisory lock), so they do not need their own deployment either.

## 2. Event stream + snapshot row in the same transaction
**Chose** `inventory_event` (append-only, source of truth) plus the `inventory` row as a snapshot updated in the same
transaction. The snapshot row *is* the available-to-promise projection.
**Rejected** pure event sourcing with an asynchronously built projection: the reservation decision needs the current
position, and an eventually consistent projection would mean deciding on stale stock.
**Guard**: `GET /api/admin/events/{id}/analytics` reports `snapshotDrift` (sections whose snapshot differs from a replay
of their stream); the load test fails if it is not 0.

## 3. Optimistic concurrency, three layers deep
1. Application: `UPDATE inventory … WHERE id = ? AND version = ?`; 0 rows means someone else moved first, the
   transaction rolls back and is retried (full-jitter backoff, capped at 32 ms, 20 attempts, then `503 CONTENTION`).
2. Storage: `UNIQUE (inventory_id, version)` on the event stream. Two writers cannot both append version N+1 even if
   layer 1 had a bug.
3. Invariant: `CHECK (held + sold <= total)` on the snapshot. No code path can commit an oversold row.

**Rejected** `SELECT … FOR UPDATE` (pessimistic): it is what the brief asks not to do, and it holds locks across
application round-trips. **Rejected, but worth knowing**: a single atomic `UPDATE … SET held = held + ? WHERE total - held - sold >= ?`
needs no version and no retries and would be faster for this one command; it was not used because the version check is
the brief's explicit subject and because it generalises to every command on the aggregate.

## 4. A per-aggregate gate in front of the optimistic write (added after measuring)
The first load test produced **5.7 failed attempts per successful hold** (2,298 version conflicts for 400 holds). Every
commit made every concurrent writer on that section fail and retry, and blocked writers held all 20 pool connections,
which slowed queue polls and reads that had nothing to do with the contended rows.
**Chose** a fair in-process semaphore per inventory id (default 1 permit, `lastticket.writers-per-aggregate`): one
transaction per aggregate per instance is in the database at a time; the rest wait in memory in arrival order holding no
connection, for at most 3 s before `503 CONTENTION`. Result on the same test: 0 conflicts, 0 pool waits.
It is a contention limiter, not the correctness mechanism: across instances the version check still arbitrates, and
`ReservationConcurrencyTest` runs with the gate opened to 16 writers to prove no-oversell holds on the version check alone
(≈200 conflicts per run, 0 oversold).
**Also added**: a sold-out fast path (plain snapshot read before queueing) so that once a section is gone the thousands
still asking get an immediate answer and never contend with real buyers.

## 5. Database clock is the only clock that decides anything about a hold
`expires_at = now() + hold_seconds`, confirm requires `expires_at > now()`, the sweeper selects `expires_at <= now()`,
all evaluated by Postgres. Application instances can disagree about the time without consequence. The API returns
`serverTime` so the browser counts down against the server, not the device.

## 6. Expiry does not depend on the sweeper being prompt
A hold past its deadline cannot be confirmed even if the sweeper has not run (`410 HOLD_EXPIRED`). The sweeper only
returns stock to the pool; until it does, availability is understated, which is the safe direction. Each hold is expired
in its own transaction guarded by `status = 'HELD'`, so confirm / release / expire are mutually exclusive and a crash
mid-batch loses nothing.

## 7. Waiting room: lottery before the opening, FIFO after
Everyone who joins before `on_sale_at` gets a uniformly random score in [0, 1); everyone after gets
`1 + ms since opening`. Joining is `ZADD NX`, so refreshing or re-joining never changes your place.
**Rejected** pure FIFO from the moment the page opens: it rewards camping and scripts that connect first.
**Rejected** a pure lottery for everyone: late arrivals could jump people who have been waiting since the opening.
Admission is a Lua script (pop N + mark admitted atomically) run once per second per event, with a one-per-second guard
key so N instances do not admit N times the rate.

## 8. Admission is a signed token, not a Redis lookup
When your turn comes the status endpoint hands out a short-lived HS256 token bound to (user, event). The reservation
endpoint verifies the signature. **Why**: the purchase path then depends only on Postgres; Redis can be down or wiped
mid-sale and people already admitted can still buy.

## 9. Redis is disposable; failure is fail-closed for admission, fail-open for reads
- Availability cache miss or Redis error → read the snapshot rows from Postgres.
- Waiting room unreachable → `503 WAITING_ROOM_UNAVAILABLE` with `Retry-After`; nobody new is admitted (letting everyone
  in would be the stampede the room exists to prevent). The UI keeps the last known position on screen, marked stale.
- Redis wiped → queued users are told `NOT_IN_QUEUE`; the UI rejoins automatically. Order among them is lost; that is
  the price of not persisting the queue, and it is stated in the limitations.

## 10. Transactional outbox to Kafka; effectively-once downstream
State change and outbox row commit together. A single relay (Postgres advisory lock) publishes in id order, keyed by
inventory id, and marks exactly the ids it sent. Delivery is **at-least-once**; the consumer inserts the message id into
`processed_message` in the same transaction as its counter updates, so a redelivery is a no-op: **effectively-once**.
**Rejected** Kafka transactions / exactly-once semantics: they do not cover the Postgres side, so the idempotent consumer
is needed anyway. **Rejected** publishing straight to Kafka from the request: a broker outage would fail or lose sales.
With the outbox, Kafka can be down for the whole on-sale and nothing upstream notices.

## 11. Dead letters in a table
Poison messages go straight to `dead_letter` (original payload, topic/partition/offset, reason); transient failures get
3 in-place retries with exponential backoff first. Replay is an admin endpoint that re-publishes the payload.
**Rejected** a `.DLT` topic: a table is queryable, paginated and replayable with what is already here.

## 12. Synthetic data, treated as hostile
There is no public feed of on-sale traffic, so `loadtest/generate-attempts.mjs` produces one (seeded) with the arrival
shape and ten kinds of defect, and `docs/data-profile.md` is the profile of that output. The intake stores every line
verbatim first, is lenient where intent is unambiguous (`"2"`, `" floor "`, three timestamp formats) and quarantines the
rest with a reason. `client_ts` is recorded but never trusted: server receipt order decides.

## 13. Least privilege in the database
The app connects as `lastticket_app`: DML only, and only `INSERT`/`SELECT` on the append-only tables
(`inventory_event`, `raw_attempt`, `attempt_outcome`, `processed_message`), so history is immutable by privilege rather
than by convention (tested). Migrations run as the owner; in Kubernetes that happens in an init container, so the serving
container never holds owner credentials.

## 14. Authentication
Anonymous sessions (signed token from `POST /api/sessions`), enforced on every non-public path; operators use a static
`X-Admin-Key` compared in constant time. **Not built**: accounts, login, bot defence (see limitations).

## 15. An index removed on evidence
V2 created `inventory_event_as_of (inventory_id, occurred_at) INCLUDE (deltas)` for point-in-time queries.
`docs/explain/plans.txt` showed the planner never used it (the `(inventory_id, version)` unique index serves the
per-section form; the per-event form reads most of the stream). V3 drops it.

## 16. Stack as specified, with two notes
Java 17 / Spring Boot 3.5 / PostgreSQL 16 / Kafka 3.9 (KRaft) / Redis 7 / React 19 + TypeScript / Docker. Plain
`JdbcClient` instead of JPA: the SQL *is* the design here and should be readable. No client-side router or UI kit.

## Deliberately not built
- **Payments.** "Pay" confirms the hold. A real integration would authorise before `confirm` and capture after.
- **Seat maps / reserved seating.** Inventory is counted per (ticket type, section), which is what the brief specifies.
- **Autoscaling, service mesh, multi-region.** The 10x analysis (`docs/performance.md`) says where the ceiling is; the
  brief says not to build past it.
- **Outbox / processed_message pruning.** Both grow without bound; a retention job (or partition drop) is needed before
  a long-lived deployment. The app role has no `DELETE` on purpose, so this would be an owner-run job.
- **Queue persistence across Redis loss**, and **bot defence** for the waiting room.
