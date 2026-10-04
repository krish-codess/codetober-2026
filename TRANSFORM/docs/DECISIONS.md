# Decisions log

Each entry records what was chosen, what was rejected and why. Entries are in the order the
decisions were made, including the ones that turned out wrong and were replaced (D-08 → D-28).

---

### D-01 · Real data source: EVE Online ESI, not World of Warcraft
**Chosen:** EVE's ESI API, which is public and needs no credentials. It provides per-region order
books (a true auction-house snapshot), 13 months of daily trade history per region and item, item
metadata, and an RSS archive of dated patch notes.
**Rejected:** Blizzard's WoW auction API. It needs an OAuth client registered to a developer
account, and that secret can't ship in a reproducible portfolio repository. Its commodity endpoint
is also region-wide, so there is no per-server market to index.
**Consequence:** "Servers" are EVE **regions** (The Forge/Jita, Domain/Amarr, Sinq Laison/Dodixie,
Heimatar/Rens, Metropolis/Hek). Each has its own order book and price history, which is how game
servers behave economically.

### D-03 · Two worlds: the real economy plus a calibrated simulation
ESI has no listing-level history: you can snapshot the live order book, but nobody can fetch last
year's snapshots. Real history exists only as daily aggregates.
**Chosen:**
* **EVE (real):** an index from ESI daily trade history, plus scheduled live order-book snapshots.
* **Simulated shard:** an ESI-shaped generator producing a year of order-book snapshots with known
  ground truth. This exercises snapshot ingestion, fill inference, listing-level manipulation and
  patch shocks.
**Rejected:** a synthetic-only system, which would prove nothing about real data, and a real-only
system, which would mean a year of waiting to accumulate snapshots and no ground truth to test against.

### D-04 · The generator emits the real wire format
The simulation writes exactly the ESI order-book page shape and the EVE RSS layout, including the
defects found while profiling:

* numeric strings
* missing fields
* a second timestamp format
* duplicates
* mislabelled rows
* truncated bodies
* HTTP 502 pages
* outages
* late arrivals

`parse.py` has no knowledge of where a payload came from, so the real and synthetic paths share one
parser. It is calibrated from the real profile: price level, book depth and ask dispersion per item
(`reference/synthetic_calibration.json`).

### D-05 · Raw store: content-addressed, write-once gzip envelopes
Each envelope stores the verbatim response body plus where and when it was fetched. The file name
contains the SHA-256, files are `chmod 444`, and writes are temp file plus atomic rename. Rewriting
identical content is a no-op, so ingestion retries are safe. One bundle per (server, snapshot)
rather than one file per (server, item, snapshot) means 6k files a year instead of 280k.
**Rejected:** storing parsed rows only (a parser fix couldn't be replayed) and an object store (no
benefit on one machine; the same layout maps onto S3 keys).

### D-06 · Lake: Parquet per (dataset, world, day), scanned with DuckDB
One file per partition, replaced atomically, so a backfill is a file swap. The partition keys live
in the path. `Lake.scan` resolves only the files inside the requested date range before DuckDB sees
them; the first version globbed all partitions and then filtered, which was quadratic over a seed
(measured, fixed). The format is zstd with 64k-row row groups, since a day is 10⁴–10⁵ rows.

### D-07 · CPI divisions are curated, not taken from ESI categories
ESI's category tree puts fuel blocks under "Commodity" and PLEX under special assets, and other
items under mixed groupings. A curated `division` column in `reference/eve_universe.json` keeps the
seven divisions economically meaningful. The ESI group and category are still stored as metadata.

### D-08 · First estimator: median ask, plus a Hampel filter on raw history *(superseded by D-27, D-28)*
The first version published the per-snapshot median ask. On the real snapshot it was fine. On the
full simulated year it failed: thin servers' books filled with persistent troll asks, which never
cancel while honest asks get bought. Once trolls were half the book the median broke, and chain
linking carried a 1,000× Fuel Block price into the cross-server index (a peak value of 2.3 million).
The Hampel filter then compared against a trailing window of **raw** prices, so the captured
stretch dragged its own reference along. Kept here because the failure shaped the final design.

### D-09 · Index formula: chain-linked Laspeyres, re-weighted quarterly
**Rejected:**
* **Fisher or Törnqvist:** these need current-period quantities every day. Snapshot volumes are
  inferred and noisy, and day-level weights would make the index jitter with the fill heuristic.
* **Monthly chaining:** chain drift with noisy weights.
* **A fixed annual basket:** too stale for a game whose patches change consumption.

Laspeyres with a fixed basket per quarter is the standard CPI compromise, and it is explainable.

### D-10 · Item weight cap of 20% within a server
Without a cap, PLEX and Large Skill Injectors carry most of EVE's expenditure, and the "CPI" becomes
a PLEX price index. The cap is redistributed pro rata and is equal-weight when infeasible
(n × 20% < 100%).

