# EXPLAIN ANALYZE

Captured by `python scripts/explain.py` against the seeded database. Do not edit by hand.

Rows at capture: unit 262,436, score 262,436, quarantine 6,988, experiment 2.

## Score page: `GET /v1/runs/{id}/scores`

Keyset pagination served by `score_ranked (run_id, split, uplift, unit_id)`, scanned backward: no sort, no offset.

```sql
SELECT unit_id, split, uplift FROM score WHERE run_id = %(run)s AND split = 'test' AND (uplift, unit_id) < (%(uplift)s, %(unit)s) ORDER BY uplift DESC, unit_id DESC LIMIT 101
```

```
Limit (actual time=0.247..0.329 rows=101.00 loops=1)
  Buffers: shared hit=6
  ->  Index Only Scan Backward using score_ranked on score (actual time=0.244..0.313 rows=101.00 loops=1)
        Index Cond: ((run_id = '2e0944fe-3b05-4107-a377-f09eeb5280ac'::uuid) AND (split = 'test'::text) AND (ROW(uplift, unit_id) < ROW('0.05870557507918303'::double precision, 'C0162800'::text)))
        Heap Fetches: 1
        Index Searches: 1
        Buffers: shared hit=6
Planning:
  Buffers: shared hit=34
Planning Time: 4.865 ms
Execution Time: 0.413 ms
```

The same query with `score_ranked` dropped (inside a transaction that is rolled back):

```
Limit (actual time=166.536..177.795 rows=101.00 loops=1)
  Buffers: shared hit=2523
  ->  Gather Merge (actual time=166.534..177.766 rows=101.00 loops=1)
        Workers Planned: 1
        Workers Launched: 1
        Buffers: shared hit=2523
        ->  Sort (actual time=84.480..84.504 rows=50.50 loops=2)
              Sort Key: uplift DESC, unit_id DESC
              Sort Method: top-N heapsort  Memory: 37kB
              Buffers: shared hit=2523
              Worker 0:  Sort Method: quicksort  Memory: 25kB
              ->  Parallel Seq Scan on score (actual time=43.788..76.504 rows=9843.00 loops=2)
                    Filter: ((run_id = '2e0944fe-3b05-4107-a377-f09eeb5280ac'::uuid) AND (split = 'test'::text) AND (ROW(uplift, unit_id) < ROW('0.05870557507918303'::double precision, 'C0162800'::text)))
                    Rows Removed by Filter: 121375
                    Buffers: shared hit=2508
Planning:
  Buffers: shared hit=4
Planning Time: 7.669 ms
Execution Time: 177.903 ms
```

## Policy export: `GET /v1/policies/{id}/export`

The top n by predicted uplift, read straight off `score_ranked` in ranking order.

```sql
SELECT unit_id, uplift FROM score WHERE run_id = %(run)s AND split = 'test' ORDER BY uplift DESC, unit_id DESC LIMIT 5000
```

```
Limit (actual time=0.350..4.030 rows=5000.00 loops=1)
  Buffers: shared hit=59
  ->  Index Only Scan Backward using score_ranked on score (actual time=0.347..2.796 rows=5000.00 loops=1)
        Index Cond: ((run_id = '2e0944fe-3b05-4107-a377-f09eeb5280ac'::uuid) AND (split = 'test'::text))
        Heap Fetches: 6
        Index Searches: 1
        Buffers: shared hit=59
Planning:
  Buffers: shared hit=25
Planning Time: 4.197 ms
Execution Time: 4.493 ms
```

The same query with `score_ranked` dropped (inside a transaction that is rolled back):

```
Limit (actual time=136.590..152.834 rows=5000.00 loops=1)
  Buffers: shared hit=2553
  ->  Gather Merge (actual time=136.588..152.038 rows=5000.00 loops=1)
        Workers Planned: 1
        Workers Launched: 1
        Buffers: shared hit=2553
        ->  Sort (actual time=83.741..84.328 rows=3080.50 loops=2)
              Sort Key: uplift DESC, unit_id DESC
              Sort Method: top-N heapsort  Memory: 977kB
              Buffers: shared hit=2553
              Worker 0:  Sort Method: top-N heapsort  Memory: 977kB
              ->  Parallel Seq Scan on score (actual time=36.567..58.543 rows=19843.50 loops=2)
                    Filter: ((run_id = '2e0944fe-3b05-4107-a377-f09eeb5280ac'::uuid) AND (split = 'test'::text))
                    Rows Removed by Filter: 111374
                    Buffers: shared hit=2508
Planning:
  Buffers: shared hit=4
Planning Time: 2.668 ms
Execution Time: 153.774 ms
```

## Served experiment: every read endpoint starts here

Served by the partial index `experiment_serving (dataset_id, finished_at DESC) WHERE status = 'complete'`. With a handful of experiments the planner may prefer a sequential scan; the index is for the table after months of scheduled retraining.

