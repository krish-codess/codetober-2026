# Performance, scalability and accuracy

All numbers on this page were **measured** on the reference machine. None are estimates.
Reference machine: Windows 11 laptop with 8 logical CPUs. Docker Desktop (WSL2) runs the stack, and
`bench` runs natively on Python 3.11.
Raw measurements are in `docs/perf/bench.json` (`goldstandard bench`) and the query plans in
`docs/explain/` (`goldstandard explain`).

## Data volume

| Dataset | Per day | Per year (seeded) |
|---|---|---|
| Simulated order-book listings (4 servers × 44 items × 4 snapshots) | ~17,000 | **6.8 M** |
| Raw synthetic payloads (gzip JSON bundles) | 16 | ~6,300 files, ~300 MB |
| Real ESI history rows (5 regions × 45 items) | ~225 | 85,000 (396 days fetched in one call per series) |
| Real order-book listings per snapshot | ~14,000 | — (live, every 6 h) |
| Published prices (`item_price_daily`, both worlds) | ~401 | ~160,000 |
| Published index values (88 series) | 88 | ~32,000 |

## Runtime budgets and measured throughput

The budget is what the daily schedule can afford: a day's partition must finish well inside the
6-hour ingestion interval, and a full rebuild from raw must fit a coffee break. Measured values are
marked "measured" or come from `bench.json`.

**Stage throughput** (`goldstandard bench --days 60 --servers 4`: 1.02 M simulated listings, native, single process):

| Stage | Budget | Measured |
|---|---|---|
| Feed generation (simulated ingestion) | — | 31,000 orders/s (4 processes) |
| Parse + boundary validation | ≥ 20k rows/s | **58,000 rows/s** (one native JSON pass + Polars casts) |
| Robust snapshot estimates | < 100 ms/day | **9.8 ms/day** |
| Fill inference | < 100 ms/day | **17.7 ms/day** |
| Day-level acceptance (60 days × 176 cells) | < 5 s | **0.47 s** |
| Index (60 days × 40 series) | < 1 s | **0.06 s** |

**End-to-end seed in the compose stack** (`docker compose logs seed`, Linux containers, both worlds from empty):

| World | Days | Prices + lake | Baskets + index | Analytics | Total |
|---|---|---|---|---|---|
| Simulated shard (6.8 M listings) | 397 | 256 s (0.64 s/day, including Postgres publish) | 3.9 s | 4.4 s | **~4.4 min** |
| EVE (85k history rows) | 396 | 41 s | 4.5 s | 2.3 s | **~48 s** |

Generating the year of simulated snapshots takes about 5 min, so a clean `docker compose up` reaches a
fully seeded, serving stack in **about 10 minutes** once images are built. The daily incremental
run (one partition per world) takes about 1 s, against a 6-hour budget.

## Query performance (EXPLAIN ANALYZE, `docs/explain/`)

Every hot query's predicate is served by an index; none scans its table:

| Query (endpoint) | Plan | Rows | Execution |
|---|---|---|---|
| Manipulation feed page (`/v1/manipulation`) | **Index Scan** `manipulation_event_feed`, stops at LIMIT (migration 0003 matched the index order to the ORDER BY) | 51 | 0.22 ms |
| Inflation matrix (`/v1/inflation/matrix`) | Seq scan of 40 series + **Index Scan Backward** `inflation_rate_pkey` ×40 (LATERAL … LIMIT 1) | 40 | 1.4 ms (was 18 ms with DISTINCT ON + seq scan) |
| Vintage lookup on publish (pipeline) | **Index Only Scan Backward** `item_price_daily_pkey`, one probe per staged row | 176 | 1.7 ms |
| Index series (`/v1/index`) | Bitmap scan on `index_value_pkey` + in-memory sort for DISTINCT ON | 364 | 0.4 ms |
| Purchasing-power prices | Bitmap scan on `item_price_daily_pkey` + sort | 1,098 | 3.0 ms |
| Patch timeline page (`/v1/patches`) | Bitmap scan on `patch_event_timeline` + sort | 11 | 0.05 ms |

At these result sizes (10²–10³ rows) the planner rightly prefers a bitmap scan plus a sort of a
few kB in memory over an ordered index scan. Every predicate is still served by an index, and none
of these scans the table. Table sizes at capture: 159k prices, 32k index values, 39k manipulation
events, 84k inflation rows.

## API latency