### D-11 · Missing prices: cell-relative imputation, never carry-forward
A missing item's weight is carried by the observed items of its own (server, division) group, and
coverage is published. **Rejected:** last-observation-carried-forward, which fabricates a price and
hides thinness. Coverage below 50% publishes no value at all.

### D-12 · Reproducibility through append-only vintages
**Chosen:** published tables reject UPDATE and DELETE by trigger, which also binds the owner role.
A change is a new vintage with a reason, and `as_of` reads reproduce any past state.
**Rejected:** overwrite plus an audit table (the audit is optional, the overwrite is not), and full
event sourcing (heavier, with the same guarantee for this use).

### D-13 · Baskets freeze after the reference period plus a 3-day grace period
Simulated late arrivals land up to 3 days late. Freezing earlier would weight on incomplete data;
never freezing would let weights change after publication.

### D-14 · Migrations: a 90-line runner, not Alembic
Plain `NNNN_name.up.sql` and `.down.sql` files, each applied in a transaction and checksummed.
Editing an applied migration is an error, and CI runs up, down and up again. Alembic would add
SQLAlchemy only to run SQL files.

### D-15 · Three database roles
`gs_owner` runs migrations only. `gs_pipeline` can insert into published tables but cannot update
them or read API keys. `gs_api` reads, inserts patch notes and idempotency records only, and runs
in read-only transactions by default. Passwords come from the environment, and the bootstrap script
creates the roles. Tested in `test_roles_have_least_privilege`.

### D-16 · Orchestration: daily partitions per world with single-run backfills, plus a fingerprint sensor
Each (world, day) partition records the fingerprint of its raw inputs. A sensor compares these with
the raw store and requests runs only for changed day ranges plus their dependents: 14 days for the
Hampel window, and the next day for fills. Dependent days reuse their validated lake partition
instead of re-parsing raw.
**Rejected:**
* Multi-dimensional partitions: weaker single-run backfill support.
* Auto-materialisation policies: they can't see raw-file changes.

Analytics are recomputed per world after every daily run; they're cheap and not vintaged.

### D-17 · API: public reads, scoped API keys for writes, idempotency keys
The only write is recording a patch note. It requires a `patches:write` key, stored as a SHA-256
hash and created by the owner role, plus an `Idempotency-Key` header:

* the same key and body replays the stored response
* the same key with a different body returns 409

Unbounded lists use keyset cursors on stable sorts; offset pagination isn't used anywhere.

### D-18 · Frontend: React owns the DOM, D3 does the maths; no state or query library
D3 is used for scales and path geometry only. That keeps charts testable with Testing Library and
accessible (one focusable element with a keyboard model and a live readout).
**Rejected:**
* **TanStack Query:** a 90-line cache gives TTL, in-flight dedupe, ETag revalidation and explicit
  invalidation, which is all this app needs.
* **A router:** four tabs held in the query string.

API types are generated from the OpenAPI spec.

### D-19 · TypeScript pinned to 5.9
The Vite template pinned TypeScript 6. `openapi-typescript` declares a TS 5 peer dependency, so 5.9
is pinned rather than forcing the install.

### D-20 · Compose PostgreSQL on host port 5433
The development machine runs a native PostgreSQL on 5432. The port is configurable (`GS_PG_PORT`).

### D-21 · Patch tagging by keyword lists, not NLP
Patch notes are tagged with divisions by transparent keyword lists (`reference.DIVISION_KEYWORDS`).
An auditor can see exactly why a patch was linked to "fuel". Real EVE notes mention "ship" in nearly
every release, so relevance ranks tags by mention count and decays with lag.

### D-22 · EVE patch release time defaults to 11:00 UTC
The RSS archive has dates, not times. EVE deploys during the 11:00 UTC downtime, and sub-patches on
the same day are ordered by their `.N` suffix.

### D-23 · Only complete UTC days are processed
A half-ingested day would be published and then "revised" when the rest arrives, which is noise in
the vintage log. Partitions are requested only for days before today (UTC).

### D-24 · Fill inference is a heuristic with a measured error
A vanished ask is a fill if it was priced at or below the highest partial-fill price seen between
the two snapshots (buyers lift asks cheapest first), and a cancellation otherwise. Expired orders
are excluded. On the simulator's ground truth, inferred volume is within ±25% in aggregate with
log-correlation > 0.9 (`test_fill_inference_tracks_ground_truth_volume`). Volumes only drive basket
weights, never prices.

### D-25 · Dev data lives outside OneDrive
The repository sits in a OneDrive-synced folder. The ~300 MB raw store and lake go to
`%LOCALAPPDATA%` via `GS_DATA_DIR` in the uncommitted `.env`. Containers use named volumes.

### D-26 · Committed real-data snapshot (2.3 MB)
`reference/fixtures/eve_raw_2026-10-02.tar` is the real ESI pull this project was designed on.
`goldstandard seed --offline` restores it, so a clean clone reproduces the real index without
network access, and CI doesn't depend on ESI being up. Without `--offline`, live data tops it up.

