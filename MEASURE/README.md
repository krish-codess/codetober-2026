# Your App, Wrapped

A personalised year-in-review for every user of a product, where every number is true.

A batch pipeline turns a year of usage events into per-user aggregates, ranks each user against the
population with quantile sketches, picks the most interesting things that are genuinely true about
each person, and serves the result as an animated story with share cards rendered on the server.

The demonstration product is public GitHub activity (the [GH Archive](https://www.gharchive.org/)
event feed): real hours for profiling and a real-data run, and a generated year in the same format
for scale.

| | |
|---|---|
| ![Story card on a phone](docs/screenshots/phone-2-card.png) | ![Share sheet with the server-rendered card](docs/screenshots/desktop-4-share.png) |

More in [docs/screenshots](docs/screenshots/): the intro, a card, the summary, the share sheet and
the public share page, each at desktop and phone size, captured by the end-to-end test.

## What "true" means here

"Top 5%" is the kind of sentence that is easy to get almost right. This system gets it exactly right,
by construction and then by checking:

- A quantile sketch per metric proposes about 100 thresholds. One hash aggregation then counts
  **exactly** how many users clear each one. A claim is built from that exact count over the exact
  population, compared in integers against a fixed ladder. A user in the top 5.2% is told "top 10%".
  The sketch decides where thresholds sit; it never decides whether a claim is true.
  ([mart_metric_cutpoints.sql](dbt/models/marts/mart_metric_cutpoints.sql), [cards.py](wrapped/cards.py))
- Before a run can be published, an audit rebuilds every payload, compares every number on every
  card with the warehouse, and re-checks every claim against an exact rank from a full sort.
  One violation and nothing is published. ([build.py](wrapped/build.py))
- Things the data cannot support are not said: commits (the feed stopped carrying commit counts in
  October 2025, so the cards count pushes), "night owl" (there is no timezone; the card says "14:00
  UTC"), "newcomer" (one year of data cannot know).

## Evidence for each requirement

| Requirement | How it is met | Evidence |
|---|---|---|
| Percentiles without sorting the user base per user | One t-digest per metric, one hash aggregation for exact bucket counts, a 100-element lookup per user. No sort of the population anywhere in the pipeline. | [mart_metric_cutpoints.sql](dbt/models/marts/mart_metric_cutpoints.sql), [mart_user_ranks.sql](dbt/models/marts/mart_user_ranks.sql); 1.6 million (user, metric) values ranked inside a 111 s `dbt build` at 200,000 users |
| Only true facts | Exact counts, integer ladder comparison, truncated shares, and an audit that gates publishing. | `test_claim_never_rounds_in_the_users_favour`, `test_claim_holds_for_every_count_in_a_population`, `test_the_audit_catches_a_lie`, dbt test `assert_ranks_are_exactly_true`; 422,831 claims audited at 200,000 users and 192,330 on real hours, 0 violations in both |
| Different superlatives per user | Rarity scoring, one card per family, per-user tie-breaking. | At 200,000 users: 2,850 distinct sets of cards, the most common held by 12.2% of users, all 19 selectable card types in use ([benchmark](docs/evidence/benchmark.md)). `test_stories_are_spread_out` fails the build if one story exceeds 25%. |
| Low-activity users handled gracefully | Three tiers; a quiet user is never shown a bare count or a comparison they lose, and still gets a real superlative when one exists. | `test_quiet_user_gets_a_story_not_a_shortfall`, `test_quiet_user_still_gets_a_true_rare_fact`; 42.9% of the generated population is in the minimal tier |
| Never expose another user's data | No user id in any route; k-anonymity on thresholds; payloads carry no threshold, group size or other account; shares are a one-card snapshot. | `test_story_is_the_token_holders_and_only_theirs`, `test_no_route_accepts_a_user_id`, `test_no_comparison_rests_on_fewer_than_k_people`, `test_no_threshold_value_or_group_size_reaches_the_payload`, `test_share_is_retry_safe_and_public_view_is_one_card` |
| Whole user base in a practical batch window | 200,000 users and 6.4 million events go from raw files to published in 13.5 minutes on a laptop with DuckDB capped at 1 GB. | [docs/evidence/benchmark.md](docs/evidence/benchmark.md) |

## Architecture

```mermaid
flowchart LR
    subgraph sources[Sources]
        GH[GH Archive<br/>hourly .json.gz]
        GEN[wrapped generate<br/>same format, planted defects]
    end
    subgraph batch[Batch: python -m wrapped run-all]
        RAW[(raw/<br/>immutable)]
        ING[ingest<br/>validate every line]
        BR[(bronze Parquet)]
        Q[(quarantine Parquet<br/>+ reason)]
        MAN[(manifest Parquet)]
        DBT[dbt build on DuckDB<br/>staging, intermediate, marts<br/>sketch thresholds + exact counts]
        WH[(warehouse.duckdb)]
        SEL[build<br/>selector + narrative]
        PAY[(payloads Parquet<br/>one row per user)]
        AUD{audit<br/>exact re-check}
        PUB[publish<br/>COPY + activate, one txn]
    end
    subgraph serve[Serving]
        PG[(PostgreSQL<br/>runs, payloads, views, shares)]
        API[FastAPI<br/>signed links, share cards via Pillow]
        CARDS[(rendered cards)]
        EDGE[nginx as CDN<br/>static UI, cached cards]
        UI[React + Framer Motion<br/>story, share sheet, admin]
    end
    GH --> RAW
    GEN --> RAW
    RAW --> ING --> BR --> DBT
    ING --> Q
    ING --> MAN
    DBT --> WH --> SEL --> PAY --> AUD
    AUD -- 0 violations --> PUB --> PG
    AUD -. any violation .-> STOP([stop: nothing published])
    PG <--> API
    API --> CARDS
    EDGE --> API
    UI --> EDGE
```

| Layer | Where | Notes |
|---|---|---|
| Ingest | [wrapped/ingest.py](wrapped/ingest.py) | Raw files are never touched. Each batch writes accepted events, a quarantine with reasons, and a manifest row per file; the manifest is the commit marker. |
| Modelling | [dbt/](dbt/) | `stg_`, `int_`, `mart_` layers, 78 nodes of models and tests. Incremental models rebuild any UTC day a late batch touched. |
| Selection and words | [wrapped/cards.py](wrapped/cards.py) | Pure functions: catalogue, scoring, selection, copy. |
| Batch generation and audit | [wrapped/build.py](wrapped/build.py) | One payload per user to Parquet; then the audit. |
| Publish and rollback | [wrapped/publish.py](wrapped/publish.py) | A run is loaded and activated in one transaction; rollback flips one row. |
| API | [wrapped/api.py](wrapped/api.py) | Contract in [docs/openapi.json](docs/openapi.json), generated from the code; CI fails if it drifts. |
| Share cards | [wrapped/render.py](wrapped/render.py) | 1200x630 PNG drawn from the same card dict the story uses. |
| UI | [web/src](web/src) | Story, share sheet, admin analytics. |

## Run it

### With Docker (the whole system, one command)

```bash
cd MEASURE
cp .env.example .env        # then replace every CHANGE_ME
docker compose up --build
```

First start generates a synthetic year for 20,000 users, runs the whole batch, publishes it and
starts the API and the UI. Then:

```bash
docker compose run --rm batch links     # personal links, a few per tier; open one
open http://localhost:8080/#admin       # analytics; sign in with WRAPPED_ADMIN_TOKEN
```

> **Status of this path.** The Dockerfile, compose file and edge configuration are written and reviewed but have
> **not been run**: Docker Desktop's engine would not start on the machine this was built on. The CI workflow's
> `stack` job runs exactly this path (compose up, [verify.sh](scripts/verify.sh), the browser journey, the rollback
> drill) and has not run yet either, because nothing has been pushed. Everything below it was run for real.

### Without Docker

Needs Python 3.11, Node 22 and a PostgreSQL you can create a database in.

```bash
cd MEASURE
python -m venv .venv && . .venv/bin/activate      # Windows: py -3.11 -m venv .venv; .venv\Scripts\activate
pip install -e ".[dev]"
export WRAPPED_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/wrapped   # an existing, empty database

python -m wrapped migrate                 # schema and roles
python -m wrapped generate --users 3000   # a synthetic year (or: python -m wrapped fetch 2025-03-12-15)
python -m wrapped run-all                 # ingest, dbt build, payloads, audit, publish
python -m wrapped links                   # personal links to open
python -m wrapped serve                   # API on http://127.0.0.1:8000

cd web && npm ci && npm run dev           # UI on http://localhost:5173, proxying to the API
```

With one database URL set, all three roles (migrate, batch, API) use it. That is the development
shortcut; the compose stack gives each its own user.

### Commands

Every stage is its own command, idempotent, and runnable alone.

| Command | Does |
|---|---|
| `generate --users N --seed S` | Synthetic year into `raw/`, with a ledger of planted defects. |
| `fetch HOUR...` | Download real GH Archive hours (`2025-03-12-15`) with capped, backed-off retries. |
| `profile` | Profile the raw files into [docs/data-profile.md](docs/data-profile.md). |
| `ingest` | New raw files into bronze, quarantine and manifest. A second run is a no-op. |
| `transform [--full-refresh]` | `dbt build`: models and tests. |
| `build` | One payload per user. Same inputs, same run id, no work. |
| `audit` | Check every payload against the warehouse. Exit 1 on any violation. |
| `publish` / `rollback` | Activate the built run / serve the previous one again. |
| `run-all` | ingest, transform, build, audit, publish, in order; stops at the first failure. |
| `links` | Print personal links (what a product would email). |
| `serve`, `migrate`, `openapi PATH` | API, migrations, API reference. |

## Data

Real hours were profiled before any schema was written: [docs/data-profile.md](docs/data-profile.md).
What that changed:

| Observed in the real feed | Consequence |
|---|---|
| 20 to 31% of events come from `[bot]` logins; `github-actions[bot]` alone is up to 23% | Automation is excluded from the population ([int_users.sql](dbt/models/intermediate/int_users.sql)). |
| `PushEvent` payload lost `size`, `distinct_size`, `commits` between June and November 2025 | Cards count pushes, never commits. |
| New payload keys and a new event type (`DiscussionEvent`) appear during the year | Only stable fields are extracted; unknown types are accepted as `other`. |
| 54 to 66% of actors in an hour have exactly one event; the busiest has 35,831 | The generator's heavy tail; the low-activity tiers. |
| Lines up to 229,288 characters | Line buffer of 4 MB in ingest. |
| Hour suffix is not zero-padded; a missing hour is an error document | The fetcher's naming and its length and gzip checks. |
| A cut-off download is an unreadable gzip | File-level rejection, recorded in the manifest, without stopping the batch. |
| The archive answers 403 to Python's default User-Agent | The fetcher identifies itself; 4xx is not retried. |

The real hours contained no malformed lines, no duplicate ids within an hour and no nulls. Those
defects are planted by the generator at stated rates, because a year-long feed has them:

| Defect | Rate per event | What the pipeline does |
|---|---:|---|
| Redelivered event (same id, same or next file) | 0.2% | Kept in bronze, deduplicated in `int_events__deduped`. |
| Late arrival (hours to weeks later, across the year boundary) | 0.4% | Incremental models rebuild the affected days. |
| Timestamp spelled differently (`+00:00`, millis, `-07:00`) | 0.5% | Parsed to UTC. |
| Missing actor / missing id | 0.05% / 0.02% | Quarantined with a reason. |
| Unparseable or far-future timestamp | 0.02% / 0.01% | Quarantined with a reason. |
| Line cut off mid-object | 0.02% | Quarantined with a reason. |
| User renamed mid-year | 0.5% of users | Identity is the id; the latest login is shown. |
| Previous-year events in this year's first files | a few | Kept in bronze, excluded from the year. |

`test_quarantine_matches_the_defects_that_were_planted` asserts the quarantine counts equal the
generator's ledger, reason by reason, and that every raw line is either accepted or quarantined.

Full table and column reference: [docs/data-dictionary.md](docs/data-dictionary.md).

### Real-data run

Four real hours of 2025 were fetched and run through the same batch, unpublished
([benchmark, run 2](docs/evidence/benchmark.md)): 570,995 lines, all accepted; one file rejected whole (a
download cut off part-way); 866 accounts and 25% of events excluded as automation; 159,498 people; 192,330
claims audited with 0 violations. Three hours is not a year: 91% of those accounts have fewer than five
events, and one set of cards reaches 33.7% of them, against 12.2% on a full generated year.

## Performance

Measured on the development laptop (Windows 11, 16 GB RAM with about 1 GB free during the run,
DuckDB capped at 1 GB and 4 threads, data on a local SSD). Full numbers and the raw stage report:
[docs/evidence/benchmark.md](docs/evidence/benchmark.md).

| Stage | Volume | Budget | Measured | Throughput |
|---|---|---:|---:|---:|
| ingest | 8,814 files, 6,427,250 lines | 5 min | 175 s | 36,700 lines/s |
| transform (dbt build, 78 nodes) | 6,406,568 events after dedup | 5 min | 111 s | 57,900 events/s |
| build | 199,956 payloads | 5 min | 142 s | 1,410 users/s |
| audit | 422,831 claims | 5 min | 143 s | 1,400 payloads/s |
| publish | 199,956 payloads, 1,161,386 card rows | 5 min | 238 s | 840 users/s |
| **total** | | **30 min** | **13.5 min** | |

Re-running any stage with nothing new costs under a second (ingest, build) or about a minute (dbt, mostly
start-up). Serving queries are single-digit milliseconds or less at this size; plans are in
[docs/evidence/explain-analyze.md](docs/evidence/explain-analyze.md). That file is also where two mistakes were
caught: a pagination query that sorted the whole run (588 ms, now 0.3 ms) and a secondary index the planner
never used (dropped in migration 0003).

API latency under load was not measured.

### What breaks first at 10x

At 2 million users and 64 million events, the three single-process Python stages go first: build, audit and
publish are 523 of the 809 seconds, all linear in users, each on one core, and publish holds one transaction
open for all of it (about 40 minutes at 10x, with the WAL that implies). The change: partition users by
`user_id % N`, run N build and audit workers writing N Parquet parts, COPY the parts over N connections into the
new run (rows are keyed by run, so loaders do not conflict), and keep activation as the single final step.

This is an extrapolation from one measured size, not a measurement. Second in line, also unmeasured: the
first-load deduplication (a window over every event) spilling to disk under a 1 GB cap. Scale already broke
things once on the way to 200,000 users; what broke and how it was fixed is in the
[benchmark notes](docs/evidence/benchmark.md).

## API

Reference generated from the code: [docs/openapi.json](docs/openapi.json) (the API also serves it at `/docs`).

| Route | Auth | |
|---|---|---|
| `GET /v1/wrapped` | personal link token | The story for the user the token names. ETag revalidation; `private, no-cache`. |
| `PUT /v1/wrapped/views/{card_type}` | token | Record a view. Idempotent. |
| `POST /v1/wrapped/shares` | token | Share one card. A retry returns the same share (200 instead of 201). |
| `GET /v1/shares/{id}` | public | That one card. |
| `GET /v1/shares/{id}/card.png` | public | 1200x630 PNG, versioned URL, cacheable forever. |
| `GET /s/{id}` | public | Landing page with Open Graph and Twitter card tags. |
| `GET /v1/admin/analytics/superlatives` | admin token | Superlative distribution across users. |
| `GET /v1/admin/analytics/share-rate` | admin token | Share rate by card type. |
| `GET /v1/admin/payloads?limit&cursor` | admin token | Keyset-paginated listing. |
| `GET /healthz`, `/readyz`, `/metrics` | none | Liveness; readiness (queries the active run and writes a rendered probe to the card store); Prometheus. |

Every error has one shape, `{"error": {"code", "message", "request_id", "details"?}}`, including
validation failures. The request id is in every log line for that request and in the `X-Request-ID`
header.

## Behaviour under failure

| Dependency | Failure | Behaviour |
|---|---|---|
| GH Archive | Timeout, 5xx, short read | Retry with backoff 2, 4, 8, ... capped at 60 s, 5 attempts, then fail the command. Partial files never get their final name. 404 is logged as a gap. Other 4xx fails at once. |
| Raw file | Truncated or not gzip | Rejected whole, recorded in the manifest with the error, batch continues. |
| Raw line | Fails validation | Quarantined with a reason and the raw text. Never dropped. |
| dbt test | Any failure | `dbt build` stops; nothing downstream runs. |
| Audit | Any violation | `run-all` stops before publish. |
| PostgreSQL, during publish | Unreachable | Connect retries 1, 2, 4, 8, 10 s. Load and activation are one transaction: all or nothing. |
| PostgreSQL, while serving | Unreachable | API starts anyway. Data routes return `503 database_unavailable` with `Retry-After: 5`; `/readyz` returns 503; `/healthz` stays 200. |
| Card store | Not writable | Share is still created; the image route returns `503 card_unavailable`; the edge serves a cached copy if it has one. |
| API, from the UI | Unreachable | Up to 4 attempts with backoff shown to the user, then a retry button with the request id. A previously loaded story stays on screen, labelled as a saved copy. |

## Security

- Input is validated at every boundary: raw lines in ingest, request bodies, path and query
  parameters by schema, tokens by signature.
- Every SQL statement that takes request data is parameterised. The f-strings in ingest and build
  interpolate module constants and paths only (the linter's `S608` rule is on; exceptions are
  listed per file in [pyproject.toml](pyproject.toml)).
- Secrets come from the environment. The process refuses to start outside `WRAPPED_ENV=dev` with the
  development secrets.
- Three database users: migrations, batch (cannot read views or shares), API (cannot write payloads
  or change the active run). `test_api_credentials_cannot_rewrite_a_story...` proves it against a
  real server.
- The personal link token lives in the URL fragment, is removed from the address bar on load, and is
  kept in session storage only.
- Story responses are `private, no-cache, Vary: Authorization` (the end-to-end test caught an earlier
  version being replayed by the browser cache to a different token).
- Containers run as non-root with all capabilities dropped; the API's filesystem is read-only apart
  from its data volume.

## Tests

```bash
pytest                                   # unit + pipeline; add WRAPPED_TEST_ADMIN_URL=postgresql://... for the API tests
cd web && npm test                       # component tests
cd web && WRAPPED_E2E_TOKEN=... npm run e2e   # browser journey against a running stack (see playwright.config.ts)
```

| Suite | Count | What it protects |
|---|---:|---|
| [tests/test_cards.py](tests/test_cards.py) | 23 | The ladder never rounds in the user's favour (every count in a 1,237-person population), tiers, family limits, quiet users, truncation, determinism, tokens, card rendering. |
| [tests/test_pipeline.py](tests/test_pipeline.py) | 13 | The real batch on a seeded year: quarantine equals planted defects, dedup, UTC, bots excluded, k-anonymity, zero audit violations, the audit catching a planted lie, story spread, idempotent re-runs, late and redelivered events counted once (incremental equals full refresh), unreadable files, capped fetch retries, dbt naming. |
| dbt data tests | 60+ | Uniqueness, not-null, relationships, accepted values, plus four singular tests: layer reconciliation, per-user invariants, exact threshold counts, exact ranks. |
| [tests/test_api.py](tests/test_api.py) | 32 | Against real PostgreSQL: each route's success, validation, authorization and malformed-input paths; cross-user isolation; idempotency; XSS escaping; pagination; least privilege; constraints; publish idempotency and rollback; migrations down and up; database-down behaviour. |
| [web/src/App.test.tsx](web/src/App.test.tsx) | 15 | Loading progress, keyboard and focus, share flow and its failure, empty, unauthorized, error with backoff, cache, stale, partial payloads, admin. |
| [web/e2e/journey.spec.ts](web/e2e/journey.spec.ts) | 2 x 2 viewports | Open a link, every card by keyboard, share, the real PNG at 1200x630, the public page as a stranger, no horizontal overflow, no 5xx. |

Data is seeded everywhere (`seed=11, users=500` in the fixtures), so a failure reproduces exactly.

## Operations

**Deploy.** `docker compose up --build -d`. Order is enforced by health and completion conditions:
database, migrations, batch, API, edge. Verify from outside:
`bash scripts/verify.sh http://HOST:8080 <user-token> <admin-token>` (25 checks).

**New data or a re-run.** `docker compose run --rm batch run-all`. Readers see the old run until
the new one is fully loaded and audited.

**Rollback (what users see).** `docker compose run --rm batch rollback`. Instant: one row.
`bash scripts/rollback-drill.sh` performs publish, rollback and verification against the running
stack.

**Rollback (code).** Images are tagged; redeploy the previous tag. Schema: `alembic downgrade -1`
(every migration has a tested downgrade).

**Backup.** State worth backing up is small: the `shares` and `card_views` tables
(`pg_dump -t shares -t card_views`) and the raw directory. Everything else (bronze, warehouse,
payloads, rendered cards) is derived and rebuilt by `run-all`. Restore: `pg_restore`, then `run-all`.

**Changing card copy.** Bump `CATALOGUE_VERSION` in [cards.py](wrapped/cards.py) so the change gets a
new run id; otherwise `publish` sees the old run as already loaded.

**Observability.** JSON logs with a correlation id (request id for the API, run id for the batch).
Metrics at `/metrics`: requests by route and status, latency histogram, card render time, shares
created by card type, database-unavailable count. Each batch stage appends its duration and result
to `run_report.jsonl` in the data directory.

## Limitations

- **Not deployed, and the container path is unverified.** There were no cloud credentials, and Docker would
  not start locally. What was verified ran as local processes against a real PostgreSQL 18: the batch, the API
  (25 outside-in checks with [verify.sh](scripts/verify.sh)), and the browser journey. Compose, the nginx edge
  cache and the rollback drill script are untested until CI or a working Docker runs them.
- **The last code changes were not re-run through the PostgreSQL test suite.** After the 32 API tests passed,
  three things changed: the pagination and distribution queries were rewritten, migration 0003 was added, and
  the story's cache headers were changed. The new SQL was executed (it is what `explain.py` ran) and the browser
  journey passed after the header change, but the machine ran out of memory and the test database went down
  before the suite could be repeated. The 13 pipeline tests were likewise not repeated after build and audit
  were changed to streaming merges, though the batch itself was then run end to end at 3,000, 159,498 and
  199,956 users with a clean audit each time. Run `pytest` with `WRAPPED_TEST_ADMIN_URL` before trusting them.
- **The year is synthetic.** The feed's shape per hour is calibrated on real hours; how a person's activity
  spreads over a year is assumed. Percentile cut-offs on a real year would differ.
- **Automation detection is a login suffix and a daily ceiling.** Low-volume automation without a bot label
  stays in the population.
- **Days and hours are UTC.** A streak can be broken or made by a timezone the feed does not carry.
- **Comparison rungs stop at 25%.** A user who is top 30% at something is told nothing about it.
- **One year, one product, one locale.** Copy is English; dates are day-month.
- **No rate limiting, no token revocation.** A leaked personal link works until it expires (90 days).
- **Share cards have two layouts to keep in step** (CSS and Pillow).
- **A share keeps the card as it was when shared**, even after a later run changes that user's numbers,
  until they share it again.
- **The UI bundle is 119 kB gzipped**, most of it the animation library.

## Decisions

[DECISIONS.md](DECISIONS.md): what was chosen, what was rejected, and what was deliberately not built.
