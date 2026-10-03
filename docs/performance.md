# Performance: budgets and measurements

Machine: Windows 11 laptop, 8 logical CPUs, Python 3.11, DuckDB 1.5.6, PostgreSQL 16 in
Docker Desktop, repository on a OneDrive-synced disk. It is a slow and noisy machine
(Hypothesis takes ~4 ms to generate a list of six integers here); treat the numbers as
an upper bound. Every figure below was produced by `tydlc run` and read from its
`report.json`. Timings vary by about ±25% between identical runs on this machine.

## Volume and budget

One run, at the CI setting (`--seed 28 --max-examples 200`):

| | |
|---|---|
| Datasets generated | 200 |
| Input rows generated | 1,237 (at most 6 per table, 3 tables) |
| Properties judged per dataset | 110 (13 declared + 97 discovered) |
| Verdicts recorded | 22,000 (9 KB of Parquet) |
| Pipeline executions | ~800 in the sweep (three properties re-execute), plus shrinking |
| Properties shrunk | 40 |

| Stage | Budget | DuckDB | PostgreSQL |
|---|---|---|---|
| Discover (ingest seeds, infer candidates) | 2 s | 0.5 s | 0.7 s |
| Sweep (generate + judge) | 60 s | 23–34 s | 15 s |
| Aggregate (CSV → Parquet → tallies) | 2 s | 0.2 s | 0.35 s |
| Shrink 40 failures, 8 processes | 120 s | 80–96 s | 68 s |
| **Total** | **180 s** | **104–131 s** | **84 s** |

Sweep throughput: 6–9 datasets/s on DuckDB, 14 datasets/s on PostgreSQL.

The DuckDB range is two runs of the same command. The PostgreSQL column and the lower
DuckDB figure were measured before the last change to the runner (generating all
datasets before evaluating them), which does not change the amount of work; the upper
DuckDB figure is after it. Shrinking with a single process was measured earlier in
development at 200–270 s for 37 failures, against 58–80 s with 8.

Other measured runs: 100 datasets inside the Linux container took 91 s (DuckDB) and
59 s (PostgreSQL); 30 datasets on PostgreSQL through the API took 59 s, of which 55 s
was shrinking. Shrink cost does not depend on the number of datasets, so it dominates
small runs.

## What was slow, and what fixed it

Each of these was measured before and after.

| Problem | Before | After | Fix |
|---|---|---|---|
| DuckDB's Python client attempts `import pandas` twice per bound parameter; when pandas is absent each attempt is an uncached filesystem search | 19 ms per INSERT | 2.7 ms | A `None` entry in `sys.modules` so the import fails instantly |
| Hypothesis `Phase.explain` after shrinking | 32 s per shrink | 1.6 s | Phase disabled for shrink searches |
| Strategies built inside the draw, `unique_by` rejection sampling | 18 ms per dataset | 5 ms | Static strategies, foreign keys as indexes, post-hoc dedupe |
| 37 sequential shrinks | 200–270 s | 58–80 s | Process pool |

Tried and rejected:

- **Prepared statements on DuckDB** (8 ms → 1.8 ms per model query). The cached plan
  keeps statistics from prepare time and fails once the data changes ("Perfect hash
  aggregate: aggregate group 4 exceeded total groups 4"). A harness that can return
  wrong answers is worse than a slow one.
- **Replacing tables instead of DELETE** to avoid tombstones: no measurable gain.

## Where the time goes now

One pipeline execution is 11 statements (3 deletes, 3 inserts, 5 model selects) at
15–30 ms on DuckDB, almost all of it per-statement planning of CTE-heavy views over a
handful of rows. PostgreSQL is faster per execution here because psycopg prepares
repeated statements automatically. Hypothesis itself costs about 5 ms per dataset.

## What breaks first at 10x

- **10x datasets per run (2,000):** the sweep, which is single-process and linear:
  about 4–6 minutes on DuckDB. The change: partition the already-generated dataset
  list across the same process pool the shrinker uses. Generation is already separate
  from evaluation, so the split is mechanical.
- **10x properties (1,000 candidates):** shrinking, at roughly 2 s of CPU per falsified
  property per core. The change: group falsified properties whose verdicts are
  identical on every example and shrink one representative per group; in a jaffle run
  that would cut 40 searches to about 15.
- **10x API traffic:** connection setup. Each request opens its own PostgreSQL
  connection. The change: `psycopg_pool` in `api.db()`.
- **10x concurrent runs through the API:** CPU. Runs execute inside the API process,
  each with its own pool of shrink processes. The change: a job table and a separate
  worker service, which also makes runs survive an API restart.
- **10x history (20,000 runs):** nothing measured breaks. The catalog query is bounded
  to 20 runs of history per property and the failure list is keyset-paginated; both
  are index-served (`explain_analyze.md`).
