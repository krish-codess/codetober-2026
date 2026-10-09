# EXPLAIN ANALYZE for the serving queries

Captured by `scripts/explain.py` on PostgreSQL 18.3. Active run: 199,956 payloads, 1,161,386 payload cards; 1 run(s) loaded. Timings are from a laptop and matter only relative to each other.

## Story lookup: `GET /v1/wrapped`

Served by the primary keys `active_runs(year)` and `wrapped_payloads(run_id, user_id)`.

```
Nested Loop (actual time=0.133..0.135 rows=1.00 loops=1)
  Buffers: shared hit=8 read=1
  ->  Nested Loop (actual time=0.039..0.040 rows=1.00 loops=1)
        Join Filter: (r.run_id = a.run_id)
        Buffers: shared hit=2
        ->  Seq Scan on active_runs a (actual time=0.025..0.026 rows=1.00 loops=1)
              Filter: (year = '2025'::smallint)
              Buffers: shared hit=1
        ->  Seq Scan on generation_runs r (actual time=0.010..0.010 rows=1.00 loops=1)
              Buffers: shared hit=1
  ->  Index Scan using wrapped_payloads_pkey on wrapped_payloads p (actual time=0.091..0.091 rows=1.00 loops=1)
        Index Cond: ((run_id = r.run_id) AND (user_id = 20896655))
        Index Searches: 1
        Buffers: shared hit=6 read=1
Planning:
  Buffers: shared hit=62
Planning Time: 0.696 ms
Execution Time: 0.189 ms
```

## Card-in-story check: `PUT /v1/wrapped/views/{card_type}`

Served by the unique constraint `payload_cards(run_id, user_id, card_type)`.

```
Nested Loop (actual time=0.286..0.289 rows=1.00 loops=1)
  Buffers: shared hit=3 read=3
  ->  Seq Scan on active_runs a (actual time=0.021..0.022 rows=1.00 loops=1)
        Filter: (year = '2025'::smallint)
        Buffers: shared hit=1
  ->  Index Only Scan using payload_cards_run_id_user_id_card_type_key on payload_cards c (actual time=0.262..0.262 rows=1.00 loops=1)
        Index Cond: ((run_id = a.run_id) AND (user_id = 20896655) AND (card_type = 'summary'::text))
        Heap Fetches: 1
        Index Searches: 1
        Buffers: shared hit=2 read=3
Planning:
  Buffers: shared hit=24
Planning Time: 0.327 ms
Execution Time: 0.316 ms
```

## Payload listing page: `GET /v1/admin/payloads`

Keyset pagination on the primary key `wrapped_payloads(run_id, user_id)`: no offset, no sort.

```
Limit (actual time=0.097..0.172 rows=51.00 loops=1)
  Buffers: shared hit=15
  InitPlan 1
    ->  Seq Scan on active_runs (actual time=0.033..0.035 rows=1.00 loops=1)
          Filter: (year = '2025'::smallint)
          Buffers: shared hit=1
  ->  Index Scan using wrapped_payloads_pkey on wrapped_payloads p (actual time=0.094..0.155 rows=51.00 loops=1)
        Index Cond: ((run_id = (InitPlan 1).col1) AND (user_id > 20896655))
        Index Searches: 1
        Buffers: shared hit=15
Planning:
  Buffers: shared hit=6
Planning Time: 0.449 ms
Execution Time: 0.227 ms
```

## The same page as first written, joining to `active_runs` (kept as the reason for the rewrite)

With the run id arriving through a join, the planner cannot use the key's order: it scans and sorts the run.

```
Limit (actual time=429.196..444.798 rows=51.00 loops=1)
  Buffers: shared hit=6389 read=35814 written=1
  ->  Gather Merge (actual time=429.194..444.789 rows=51.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=6389 read=35814 written=1
        ->  Sort (actual time=317.496..317.505 rows=51.00 loops=3)
              Sort Key: p.user_id
              Sort Method: top-N heapsort  Memory: 31kB
              Buffers: shared hit=6389 read=35814 written=1
              Worker 0:  Sort Method: top-N heapsort  Memory: 32kB
              Worker 1:  Sort Method: top-N heapsort  Memory: 28kB
              ->  Hash Join (actual time=17.260..300.560 rows=33325.67 loops=3)
                    Hash Cond: (p.run_id = a.run_id)
                    Buffers: shared hit=6373 read=35814 written=1
                    ->  Parallel Seq Scan on wrapped_payloads p (actual time=16.307..277.222 rows=33325.67 loops=3)
                          Filter: (user_id > 20896655)
                          Rows Removed by Filter: 33326
                          Buffers: shared hit=6370 read=35814 written=1
                    ->  Hash (actual time=0.911..0.912 rows=1.00 loops=3)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=3
                          ->  Seq Scan on active_runs a (actual time=0.893..0.894 rows=1.00 loops=3)
                                Filter: (year = '2025'::smallint)
                                Buffers: shared hit=3
Planning Time: 0.257 ms
Execution Time: 444.873 ms
```