### D-27 · Estimator v2: interior lower quartile *(replaces D-08)*
The per-snapshot rank is `k = max(1, floor((n-1)/4))`. Persistent manipulation sits above the
market and bait below it gets bought instantly, so a quantile below the median survives up to ~75%
high-side contamination. The `k ≥ 1` floor stops a single bait from setting the price. The provable
single-listing bound is unchanged, and the level is closer to what a buyer actually pays.

### D-28 · Day-level acceptance: cross-server consensus first, causal Hampel second *(replaces D-08)*
No single-book estimator can tell one honest ask from two trolls. The other servers can, because the
same good can't trade 4× apart for long when hauling arbitrages it.

* When at least 3 servers observed the item, consensus decides, in both directions. This rejects
  captured books and prevents lock-out after real level shifts.
* Otherwise a causal Hampel check runs against the cell's own **accepted** prices.

Both worlds use the same function (`estimators.robust_daily`). Each rule has a property or
scenario test.

### D-29 · Prices must be corroborated by trades, and only traded prices form consensus
Found by scoring the simulated year against ground truth. After D-27 and D-28 the index still
drifted about 10% for a week:
* Fuel Blocks were cornered on **two of three** small servers at once, so the consensus median
  *was* the cornered price.
* When a corner ended mid-day, the day showed volume (honest supply returning) while three of four
  snapshots still showed the 3× ask.

**Fix:**
* A price counts as traded only when the day's traded VWAP is within 1.5× of it.
* Only such prices vote in consensus.
* An uncorroborated price far from its reference is rejected.

The fixture's index error went from mean 3.3% (worst day 10.5%) to 1.0% (worst day 7.1%). On the full
simulated year it went from **2.0% to 0.32%**, and materially wrong published prices fell from 1.9% to
0.09% (PERFORMANCE.md, "Accuracy").

**Rejected:**
* Tightening the 4× consensus limit: real regions legitimately differ by up to about 2–3× on thin
  items (METHODOLOGY §2c).
* Depth-weighted consensus: depth is exactly what a corner fakes.

### D-30 · A small remnant does not impute for its group
When a group's heavy item was missing, its remaining 15% of weight set the whole group's relative.
A cornered Gila flipping between `ok` and `thin` swung the ships division ±45% from one day to the
next. A group now needs at least 50% of its weight observed to impute for itself; otherwise
imputation moves up a level.

### D-31 · Freshness measures data arrival, not index lag
The index legitimately stops for 3 days at each quarter boundary (the basket grace period, D-13).
Readiness and the stale banner therefore use the newest day with published *prices*. The UI explains
the index lag separately instead of reporting "stale" every quarter.

### D-32 · PLEX no longer trades in the regional markets
Profiling found 5 of 225 real series with no history at all: PLEX in all five regions, with empty order books too. In 2025 CCP moved
PLEX trading into a separate *Global PLEX Market* region
([announcement](https://www.eveonline.com/news/view/global-plex-market-and-friction-free-trade)),
so the five regional markets this index tracks no longer carry it. Ingesting the global region as a sixth
"server" would be a small change (one more `region_id`); it was not done because PLEX is an account
service rather than an in-game good, and the services division is already represented by skill
injectors and extractors.

PLEX stays in the item universe, because the item is real:
* it produces honest `missing` rows
* it gets no basket weight
* the API offers only items with published prices to the UI

### D-33 · Reference basket worth 1B ISK
The basket's absolute size doesn't matter for the index, but it does for labour-hours: at 1M ISK it
cost "2 minutes" of ratting. 1B ISK, about a month of play, gives hours a reader can relate to.

---

## Deliberately not built

| Not built | Why | What it would take |
|---|---|---|
| WoW Blizzard client | Credentials can't ship in a reproducible repo (D-01) | An OAuth client-credentials fetcher and a parser for the WoW auction shape into `LISTING_SCHEMA` |
| Real faucet feed for EVE | No public API; CCP's Monthly Economic Report is a PDF/CSV dump | A monthly MER importer. Until then EVE faucets are reported as *unknown*, never 0 |
| Users, tenants and SSO | Nothing per-user exists; reads are public data | An OIDC proxy in front of the API; scopes already exist |
| API rate limiting | Single-tenant demo behind nginx | An nginx `limit_req` zone, or a gateway |
| Alerting | Metrics and readiness are exposed, but there is no Alertmanager | Prometheus rules on `esi_requests_total{status!="200"}`, freshness age and `api_errors_total` |
| Horizontal scale-out | See PERFORMANCE.md "10× load" | Parallel day partitions on Dagster's multiprocess executor; partitioned PostgreSQL |
| Cloud deployment | No cloud credentials in this environment | The compose file maps one-to-one onto a single VM. See OPERATIONS.md |
