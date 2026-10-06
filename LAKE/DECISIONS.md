# Decisions

What was chosen, what was rejected, and why. Numbers quoted here were measured on the
development machine while building; the probes that produced them are described inline.

## Stack substitutions

**Avro is read and written with polars, not fastavro or DuckDB's `avro` extension.**
The brief's stack implies fastavro. Measured on one month of taxi data (2.96M rows, 19 columns):

| Library | Write | Read | Problem |
|---|---|---|---|
| fastavro 1.13 | ~11k rows/s | ~17k rows/s | A 9.5M-row variant would take ~15 min to write and ~9 min per query |
| DuckDB `avro` extension | 24 s, uncompressed only | 25-29 s per query | No codec option on write; cannot read `zstandard` |
| polars 2.0 | 3-26 s by codec | 6-11 s | None; round trip is exact (`Table.equals`) |

polars it is. Consequence: Avro codecs are `none`, `snappy`, `deflate`, which is what polars
writes. Avro-zstandard is not in the matrix.

**ORC is read through PyArrow's dataset reader**, because DuckDB has no ORC reader. DuckDB
pushes projections and filters into the Arrow scanner. The scanner honours projections and
ignores filters for stripe pruning (measured; pinned by a test). That is a property of this
reader, not of ORC, and every table in the UI names the reader next to the format.

**No fastavro, no pandas, no chart library, no ORM.** The chart is ~200 lines of SVG, which
is what made keyboard navigation and shape-per-format straightforward.

## Measurement

**Bytes read are counted by an fsspec filesystem wrapper, in a separate untimed execution.**
One mechanism covers DuckDB (registered filesystem), PyArrow (`PyFileSystem`) and polars
(file object), and it yields read-call counts as well as bytes, which the cost model prices
as GET requests. It routes I/O through Python, so it must never share an execution with a
timer. Rejected: process I/O counters (`/proc/self/io`, `GetProcessIoCounters`): no call
counts, two platform implementations, noise from unrelated reads.

**DuckDB's external file cache is disabled for counted runs.** DuckDB treats a registered
filesystem as remote and caches file data: the second query through the counter read 0 bytes.
Found by the probe, fixed with `SET enable_external_file_cache = false`, pinned by
`test_repeat_queries_are_not_served_from_an_engine_cache`.

**"Cold" is verified, not asserted.** A cold run evicts the file from the OS page cache
(`posix_fadvise(DONTNEED)` on Linux, a `FILE_FLAG_NO_BUFFERING` open on Windows) and opens a
fresh DuckDB. Whether eviction worked is measured every run (`eviction_check`: sequential
read speed cached vs just-evicted), published in the results and shown in the UI. Where it
did not work, cold means "fresh engine" and the page says so. Rejected: `drop_caches`
(needs root/privileged containers), a fresh process per run (adds nothing once the cache is
evicted and the connection is new; DuckDB keeps no cross-connection state by default).

**Repetition is budgeted.** Up to 3 cold and 5 warm runs, but a cell stops repeating a kind
of run after `LAKE_CELL_BUDGET_S` (20 s), never below 1 cold and 2 warm. Without it Avro,
at ~30 s a query, would take 4 minutes per cell and the run several hours. The number of
runs behind every median is in the results and the detail panel.

**CSV is read with the known schema and no type sniffing.** DuckDB's sniffer cost ~0.5 s
per bind in the probe and would have been charged to every CSV query. Supplying the schema
is CSV's best case; the baseline should not lose on a technicality.

**Every variant is checksummed against the source** (row count plus an order-independent
`bit_xor(hash(...))` over columns cast to contract types) before it may be benchmarked, and
every query result is compared with the answer from `clean/source.parquet` (numbers within
1e-9 relative, to allow parallel float summation; everything else exact).

**Write time is measured once per variant.** Writes take tens of seconds; repeating them
would double the run for a number that is not the headline. Stated as a limitation.

**Benchmarks do not run concurrently and cannot be started over HTTP.** A benchmark sharing
a process with a web server measures the web server.

## Data

