# Index evidence: EXPLAIN ANALYZE

Captured from the seeded development database (1,500 simulated ships, 12 hours) on PostgreSQL 18.3 / PostGIS 3.6.2, warm cache, on a 16 GB laptop. Every index in the schema appears here with the query that justifies it.

## Table sizes

```
positions           454483 rows  100 MB
quarantine           17737 rows  6256 kB
vessel_latest         1402 rows  15 MB
vessels               1402 rows  256 kB
dark_events            622 rows  384 kB
coverage_cells         537 rows  328 kB
```

## 1. Live map viewport: `vessel_latest_geom_gist`

Serves `GET /api/v1/vessels/live?bbox=`.

```
Limit (actual time=0.489..0.504 rows=62.00 loops=1)
  Buffers: shared hit=77
  ->  Sort (actual time=0.486..0.493 rows=62.00 loops=1)
        Sort Key: mmsi
        Sort Method: quicksort  Memory: 29kB
        Buffers: shared hit=77
        ->  Bitmap Heap Scan on vessel_latest (actual time=0.326..0.434 rows=62.00 loops=1)
              Recheck Cond: (geom && '0103000020E61000000100000005000000000000000000594000000000000014C0000000000000594000000000000014400000000000805B4000000000000014400000000000805B4000000000000014C0000000000000594000000000000014C0'::geometry)
              Heap Blocks: exact=48
              Buffers: shared hit=77
              ->  Bitmap Index Scan on vessel_latest_geom_gist (actual time=0.274..0.275 rows=62.00 loops=1)
                    Index Cond: (geom && '0103000020E61000000100000005000000000000000000594000000000000014C0000000000000594000000000000014400000000000805B4000000000000014400000000000805B4000000000000014C0000000000000594000000000000014C0'::geometry)
                    Index Searches: 1
                    Buffers: shared hit=29
Planning:
  Buffers: shared hit=35
Planning Time: 1.361 ms
Execution Time: 3.979 ms
```

## 2. One vessel's track: `positions_pkey (mmsi, ts)`

Serves `GET /api/v1/vessels/{mmsi}/track`, and is the idempotency key of the sink.

```
Limit (actual time=0.644..0.729 rows=410.00 loops=1)
  Buffers: shared hit=193
  ->  Sort (actual time=0.642..0.677 rows=410.00 loops=1)
        Sort Key: ts
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=193
        ->  Bitmap Heap Scan on positions (actual time=0.140..0.496 rows=410.00 loops=1)
              Recheck Cond: ((mmsi = 477001198) AND (ts > '2026-10-09 15:30:00+05:30'::timestamp with time zone))
              Heap Blocks: exact=188
              Buffers: shared hit=193
              ->  Bitmap Index Scan on positions_pkey (actual time=0.085..0.085 rows=410.00 loops=1)
                    Index Cond: ((mmsi = 477001198) AND (ts > '2026-10-09 15:30:00+05:30'::timestamp with time zone))
                    Index Searches: 1
                    Buffers: shared hit=5
Planning:
  Buffers: shared hit=28
Planning Time: 0.380 ms
Execution Time: 0.786 ms
```

## 3. Neighbours heard near a vanished vessel: `positions_geog_gist` AND `positions_ts_brin`

The detector's proof that a receiver was up: other vessels within 20 km during the first 30 minutes of a silence. The planner intersects the spatial index with the 24 kB time BRIN.

```
Aggregate (actual time=66.376..66.379 rows=1.00 loops=1)
  Buffers: shared hit=225
  ->  Sort (actual time=66.322..66.334 rows=112.00 loops=1)
        Sort Key: mmsi
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=225
        ->  Bitmap Heap Scan on positions (actual time=17.540..66.263 rows=112.00 loops=1)
              Recheck Cond: ((ts > '2026-10-09 19:01:02+05:30'::timestamp with time zone) AND (ts <= '2026-10-09 19:31:02+05:30'::timestamp with time zone))
              Rows Removed by Index Recheck: 1910
              Filter: ((mmsi <> 477001198) AND st_dwithin(geog, '0101000020E61000003D0AD7A3709D4B4048E17A14AEC73940'::geography, '20000'::double precision, true))
              Rows Removed by Filter: 6728
              Heap Blocks: lossy=125
              Buffers: shared hit=225
              ->  BitmapAnd (actual time=2.761..2.763 rows=0.00 loops=1)
                    Buffers: shared hit=68
                    ->  Bitmap Index Scan on positions_ts_brin (actual time=0.190..0.191 rows=3490.00 loops=1)
                          Index Cond: ((ts > '2026-10-09 19:01:02+05:30'::timestamp with time zone) AND (ts <= '2026-10-09 19:31:02+05:30'::timestamp with time zone))
                          Index Searches: 1
                          Buffers: shared hit=8
                    ->  Bitmap Index Scan on positions_geog_gist (actual time=2.548..2.548 rows=5662.00 loops=1)
                          Index Cond: (geog && _st_expand('0101000020E61000003D0AD7A3709D4B4048E17A14AEC73940'::geography, '20000'::double precision))
                          Index Searches: 1
                          Buffers: shared hit=60
Planning:
  Buffers: shared hit=62
Planning Time: 4.987 ms
Execution Time: 69.719 ms
```

## 4. Ranked events page: `dark_events_rank (score DESC, id DESC)`

Serves `GET /api/v1/dark-events` and its keyset cursor.

```
Limit (actual time=0.211..0.290 rows=51.00 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using dark_events_rank on dark_events (actual time=0.206..0.277 rows=51.00 loops=1)
        Index Cond: (ROW(score, id) < ROW('60.1'::real, '300'::bigint))
        Heap Fetches: 0
        Index Searches: 1
        Buffers: shared hit=4
Planning:
  Buffers: shared hit=21
Planning Time: 0.468 ms
Execution Time: 0.390 ms
```

## 5. Coverage cells in view: `coverage_cells_geom_gist`

```
Bitmap Heap Scan on coverage_cells (actual time=0.080..0.087 rows=18.00 loops=1)
  Recheck Cond: (geom && '0103000020E61000000100000005000000000000000000594000000000000014C0000000000000594000000000000014400000000000805B4000000000000014400000000000805B4000000000000014C0000000000000594000000000000014C0'::geometry)
  Heap Blocks: exact=1
  Buffers: shared hit=3
  ->  Bitmap Index Scan on coverage_cells_geom_gist (actual time=0.044..0.044 rows=18.00 loops=1)
        Index Cond: (geom && '0103000020E61000000100000005000000000000000000594000000000000014C0000000000000594000000000000014400000000000805B4000000000000014400000000000805B4000000000000014C0000000000000594000000000000014C0'::geometry)
        Index Searches: 1
        Buffers: shared hit=2
Planning:
  Buffers: shared hit=19
Planning Time: 0.448 ms
Execution Time: 0.148 ms
```
