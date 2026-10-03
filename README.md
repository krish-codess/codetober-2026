# tydlc: test your data like code

Property-based testing for data pipelines. `tydlc` generates thousands of hostile but
referentially valid rows, runs a real transformation pipeline over them, checks
properties of the *transformation* (not fixed expected outputs), discovers the
invariants the pipeline actually relies on, and shrinks every failure to the smallest
dataset that still breaks.

Pointed at [dbt-labs/jaffle-shop-classic](https://github.com/dbt-labs/jaffle-shop-classic),
unmodified, it found four real bugs that the project's own tests and sample data
cannot see. Details and minimal cases: [docs/findings.md](docs/findings.md).

| # | Bug | Engine | Minimal reproducing input |
|---|---|---|---|
| 1 | Unpaid order gets NULL amounts, violating jaffle's own `not_null` tests | both | 1 customer, 1 order, 0 payments |
| 2 | `amount / 100` truncates cents | PostgreSQL | one 1-cent payment |
| 3 | Order total depends on physical row order (float money) | DuckDB | three payments: 1, 2, −1 cents |
| 4 | Same input, bit-different output on re-execution | DuckDB | state-dependent |

![Minimal reproducing dataset](docs/screenshots/2-minimal-dataset.png)

## Run it

Needs Docker. Nothing else.

```sh
cp .env.example .env
docker compose up --build
```

This starts PostgreSQL, applies migrations, records one real run per engine (about
three minutes, so the viewer has data), and serves:

- http://localhost:8028 — failure viewer and invariant catalog
- http://localhost:8028/docs — API reference, generated from the code
- http://localhost:8028/healthz — checks PostgreSQL and DuckDB for real

Start another run over HTTP (the key is `TYDLC_API_KEY` from `.env`):

```sh
curl -X POST localhost:8028/api/runs -H 'content-type: application/json' \
  -H 'X-API-Key: change-me-api-key' -H 'Idempotency-Key: my-first-run-1' \
  -d '{"engine": "postgres", "seed": 7, "max_examples": 100}'
```

### Without Docker

Python 3.11+. The property gate needs no database at all.

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
tydlc run                                         # DuckDB, seed 28, 200 datasets
```

```
jaffle on duckdb: seed 28, 200 datasets, 1237 rows, ...

  ok   declared   pipeline_does_not_crash
  ok   declared   orders_one_row_per_raw_order
  ok   declared   customers_one_row_per_raw_customer
  FAIL declared   orders_amount_not_null  fails 75% of examples; minimal case 2 rows  [known bug]
  FAIL declared   orders_method_amounts_not_null  fails 75% of examples; minimal case 2 rows  [known bug]
  ok   declared   cents_conserved
  ...
  FAIL declared   row_order_invariant  fails 11% of examples; minimal case 5 rows  [known bug]
  ...
discovery: 60/97 seed-inferred candidates survived (hit rate 0.6186)
gate: pass
```

Every stage runs on its own and can be re-run safely:

| Command | Stage |
|---|---|
| `tydlc profile` | Profile the raw CSVs (Markdown) |
| `tydlc ingest [--src DIR]` | Validate raw CSV → staged Parquet + quarantine Parquet |
| `tydlc discover` | List invariant candidates inferred from the seed data |
| `tydlc run [--engine duckdb\|postgres] [--seed N] [--max-examples N] [--store]` | Sweep, shrink, gate. Exit 1 if the gate fails |
| `tydlc migrate [--to N]` | Apply migrations, or roll back to version N |
| `tydlc serve` | API + viewer |
| `tydlc openapi` | Print the API contract |

## How it works

```mermaid
flowchart TD
    subgraph Inputs
        SCHEMA["Schema definition<br/>(tables, keys, FKs, accepted values)"]
        SEEDS["Real seed CSVs<br/>(immutable)"]
        MODELS["Pipeline SQL<br/>(jaffle-shop dbt models)"]
    end

    SEEDS --> INGEST["ingest: validate at the boundary"]
    SCHEMA --> INGEST
    INGEST --> STAGED[("staged Parquet")]
    INGEST --> QUAR[("quarantine Parquet<br/>+ reason")]

    SCHEMA --> GEN["generators (Hypothesis)<br/>hostile values, valid references"]
    STAGED --> DISC["discovery<br/>templates that hold on real data"]
    MODELS --> ENGINE["engine: DuckDB or PostgreSQL<br/>temp tables + views"]
    DISC --> PROPS["properties<br/>declared + discovered"]

    GEN -->|"N datasets, fixed seed"| SWEEP["sweep<br/>every example x every property"]
    ENGINE --> SWEEP
    PROPS --> SWEEP
    SWEEP --> OUT[("outcomes Parquet<br/>one verdict per example x property")]
    OUT -->|"DuckDB aggregate"| TALLY["failure frequency<br/>confidence per invariant"]
    SWEEP -->|"falsified"| SHRINK["shrink (process pool)<br/>Hypothesis + row-minimality pass"]
    TALLY --> REPORT["report.json + gate verdict"]
    SHRINK --> REPORT

    REPORT -->|"exit code"| CI["GitHub Actions"]
    REPORT -->|"--store"| PG[("PostgreSQL<br/>runs, properties, results, failures")]
    PG --> API["FastAPI"]
    API --> UI["Viewer: failures, minimal dataset,<br/>invariant catalog"]
```

Module boundaries (`src/tydlc/`):

| Module | Responsibility | Depends on |
|---|---|---|
| `schema.py` | Declarative source schema | nothing |
| `generators.py` | Schema → Hypothesis strategy | schema |
| `engine.py` | Run the SQL models over a dataset | schema, a DB driver |
| `properties.py` | `Property`, `Ctx`, comparison helpers | schema |
| `discovery.py` | Invariant templates and candidate selection | properties |
| `ingest.py` | CSV boundary validation, quarantine, profile | schema, DuckDB |
| `runner.py` | Sweep, tallies, shrink, report, gate | all of the above |
| `store.py` | PostgreSQL: migrations and every query | psycopg only |
| `api.py` / `cli.py` | Interfaces | runner, store |
| `subjects/jaffle/` | The pipeline under test and its properties | properties, schema |

**Properties are functions of (input, output)**, for example "total order dollars × 100
equals total raw payment cents", or the metamorphic "adding a $1.00 payment changes
exactly one order by exactly 1.00". None compares against a stored expected result.
They live in [`subjects/jaffle/__init__.py`](src/tydlc/subjects/jaffle/__init__.py).

**Discovery** instantiates seven templates over every model column, keeps those that
hold on jaffle's real seed data (97 candidates), and runs them like any other property.
60 survive adversarial data; 37 are falsified, each with its own shrunk counterexample.
Confidence is `1 − 3/n` (rule of three) over the n examples that exercised it.

**The gate.** Four bugs exist in a pipeline this project does not own, so they are
baselined in `known_bugs` with their explanation. CI fails if a declared property fails
that is not baselined, or if a baselined bug stops failing.

## Evidence for each requirement

| Requirement | Where it is shown |
|---|---|
| Adversarial data that keeps referential integrity | `tests/test_generators.py`: 150 generated datasets checked for unique keys, valid FKs, accepted values; and that orders with no payment, odd cents, refunds, NULL and empty names are all produced |
| Shrinks to a minimal case automatically | `test_failure_is_shrunk_to_the_minimal_dataset`: bug 1 arrives as exactly 1 customer + 1 order + 0 payments. All 40 falsified properties in a run are shrunk; the largest minimal case is 5 rows |
| Properties over transformations | `subjects/jaffle/__init__.py`; no fixture files exist in the repository |
| Discovers candidate invariants | `tydlc discover`; `tests/test_discovery.py` shows discovery re-derives jaffle's declared dbt tests from data alone |
| CI time budget, fixed seed | [docs/performance.md](docs/performance.md). `test_same_seed_reproduces_exactly_in_a_fresh_process` compares two separate processes field by field |
| Finds a real bug | [docs/findings.md](docs/findings.md): four |

## Using it in CI

`.github/workflows/ci.yml` runs lint, type check, tests (with PostgreSQL as a service
container), the properties on both engines, and the package and image builds. The
properties step is this repository's own composite action:

```yaml
- uses: actions/checkout@v4
- uses: <owner>/<repo>@v0.1.0
  with:
    engine: duckdb
    seed: "28"
    max-examples: "200"
```

`pytest` alone also runs every declared property as an ordinary Hypothesis test
(`tests/test_properties.py`), with the known bugs as strict xfails.

## API

Generated from the code: live at `/docs`, committed as
[docs/openapi.json](docs/openapi.json) (a test fails if the two differ).

| Endpoint | |
|---|---|
| `GET /api/runs` | Runs, newest first. Cursor-paginated |
| `POST /api/runs` | Start a run. Needs `X-API-Key` and `Idempotency-Key`; a replay returns the same run with 200 instead of 202 |
| `GET /api/runs/{id}` | One run |
| `GET /api/runs/{id}/invariants` | Invariant catalog with confidence and 20-run history. Cursor-paginated |
| `GET /api/failures` | Falsified properties, filter by `run_id` / `property`. Cursor-paginated |
| `GET /api/failures/{id}` | One failure with its minimal dataset |
| `GET /api/analytics/failure-frequency` | Share of generated examples violating each property |
| `GET /api/analytics/discovery-hit-rate` | Per run: candidates proposed vs survived |
| `GET /healthz`, `GET /metrics` | Dependency health; Prometheus metrics |

Errors always have one shape, never a stack trace:

```json
{"error": {"code": "validation_error", "message": "the request did not match the schema",
           "correlation_id": "3b89...", "details": [{"field": "body.engine", "problem": "..."}]}}
```

Codes: `bad_cursor` 400, `unauthorized` 401, `not_found` 404, `validation_error` 422,
`database_unavailable` 503, `internal_error` 500.

## Viewer

| | |
|---|---|
| ![Failures](docs/screenshots/1-failures.png) | ![Catalog](docs/screenshots/3-catalog.png) |

Captured by the end-to-end test (`SCREENSHOT_DIR=docs/screenshots pytest tests/test_e2e.py`),
which drives the journey above with the keyboard and at 375 px width
([phone screenshot](docs/screenshots/4-catalog-phone.png)).

States handled: loading (`aria-busy`), no runs, no failures, no filter matches, server
error with correlation id and retry, run still in progress (auto-refresh every 5 s),
run failed, and stale data (a failed refresh keeps the last good data and says when it
is from). GETs are cached for 30 s; Refresh invalidates. Status is always text plus a
symbol, never colour alone. Light and dark schemes.

## Tests

```sh
docker compose up -d db
docker compose exec db createdb -U tydlc_owner tydlc_test      # once
export TEST_DATABASE_URL=postgresql://tydlc_owner:change-me-owner@localhost:5438/tydlc_test
pytest                                  # add E2E_BROWSER_CHANNEL=msedge|chrome to use an installed browser
node --test tests/js/app.test.mjs       # frontend component tests, Node 22+
```

Without `TEST_DATABASE_URL` the PostgreSQL tests skip and say why. Without a browser
(`playwright install chromium`) the end-to-end tests skip and say why.

| File | Covers |
|---|---|
| `test_generators.py` | Generated data is valid, hostile, shrinkable, reproducible |
| `test_properties.py` | Each property on generated data; each property's logic on hand-built outputs |
| `test_discovery.py` | Templates, vacuity, candidates from the real seed |
| `test_runner.py` | Confidence, cascade, row minimality, crash handling; a full real run; cross-process reproducibility |
| `test_ingest.py` | Quarantine reasons, idempotency, late-arriving parents, data tests on real seed output |
| `test_store.py` | Migration up/down/up, constraints, idempotent writes, keyset paging, backoff |
| `test_api.py` | Every endpoint: success, validation, auth, malformed, not found, database outage |
| `test_e2e.py` | Browser journey, error and empty states |
| `test_findings.py` | Reproduces finding 4 |
| `js/app.test.mjs` | Escaping, NULL vs `""` rendering, filters, cache and stale fallback |

## Operations

**Configuration** is environment only; see [.env.example](.env.example). No secret is
in the repository.

**Failure behaviour**

| Dependency down | CLI `tydlc run` | API |
|---|---|---|
| PostgreSQL (results) | Retries 0.5, 1, 2, 4 s, then warns and runs anyway: the gate still works, the run is just not recorded | `/healthz` 503 `degraded`; data endpoints 503 `database_unavailable`; the viewer shows the error with a retry button, or the last good data marked stale |
| PostgreSQL (as engine under test) | Fails fast with the connection error after the same retries | The run is recorded as `failed` with the error |
| Pipeline SQL error on generated data | Not a crash: recorded as a violation of `pipeline_does_not_crash` and shrunk | same |
| API process dies mid-run | n/a | The run is marked `failed` ("abandoned") on the next start, after an hour |

**Security.** Input validated by Pydantic at the HTTP boundary and by `ingest` at the
file boundary. Every SQL value is a bound parameter; the only interpolated identifiers
come from the in-code schema. The API and runner connect as `tydlc_app`
(SELECT/INSERT/UPDATE and TEMP; no DDL, no DELETE); only `migrate` uses the owner. The
container runs as a non-root user. Compose binds ports to loopback.

**Observability.** JSON logs on stderr, every line carrying a `correlation_id` (the
`X-Request-ID` for requests, echoed in responses and error bodies; the run key for
runs). `/metrics`: request counts and latency by route template, runs by outcome, run
duration.

**Deploy.** The deliverable is a Python package plus a GitHub Action. Tag `vX.Y.Z` and
push: `.github/workflows/release.yml` checks the tag against `pyproject.toml`, builds,
publishes to PyPI by trusted publishing, attaches the wheel to a GitHub release, and
pushes the image to GHCR. One-time setup: add the repository as a trusted publisher on
PyPI and create the `pypi` environment.

**Rollback.**
- Package: `pip install tydlc==<previous>`; consumers of the action pin `@v<previous>`.
- Service: set the previous image tag and `docker compose up -d`.
- Schema: `tydlc migrate --to <N>` runs the `.down.sql` files in reverse order, each in
  a transaction. Roll the schema back *before* the code if the old code cannot read
  the new schema.

**Backup.** Results: `docker compose exec db pg_dump -U tydlc_owner -Fc tydlc > tydlc.dump`,
restore with `pg_restore -U tydlc_owner -d tydlc --clean tydlc.dump`. Everything under
`TYDLC_DATA_DIR` is derived and can be regenerated from the seeds and a seed number.

**Index evidence.** [docs/explain_analyze.md](docs/explain_analyze.md). Reproduce:

```sh
docker compose exec db createdb -U tydlc_owner tydlc_explain
MIGRATION_DATABASE_URL=postgresql://tydlc_owner:change-me-owner@localhost:5438/tydlc_explain tydlc migrate
docker compose exec -T db psql -U tydlc_owner -d tydlc_explain -q < docs/explain_analyze.sql
```

## What was verified, and what was not

Done on the development machine (Windows 11, Docker Desktop) and recorded here:

- `docker compose up --build` from empty volumes: migrations applied, both seed runs
  recorded by the least-privilege role, API healthy, viewer serving, a run started
  over HTTP completed.
- Wheel built, installed into a clean virtualenv outside the source tree, and run.
- Package rollback rehearsed: installed 0.1.1 (a throwaway local build), then
  `pip install tydlc==0.1.0`, confirmed the version and that it runs.
- Schema rollback performed (`migrate --to 0`, then `migrate`) and covered by a test.

Not done: this repository has not been pushed, so the GitHub Actions workflows have
never executed on GitHub, and nothing has been published to PyPI or GHCR. Those need
the owner's accounts. The workflows run the same commands as above.

## Limitations

- **One subject.** The engine, generators, runner and discovery take any `Subject`,
  but only jaffle is defined, and adding one means writing Python. There is no dbt
  project loader.
- **SQL pipelines only**, expressed as views over source tables, on DuckDB or
  PostgreSQL. No incremental models, snapshots, macros beyond `ref`, or Python models.
- **Small datasets by design.** At most 6 rows per table per dataset. That finds logic
  bugs; it does not find bugs that need volume (skew, memory, timeouts).
- **Three column types**: integer, text, date. No timestamps, time zones, decimals,
  JSON or arrays, which is where a lot of real hostility lives.
- **The generator only produces contract-valid input.** Orphaned keys, duplicate keys
  and NULLs in required columns are handled at the ingest boundary and tested there,
  not thrown at the pipeline.
- **A baselined bug must be found on every run.** Bug 3 fails on 1–19% of datasets.
  The seeds and budgets used in CI find it; a much smaller budget or an unlucky seed
  would report it as "no longer fails" and fail the gate.
- **Reproducibility is per entry point.** Hypothesis mixes constants from loaded local
  modules into generation. The same `tydlc run` command gives identical data in every
  process and on both engines (tested); a run started through the API draws a
  different sample from the same seed, though it shrinks to the same minimal cases.
- **Discovery cannot tell a meaningful invariant from a coincidence.** Hit rate says
  how many candidates survive, not how many matter. The templates are fixed, so an
  invariant outside them (a conditional one, say) is never proposed.
- **Minimal means row-minimal.** No row can be removed. Values are whatever Hypothesis
  converged on; that is not proved minimal.
- **Finding 4 does not gate** and is not reproducible from a seed.
- **Runs started over HTTP are not durable** (in-process background task) and there is
  no connection pool. See the performance doc for where this breaks.
- **Single API key**, no users, no rate limiting.

Further reading: [DECISIONS.md](DECISIONS.md) ·
[docs/data_dictionary.md](docs/data_dictionary.md) · [docs/profile.md](docs/profile.md) ·
[docs/performance.md](docs/performance.md)