**Source: NYC TLC yellow taxi, January-March 2024** (9.55M rows, 1.04 GB as CSV). TLC has
published Parquet, not CSV, since 2022. The raw Parquet is kept immutable with a SHA-256
manifest; stage `land` writes the one big CSV the brief asks for, in arrival order with
defects intact. Everything downstream consumes that CSV and cannot tell a real one from a
generated one.

**No silent fallback to synthetic data.** If the download fails after 5 attempts (capped
exponential backoff), the run stops and the error says to set `LAKE_SOURCE=synthetic`.
Silently swapping the dataset under a benchmark would publish numbers about the wrong data.

**Quarantine versus keep.** Rows that cannot be true are quarantined with a reason
(unparseable line, dropoff before pickup, 300,000-mile trips, pickups stamped 2002/2009,
exact duplicates). Rows that are odd but real are kept and counted (negative amounts on
voids, trips from the adjacent month, zero passengers). Dropping 1.4% of rows for being
negative would change every revenue sum. Rules live in `bakeoff/taxi.toml` next to the
reason each exists.

**Ingest refuses to finish if row accounting does not balance**: landed lines must equal
clean rows plus quarantined rows.

**Variants are sorted on the pickup timestamp.** Min/max statistics only prune when the
filter column is clustered. One extra variant (`parquet-zstd3-shuffled`) stores the same
rows in random order as the control: same query, same format, pushdown gone.

**One file per variant, no partitioning.** The experiment varies what is inside a file
(codec, row groups, stripes), so the file count is held at one. The small-chunk failure
mode is still measured: `parquet-zstd3-rg10k` has ~950 row groups and the read-call count
shows what that does to request cost. Not built: hive partitioning, multi-file datasets,
compaction. See README "Storage layout" for what the measurements imply for them.

**Rule and query SQL comes from `taxi.toml`, a reviewed file in the repository**, and is
composed into statements. Values (paths, cursors, filters, the `$lo`/`$hi` window) are
always bound parameters. Nothing from an HTTP request reaches SQL text.

## Service

**No authentication.** There is no user, tenant or mutable resource: the API serves
published measurements and a pure cost function. Adding tokens would protect nothing and
would be the only secret in the system.

**The cost model exists once, in Python.** The UI posts a workload and renders the answer;
it does not re-implement the arithmetic. The price of that is that the page needs the API,
so the "published results page" is the container, not a static site.

**Prices are a dated TOML file with sources and a `verified` note per provider.** AWS
storage, request and Athena prices were checked on 2026-10-06; several GCP and Azure
figures are recalled list prices and are labelled as such in the file and at `/api/pricing`.

**Results document is free-form JSON in OpenAPI.** Requests, errors, pagination and the
recommendation are fully typed; `/api/results` is described in prose. Typing a 60-field
nested document twice (Python and TypeScript) was judged worse than one honest gap.

## Operations

**CI workflow lives at the repository root** (`.github/workflows/lake.yml`, path-filtered
to `LAKE/**`). Sibling projects keep workflows inside their own folder, where GitHub does
not run them. A CI that never runs was the worse option.

**Benchmark data lives outside the repository and outside synced folders.** The repository
sits in OneDrive; a sync client touching multi-gigabyte files mid-benchmark corrupts
timings. `LAKE_DATA_DIR` defaults to `./data` (git-ignored) and `.env.example` says why to
move it.

**`pyproject.toml` pins every runtime dependency exactly.** The published numbers are a
property of these reader and writer versions.

## Deliberately not built

- Partitioned or multi-file layouts, compaction, table formats (Iceberg, Delta).
- Object-storage runs. Bytes and read calls are measured against a local filesystem and
  priced; no S3 request was made.
- Per-column encodings (DELTA_BINARY_PACKED, BYTE_STREAM_SPLIT), dictionary thresholds,
  page sizes, bloom filters. The matrix is codec x chunk size x layout.
- Streaming writers. `materialize` holds the dataset in memory (see README "Ceiling").
- A job queue, users, rate limiting, or any HTTP-triggered work.
- Repeated write-time measurements.
