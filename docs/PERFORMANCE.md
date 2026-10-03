# Performance, scalability and accuracy

All numbers on this page were **measured** on the reference machine. None are estimates.
<!--MACHINE-->
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

<!--THROUGHPUT-->

## Query performance (EXPLAIN ANALYZE, `docs/explain/`)

Every API query is served by an index scan that matches its predicate and sort order:

<!--EXPLAIN-->

## API latency

<!--LATENCY-->

## The 10× ceiling: what breaks first

At 10× the data (40 servers, or hourly snapshots), each component behaves as follows:

1. **Per-day processing is single-process and sequential (breaks first for rebuilds).** It costs
   about 1.2 s per synthetic day today, roughly linear in listings, so about 10–12 s per day at 10×.
   Daily operation stays fine (one day per 6 hours), but a full **rebuild** of a year goes from
   about 8 minutes to about 70 minutes, and the seed becomes the bottleneck.
   **Change:** run day partitions in parallel on Dagster's multiprocess executor. Partitions only
   depend on the previous day's last snapshot and the trailing acceptance window, so run contiguous
   chunks in parallel with a 45-day overlap. Nothing in the data model needs to change.
2. **Analytics recompute the whole world on every run.** That's O(history), and it's about 30 s
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

<!--ACCURACY-->