```sql
SELECT e.id FROM experiment e JOIN dataset d ON d.id = e.dataset_id WHERE d.name = 'synthetic' AND e.status = 'complete' ORDER BY e.finished_at DESC LIMIT 1
```

```
Limit (actual time=0.062..0.063 rows=1.00 loops=1)
  Buffers: shared hit=2
  ->  Sort (actual time=0.060..0.061 rows=1.00 loops=1)
        Sort Key: e.finished_at DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=2
        ->  Nested Loop (actual time=0.047..0.050 rows=1.00 loops=1)
              Join Filter: (d.id = e.dataset_id)
              Rows Removed by Join Filter: 1
              Buffers: shared hit=2
              ->  Seq Scan on dataset d (actual time=0.029..0.030 rows=1.00 loops=1)
                    Filter: (name = 'synthetic'::text)
                    Rows Removed by Filter: 1
                    Buffers: shared hit=1
              ->  Seq Scan on experiment e (actual time=0.014..0.016 rows=2.00 loops=1)
                    Filter: (status = 'complete'::text)
                    Buffers: shared hit=1
Planning:
  Buffers: shared hit=82
Planning Time: 13.060 ms
Execution Time: 0.112 ms
```

The same query with `experiment_serving` dropped (inside a transaction that is rolled back):

```
Limit (actual time=0.092..0.095 rows=1.00 loops=1)
  Buffers: shared hit=2
  ->  Sort (actual time=0.089..0.091 rows=1.00 loops=1)
        Sort Key: e.finished_at DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=2
        ->  Nested Loop (actual time=0.068..0.076 rows=1.00 loops=1)
              Join Filter: (d.id = e.dataset_id)
              Rows Removed by Join Filter: 1
              Buffers: shared hit=2
              ->  Seq Scan on dataset d (actual time=0.038..0.040 rows=1.00 loops=1)
                    Filter: (name = 'synthetic'::text)
                    Rows Removed by Filter: 1
                    Buffers: shared hit=1
              ->  Seq Scan on experiment e (actual time=0.023..0.026 rows=2.00 loops=1)
                    Filter: (status = 'complete'::text)
                    Buffers: shared hit=1
Planning:
  Buffers: shared hit=19
Planning Time: 2.267 ms
Execution Time: 0.164 ms
```

## Deciles of a run: `GET /v1/runs/{id}/deciles`

Served by the primary key `decile (run_id, decile)`.

```sql
SELECT * FROM decile WHERE run_id = %(run)s ORDER BY decile
```

```
Sort (actual time=0.070..0.073 rows=10.00 loops=1)
  Sort Key: decile
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=2
  ->  Seq Scan on decile (actual time=0.034..0.048 rows=10.00 loops=1)
        Filter: (run_id = '2e0944fe-3b05-4107-a377-f09eeb5280ac'::uuid)
        Rows Removed by Filter: 110
        Buffers: shared hit=2
Planning:
  Buffers: shared hit=52
Planning Time: 2.750 ms
Execution Time: 0.102 ms
```

## Unit lookup behind the score foreign key and the policy join

Served by the primary key `unit (dataset_id, unit_id)`.

```sql
SELECT treatment, outcome FROM unit WHERE dataset_id = %(dataset)s AND unit_id = %(unit)s
```

```
Index Scan using unit_pkey on unit (actual time=0.063..0.065 rows=1.00 loops=1)
  Index Cond: ((dataset_id = '1'::smallint) AND (unit_id = 'C0162800'::text))
  Index Searches: 1
  Buffers: shared hit=7
Planning:
  Buffers: shared hit=21
Planning Time: 0.199 ms
Execution Time: 0.085 ms
```

## Quarantine summary: `GET /v1/datasets`

Joins through the unique constraint `quarantine (raw_sha, line_no)`.

```sql
SELECT q.reason, count(*) FROM quarantine q JOIN raw_file f ON f.sha256 = q.raw_sha WHERE f.dataset_id = %(dataset)s GROUP BY q.reason
```

```
HashAggregate (actual time=19.945..19.953 rows=7.00 loops=1)
  Group Key: q.reason
  Batches: 1  Memory Usage: 32kB
  Buffers: shared hit=413
  ->  Hash Join (actual time=0.083..13.518 rows=6988.00 loops=1)
        Hash Cond: (q.raw_sha = f.sha256)
        Buffers: shared hit=413
        ->  Seq Scan on quarantine q (actual time=0.030..1.617 rows=6988.00 loops=1)
              Buffers: shared hit=412
        ->  Hash (actual time=0.028..0.030 rows=2.00 loops=1)
              Buckets: 1024  Batches: 1  Memory Usage: 9kB
              Buffers: shared hit=1
              ->  Seq Scan on raw_file f (actual time=0.017..0.019 rows=2.00 loops=1)
                    Filter: (dataset_id = '1'::smallint)
                    Rows Removed by Filter: 1
                    Buffers: shared hit=1
Planning:
  Buffers: shared hit=56 read=1
Planning Time: 4.991 ms
Execution Time: 20.037 ms
```
