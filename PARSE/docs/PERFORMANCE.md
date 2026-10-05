# Performance: budgets and measurements

Hardware for every number here: one laptop, 8 logical cores, 16 GB RAM, no GPU; PostgreSQL in
Docker Desktop (WSL2). Budgets were written before measuring. Sources: `reports/audit.json`,
`model_versions.metrics`, `docs/explain/`, `docs/evidence/`.

## Volume

| | Seeded | Design target |
|---|---|---|
| Feed lines | 56,685 | 10⁵–10⁶ |
| Pool (labelable) items | 10,733 | 10⁴–10⁵ |
| Held-out items | 45,528 (3,794 sentences × 12 languages) | — |
| Taxonomy nodes | 241 | hundreds |
| Labelled items | hundreds to low thousands | ≤ 50k |
| Label rows | 157,823 | 10⁶ |

## Stage budgets vs measured

| Stage | Budget | Measured | Verdict |
|---|---|---|---|
| Download corpus (252 files, 38 MB, 8 parallel) | 2 min | 19 s | ok |
| Download embedding model (118 MB) | 2 min | 65 s | ok |
| Build feed file (56,685 lines) | 30 s | 11 s | ok |
| Ingest: validate, store raw, quarantine, insert, reference labels | 2 min | **71 s** (≈ 800 lines/s) | ok — was > 14 min before two fixes, see below |
| Embed (CPU, int8 ONNX, batch 64) | 15 min | 77–122 texts/s → **≈ 10–12 min** for 56k | ok, and the first thing to break at 10× |
| Load embeddings from cache into the database | 1 min | 25 s (56,372 rows) | ok |
| Retrain at 300 labels: load / fit + calibrate / evaluate on 45k / rescore 10k | 60 s | 5.5 / 4.1 / 13.6 s; **26 s** end to end | ok |
| Fit + calibrate at 1,000 / 3,000 / 10,733 labels (host) | 2 min | 19 / 14 / 61 s | ok |
| Classifier inference (embedding → label set) | 1,000 items/s | 14,498 items/s | ok |
| Uncertainty scoring of the pool | 10 s | 17,240 items/s → 0.6 s | ok |
| Embedding one text at request time (`/health` probe) | 50 ms | 3 ms | ok |
| Queue page (`GET /queue`, 20 items) query | 10 ms | 3.4 ms | ok |
| Clean boot, synthetic seed, images built | 2 min | 40 s | ok |
| Backup (`pg_dump -Fc`) of the seeded database | — | 102 MB | — |

## Things that were slow, and what fixed them

| Symptom | Cause | Fix | Before → after |
|---|---|---|---|
| Ingest of 2.7k rows took 18 s | `executemany` = one network round trip per row through Docker's network | `INSERT … SELECT FROM unnest(arrays)`, 5k rows per statement | 18 s → < 1 s |
| Ingest of the real file never finished | `UPDATE … FROM (aggregate)` on rows inserted in the same transaction: no statistics, planner chose a nested loop over a 45k-row aggregate; the app role cannot `ANALYZE` | resolve duplicates in memory, apply with one primary-key `UPDATE` | > 14 min → part of the 71 s |
| Training spent 60 s loading data | `GROUP BY f.id, e.vec` grouped on a 1.5 kB `bytea` | aggregate labels per item first, then join vectors | ~50 s → 5.5 s |
| Fit took 67–227 s in the container, 6 s on the host | OpenBLAS thread pool contention across ~1,200 tiny fits | `OPENBLAS_NUM_THREADS=1` in the image | 227 s → 12.6 s (benchmark), 67 s → 4.1 s (real retrain) |
| Queue page took 76 ms and ignored its index | anti-join against `annotations` forced scan + sort | delete the prediction row when an item is annotated | 76 ms → 3.4 ms |

All five were found by running the real corpus or the real container, not by the test fixture.

## Indexes

Every non-constraint index, with `EXPLAIN (ANALYZE, BUFFERS)` output for the query it serves, with
and without the index: [`docs/explain/`](explain/README.md). Two indexes failed that test and were
removed by migration `0002`.

## What breaks first at 10×

Embedding throughput. Details and the specific change for each ceiling: [OPERATIONS.md](OPERATIONS.md#scale-what-breaks-first-at-10).
