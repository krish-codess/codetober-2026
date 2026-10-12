# Evidence: latency, throughput and frame rate

All numbers were measured on the development machine: a 16 GB Windows laptop (Intel Iris Xe graphics)
with under 1 GB of memory free and other workloads running, single-node Kafka 4.1.2 and PostgreSQL 18.3 /
PostGIS 3.6.2 on the same machine. They are a floor, not a best case.

## Ingest latency

**Critical path:** a sentence arrives on a provider socket -> appended to the archive -> decoded and
validated -> acknowledged by Kafka -> consumed -> committed in PostGIS, where the API can serve it.

**Budget:** p95 of 2 seconds from receive to stored, at the expected load of up to 1,000 position reports
per second (a global AISStream subscription delivers a few hundred per second).

**Measurement:** `python -m ghostfleet loadtest --rate N --seconds 30`, with one `ghostfleet sink` process
running. The tool generates real NMEA sentences at a fixed rate into an archive, runs the normal pump, and
computes `ingested_at - received_at` per row in SQL after every row has been confirmed stored.

| target rate (reports/s) | sent | stored | p50 | p95 | p99 | max | within budget |
|---|---|---|---|---|---|---|---|
| 250 | 7,494 | 7,494 | 0.12 s | 0.75 s | 0.96 s | 1.25 s | yes |
| 500 | 14,995 | 14,995 | 0.18 s | 0.83 s | 1.01 s | 1.19 s | yes |
| 1,000 | 29,998 | 29,998 | 0.46 s | 0.87 s | 1.02 s | 1.30 s | yes |
| 2,000 | 59,753 | 59,753 | 0.80 s | 1.26 s | 1.51 s | 1.55 s | yes |
| 3,000 | 89,566 | 89,566 | 13.8 s | 18.7 s | 19.2 s | 20.3 s | **no: saturated** |
| 4,000 | 118,589 | 118,589 | 28.2 s | 35.8 s | 36.6 s | 37.3 s | **no: saturated** |

Nothing was lost at any rate: past saturation the archive and Kafka absorb the excess and latency grows
instead. `ingested_at` is taken at insert, a few milliseconds before the transaction commits, so true
visibility is marginally later than shown.

## Where the ceiling is

The single sink process saturates between 2,000 and 3,000 rows per second. Its measured cost is about
0.5 ms per row: roughly 0.3 ms in three SQL statements (insert into `positions` with a GiST and a BRIN
index and a foreign key, upsert of `vessel_latest`, vessel anti-join) and the rest in Python (JSON decode,
validation, array building).

**At 10x the budgeted load (10,000 reports/s) the sink breaks first.** The change that would be needed, not built:

1. Run one sink per Kafka partition (the topic has 12; the consumer group already balances them). This is
   configuration, and lifts the ceiling until PostgreSQL itself is the limit.
2. Then the `positions` table: partition it by day so each day's GiST index stays in memory, and write
   with `COPY` into a staging table followed by one `INSERT ... ON CONFLICT`.
3. The collector's pump is next: decoding is single-threaded Python at roughly 3,000 to 4,000 sentences
   per second per provider. One pump process per provider, or a compiled decoder, removes that.

## Map frame rate

**Claim:** thousands of moving vessels render without dropped frames.

**Measurement:** `web/e2e/journey.spec.ts`, third test. The page is loaded with `?bench=20000`, which fills
the fleet buffers with 20,000 synthetic moving vessels. Every frame, every vessel is dead-reckoned in
JavaScript and the position buffer is re-uploaded to the GPU, exactly as with live data. The test zooms and
pans while recording every `requestAnimationFrame` interval for about 14 seconds.

| vessels | frames | p50 | p95 | p99 | max | frames over 33 ms |
|---|---|---|---|---|---|---|
| 20,000 | 1,022 | 13.9 ms | 14.1 ms | 14.2 ms | 69.6 ms | 2 |

That is about 72 frames per second, held: 2 of 1,022 frames exceeded 33 ms, both at the start of an
interaction. Renderer: ANGLE on Intel Iris Xe (Direct3D 11), headless Chrome.

How: positions live in one `Float32Array` drawn as a deck.gl binary attribute, so a frame costs one loop
over a flat buffer and one buffer upload, with no per-vessel objects and no React render.

## Query latency

See [explain.md](explain.md) for `EXPLAIN ANALYZE` of every indexed query. On 454,483 positions, warm:
viewport query 4 ms, one vessel's track 0.8 ms, neighbours-heard 70 ms, ranked events page 0.4 ms.