Measured through the published port with 40 sequential uncached requests per endpoint
(`bench --api`). The browser additionally caches by ETag (304s) and keeps a 60 s TTL.

| Endpoint | p50 | p95 |
|---|---|---|
| `/v1/worlds` | 33 ms | 46 ms |
| `/v1/index` (one year) | 8.7 ms | 11.9 ms |
| `/v1/patches` (100 per page) | 8.6 ms | 11.5 ms |
| `/v1/inflation/matrix` | 5.4 ms | 6.2 ms |
| `/v1/purchasing-power` | 15.8 ms | 31.0 ms |
| `/v1/manipulation` | 6.3 ms | 7.4 ms |
| `/health/ready` (real query) | 4.3 ms | 5.0 ms |

The first measurement found `/v1/worlds` at 199 ms and readiness at 67 ms. Both came from a full
scan computing freshness; it now reads the newest processed partition from `data_quality_daily`.

## The 10× ceiling: what breaks first

At 10× the data (40 servers, or hourly snapshots), each component behaves as follows:

1. **Per-day processing is single-process and sequential (breaks first for rebuilds).** It costs
   0.64 s per simulated day in the Linux containers (1.2 s natively on Windows), roughly linear in
   listings, so about 6–7 s per day at 10×. Daily operation stays fine (one day per 6 hours), but a
   full **rebuild** of a year goes from about 4.4 minutes to about 45 minutes, and the seed becomes
   the bottleneck.
   **Change:** run day partitions in parallel on Dagster's multiprocess executor. Partitions only
   depend on the previous day's last snapshot and the trailing acceptance window, so run contiguous
   chunks in parallel with a 45-day overlap. Nothing in the data model needs to change.
2. **Analytics recompute the whole world on every run.** That's O(history), and it's about 4–5 s in containers
   today for inflation, shocks, manipulation events and flows. At 10× servers there are about 10×
   more series and cells, which takes minutes per run.
   **Change:** recompute analytics only for affected days plus their trailing windows, the way
   prices already work. The fingerprints exist; the analytics tables would need per-day replace
   slices.
3. **The sensor's fingerprint scan** lists every day's raw files every 5 minutes. That's cheap
   today (6k files), and acceptable at 10× (60k), but it grows with operating time.
   **Change:** keep a high-water mark of `raw_manifest.registered_at` and fingerprint only days
   touched since then.
4. **PostgreSQL is not the bottleneck at 10×.** About 1.6 M price rows a year, with every API query
   on an index (above). Past 100×: partition `item_price_daily` and `index_value` by month.
5. **Already fixed while building:** history staging used to re-parse every historical fetch on
   every run, which is O(operating days × series). It now reads only the latest fetch per series,
   which is O(series), proven by `test_history_staging_reads_only_the_latest_fetch_and_keeps_aged_out_days`.
   Lake scans used to glob all partitions and are now O(date range).

## Accuracy against ground truth (simulated shard)

The simulator records what really happened, so the estimators can be scored and not only trusted.

From `goldstandard accuracy` on the deployed stack (`docs/perf/accuracy.json`), over 365 days and
66,273 published prices:

| Question | Result |
|---|---|
| Does the published index track the same baskets evaluated on the **true** fair prices? | mean abs log error **0.32%**, p95 0.79%, worst day 2.7%; final level 99.6 vs 97.7 |
| How many published prices are materially wrong (more than 1.75× from true fair)? | **59 of 66,273 (0.09%)**; 66% of them carry a thin-market flag |
| Published prices more than 4× from true fair | **0** |
| Injected troll listings (10^1.5–10^6×) flagged the same day | **96.8%** (596/616) |
| Injected bait listings (0.005–0.3×) flagged the same day | **95.8%** (545/569) |
| Fill inference vs true traded volume | ratio **1.004**, log-correlation **0.998** over 68k cells |

**Before the trade-corroboration rule (DECISIONS D-29)**, the same measurement gave a mean index error
of 2.0% (worst day 5.5%) and 1,264 materially wrong prices (1.9%).

**Corners** (an actor buys out a thin book and relists at 2–4×) are caught on the start day only
38% of the time. That's the wrong question, though:
* most corner days have no published price at all, because the book is thin or the price is rejected
  as uncorroborated;
* the distorted-price count above is the measure of whether corners reach the index, and it is 0.09%.

The 4% of trolls and baits that go unflagged sit in books with fewer than four listings, where
nothing is extreme relative to anything. For books with four or more listings the integration test
requires at least 95% same-day detection.