## Share lookup: `GET /v1/shares/{share_id}`

Served by the primary key `shares(share_id)`.

```
Seq Scan on shares (actual time=0.095..0.096 rows=0.00 loops=1)
  Filter: (share_id = 'AAAAAAAAAAAAAAAAAAAAAA'::text)
Planning:
  Buffers: shared hit=12 read=2
Planning Time: 0.611 ms
Execution Time: 0.134 ms
```

## Share rate by card type: `GET /v1/admin/analytics/share-rate`

Two small aggregates over `card_views` and `shares`. No index: every row of the year is read by design.

```
Hash Left Join (actual time=0.076..0.101 rows=19.00 loops=1)
  Hash Cond: (t.card_type = s.card_type)
  Buffers: shared hit=1
  ->  Hash Left Join (actual time=0.056..0.071 rows=19.00 loops=1)
        Hash Cond: (t.card_type = v.card_type)
        Buffers: shared hit=1
        ->  Seq Scan on card_types t (actual time=0.031..0.036 rows=19.00 loops=1)
              Filter: shareable
              Rows Removed by Filter: 2
              Buffers: shared hit=1
        ->  Hash (actual time=0.013..0.014 rows=0.00 loops=1)
              Buckets: 1024  Batches: 1  Memory Usage: 8kB
              ->  Subquery Scan on v (actual time=0.012..0.013 rows=0.00 loops=1)
                    ->  HashAggregate (actual time=0.012..0.012 rows=0.00 loops=1)
                          Group Key: card_views.card_type
                          Batches: 1  Memory Usage: 32kB
                          ->  Seq Scan on card_views (actual time=0.010..0.010 rows=0.00 loops=1)
                                Filter: (year = '2025'::smallint)
  ->  Hash (actual time=0.013..0.014 rows=0.00 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 8kB
        ->  Subquery Scan on s (actual time=0.013..0.013 rows=0.00 loops=1)
              ->  HashAggregate (actual time=0.012..0.013 rows=0.00 loops=1)
                    Group Key: shares.card_type
                    Batches: 1  Memory Usage: 32kB
                    ->  Seq Scan on shares (actual time=0.012..0.012 rows=0.00 loops=1)
                          Filter: (year = '2025'::smallint)
Planning:
  Buffers: shared hit=38 read=2
Planning Time: 1.269 ms
Execution Time: 0.216 ms
```

## Superlative distribution: `GET /v1/admin/analytics/superlatives`

Counts every card of the active run, so it reads the whole run by design. As shipped (aggregate, then join
the catalogue), with no secondary index:

```
Hash Join (actual time=621.060..631.768 rows=21.00 loops=1)
  Hash Cond: (payload_cards.card_type = t.card_type)
  Buffers: shared hit=541 read=8947
  ->  Finalize GroupAggregate (actual time=620.947..631.633 rows=21.00 loops=1)
        Group Key: payload_cards.card_type
        Buffers: shared hit=540 read=8947
        ->  Gather Merge (actual time=620.935..631.595 rows=63.00 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=540 read=8947
              ->  Sort (actual time=516.419..516.424 rows=21.00 loops=3)
                    Sort Key: payload_cards.card_type
                    Sort Method: quicksort  Memory: 25kB
                    Buffers: shared hit=540 read=8947
                    Worker 0:  Sort Method: quicksort  Memory: 25kB
                    Worker 1:  Sort Method: quicksort  Memory: 25kB
                    ->  Partial HashAggregate (actual time=516.311..516.323 rows=21.00 loops=3)
                          Group Key: payload_cards.card_type
                          Batches: 1  Memory Usage: 32kB
                          Buffers: shared hit=524 read=8947
                          Worker 0:  Batches: 1  Memory Usage: 32kB
                          Worker 1:  Batches: 1  Memory Usage: 32kB
                          ->  Parallel Seq Scan on payload_cards (actual time=1.350..187.719 rows=387128.67 loops=3)
                                Filter: (run_id = '9ade9774-8602-5d6e-baa8-297b5684a1f6'::uuid)
                                Buffers: shared hit=524 read=8947
  ->  Hash (actual time=0.095..0.098 rows=21.00 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 10kB
        Buffers: shared hit=1
        ->  Seq Scan on card_types t (actual time=0.047..0.060 rows=21.00 loops=1)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=7
Planning Time: 0.865 ms
Execution Time: 631.932 ms
```

