# Performance

Everything here is reproducible: `loadtest/onsale.js` is the test, `loadtest/results/` holds the raw summaries quoted below.

```bash
docker compose --profile loadtest run --rm -e USERS=1000 -e VUS=500 -e ADMISSION_RATE=25 -e READ_RPS=50 k6
docker compose --profile loadtest run --rm -e USERS=10000 -e VUS=1000 -e ADMISSION_RATE=100 -e READ_RPS=100 k6
```

## Latency budget

The critical path is **placing a hold** (`POST /api/reservations`): it is the one request where a person is waiting to
find out whether they got tickets. Budget, measured at the client, while the sale is live and admission is sized to the
write capacity of the deployment:

| | p50 | p95 | p99 |
|---|---|---|---|
| place a hold | < 100 ms | < 400 ms | < 800 ms |
| availability read | – | < 150 ms | < 400 ms |

These are encoded as k6 thresholds; the test exits non-zero if they are missed, if anything is oversold, if the event
stream and the snapshot disagree, if a hold outlives its deadline, or if any unexpected 5xx is returned.

## Test bed (read this before the numbers)

One laptop: Intel i5-11320H (4 cores / 8 threads), 16 GB, Windows 11 + Docker Desktop (WSL2 VM with 8 vCPUs, 7.6 GB).
The load generator, the application, Postgres, Kafka and Redis all run in that one VM, alongside three unrelated compose
stacks that were left running, and the host was at ~74% CPU before any test started. So these numbers measure this
laptop under contention at least as much as they measure the design. They are a floor, not a claim about production.

## Results

### Sized to capacity: 1,000 users, 1,200 tickets, 25 admissions/s — all thresholds pass

`loadtest/results/summary-1000.txt`

| | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| place a hold | 21.3 ms | 97.0 ms | 191.9 ms | 334 ms | 1,437 |
| confirm | 24.3 ms | 128.4 ms | 241.5 ms | 324 ms | 590 |
| queue join / poll | 10.0 ms | 92.8 ms | 149.1 ms | 429 ms | 14,151 |
| availability read | 3.2 ms | 39.1 ms | 127.2 ms | 494 ms | 5,501 |

828 holds placed, 609 sold-out refusals, **0** `503 CONTENTION`, **0** unexpected 5xx, 6/6 invariant checks.

### The headline: 10,000 users, 1,200 tickets, 100 admissions/s — correct, but far outside the latency budget on this box

`loadtest/results/summary-10000.txt`

| | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| place a hold | 901 ms | 3,231 ms | 4,907 ms | 6,048 ms | 5,226 |
| confirm | 1,708 ms | 3,811 ms | 4,954 ms | 6,030 ms | 975 |
| queue join / poll | 924 ms | 2,874 ms | 3,820 ms | 5,616 ms | 144,956 |
| availability read | 1,491 ms | 3,169 ms | 4,141 ms | 5,625 ms | 16,172 |

182,047 requests. **Tickets oversold: 0. Snapshot/stream drift: 0. Holds left after their deadline: 0.
Unexpected 5xx: 0.** 1,369 holds were placed for 1,200 tickets over the run (expired holds went back on sale and were
re-sold); 3,810 attempts were refused as sold out (oversell-attempt rate 72.6% of reservation attempts);
68 requests got an explicit, retryable `503 CONTENTION`.

What this run does and does not show:
- It shows the correctness properties hold under 10,000 simultaneous users with the machine saturated.
- It does **not** meet the latency budget. Every endpoint, including a Redis-cached read, sat near 1 s: that is the
  signature of a saturated host, not of a slow query (see below). The latency thresholds failed and the test exited
  non-zero, as designed.
- Hold conversion fell to 34% (the script intends 70%): with seconds of latency, simulated buyers did not manage to pay
  inside a 15 s hold. The system degraded by expiring holds and re-selling the tickets, not by overselling.
- Some simulated users were still polling when the test window closed and are not in the reservation counts.

## Where the time goes

Measured with `pg_stat_statements` during a run with the gate on: the versioned `UPDATE inventory` averages **0.5 ms**,
the three inserts 0.1–1.1 ms each, and a commit on this disk is ~3.4 ms (`pgbench`, single client). The database work
for one hold is about 3 ms plus the commit. The rest of the wall time at 10,000 users is CPU scheduling delay in an
oversubscribed VM.

Two things found by measuring, and fixed (details in `docs/decisions.md` §4):
1. **Retry herd.** Without a limiter, 400 holds caused 2,298 version conflicts and starved the connection pool. A
   per-aggregate gate removed the conflicts and the starvation.
2. **Cold JVM.** The first seconds after a restart are roughly 20x slower. The same cached read had a median of 1.3–2.7 s
   on a just-started instance and 3–5 ms once warm. The load test therefore runs a warm-up scenario before the sale
   opens, and the operations guide says to do the same before a real one.

## What breaks first at 10x

**The single-row aggregate.** All writes for one (ticket type, section) are serialised through one row; throughput per
section is bounded by 1 / (transaction time), where transaction time is dominated by the commit's WAL flush. On this
laptop that is on the order of 100 holds/s per section; on a database with ~1 ms commits it is several hundred.
More application replicas do not help: they add contention on the same row.

Not built, per the brief; the specific change would be:
1. **Shard hot sections into stock buckets** (e.g. `GA/FLOOR` → N inventory rows of `total / N`), route each request to
   a bucket by hash of the user id, and fall back to the others when one runs dry. N-fold write throughput with the
   same code path; the cost is that the last few tickets can be stranded across buckets, which needs a rebalancing
   command.
2. If that is not enough, **group commit per aggregate**: one writer per section drains a queue and commits many holds
   in one transaction (one event each, one WAL flush for all).

The next ceilings after that, in order: the single outbox relay (partition the outbox by key hash, one relay per
partition); queue polling load (replace polling with server-sent events, or lengthen `pollAfterMs`); Postgres
connections (PgBouncer in transaction mode).

**The control that already exists is the admission rate.** It is per event, and the right value is "what the write path
commits within budget", which the first table above measures for a given deployment.