As first written (join, then aggregate):

```
Finalize GroupAggregate (actual time=1071.220..1085.077 rows=21.00 loops=1)
  Group Key: c.card_type, t.family
  Buffers: shared hit=684 read=8806
  ->  Gather Merge (actual time=1071.202..1085.019 rows=63.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=684 read=8806
        ->  Sort (actual time=1011.466..1011.472 rows=21.00 loops=3)
              Sort Key: c.card_type, t.family
              Sort Method: quicksort  Memory: 25kB
              Buffers: shared hit=684 read=8806
              Worker 0:  Sort Method: quicksort  Memory: 25kB
              Worker 1:  Sort Method: quicksort  Memory: 25kB
              ->  Partial HashAggregate (actual time=1011.357..1011.370 rows=21.00 loops=3)
                    Group Key: c.card_type, t.family
                    Batches: 1  Memory Usage: 40kB
                    Buffers: shared hit=668 read=8806
                    Worker 0:  Batches: 1  Memory Usage: 40kB
                    Worker 1:  Batches: 1  Memory Usage: 40kB
                    ->  Hash Join (actual time=2.311..593.024 rows=387128.67 loops=3)
                          Hash Cond: (c.card_type = t.card_type)
                          Buffers: shared hit=668 read=8806
                          ->  Parallel Seq Scan on payload_cards c (actual time=1.334..200.881 rows=387128.67 loops=3)
                                Filter: (run_id = '9ade9774-8602-5d6e-baa8-297b5684a1f6'::uuid)
                                Buffers: shared hit=665 read=8806
                          ->  Hash (actual time=0.949..0.950 rows=21.00 loops=3)
                                Buckets: 1024  Batches: 1  Memory Usage: 10kB
                                Buffers: shared hit=3
                                ->  Seq Scan on card_types t (actual time=0.907..0.916 rows=21.00 loops=3)
                                      Buffers: shared hit=3
Planning:
  Buffers: shared hit=10
Planning Time: 0.920 ms
Execution Time: 1085.299 ms
```

### The index that was removed: `payload_cards_run_type_idx (run_id, card_type)`

The first schema had this index, added for this query. The same query with the index present, inside a
transaction that is rolled back. The planner still chooses the sequential scan: one run is most of the
table. The index was dropped in migration 0003.

```
Hash Join (actual time=881.027..902.519 rows=21.00 loops=1)
  Hash Cond: (payload_cards.card_type = t.card_type)
  Buffers: shared hit=1173 read=8315
  ->  Finalize GroupAggregate (actual time=880.925..902.390 rows=21.00 loops=1)
        Group Key: payload_cards.card_type
        Buffers: shared hit=1172 read=8315
        ->  Gather Merge (actual time=880.908..902.336 rows=63.00 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=1172 read=8315
              ->  Sort (actual time=756.002..756.007 rows=21.00 loops=3)
                    Sort Key: payload_cards.card_type
                    Sort Method: quicksort  Memory: 25kB
                    Buffers: shared hit=1172 read=8315
                    Worker 0:  Sort Method: quicksort  Memory: 25kB
                    Worker 1:  Sort Method: quicksort  Memory: 25kB
                    ->  Partial HashAggregate (actual time=755.510..755.520 rows=21.00 loops=3)
                          Group Key: payload_cards.card_type
                          Batches: 1  Memory Usage: 32kB
                          Buffers: shared hit=1156 read=8315
                          Worker 0:  Batches: 1  Memory Usage: 32kB
                          Worker 1:  Batches: 1  Memory Usage: 32kB
                          ->  Parallel Seq Scan on payload_cards (actual time=8.899..294.715 rows=387128.67 loops=3)
                                Filter: (run_id = '9ade9774-8602-5d6e-baa8-297b5684a1f6'::uuid)
                                Buffers: shared hit=1156 read=8315
  ->  Hash (actual time=0.077..0.079 rows=21.00 loops=1)
        Buckets: 1024  Batches: 1  Memory Usage: 10kB
        Buffers: shared hit=1
        ->  Seq Scan on card_types t (actual time=0.044..0.052 rows=21.00 loops=1)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=24 read=1
Planning Time: 4.095 ms
Execution Time: 902.670 ms
```
