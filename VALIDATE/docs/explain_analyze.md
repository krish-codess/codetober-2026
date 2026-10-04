# Index evidence (EXPLAIN ANALYZE)

PostgreSQL 16, synthetic history of 2,000 runs x 110 properties (220,000 results,
72,005 failures). Reproduce with the commands in the README ("Index evidence"); the
script is `docs/explain_analyze.sql`. Raw output follows.

**Q1 - rejected.** A `(property_id, run_id DESC)` index on `property_results` was in
the first draft of the schema. The planner does as well without it (0.175 ms vs
0.208 ms): it walks the newest runs and probes the primary key. It was removed.

**Q2 - shipped.** `failures_property_id_idx` turns the history of a rarely-failing
property from 291 buffer hits into 7 (1.5 ms into 0.13 ms), and the gap grows with the
table.

**Q3** is the full catalog query as the API runs it: 110 invariants with 20-run
history each.

```
 runs | results | failures 
------+---------+----------
 2000 |  220000 |    72005
(1 row)


===== Q1: last 20 results of one property on one engine (catalog history) =====
----- as shipped: primary key (run_id, property_id) only -----
                                             QUERY PLAN                                             
----------------------------------------------------------------------------------------------------
 Limit (actual rows=20 loops=1)
   Buffers: shared hit=87
   ->  Nested Loop (actual rows=20 loops=1)
         Buffers: shared hit=87
         ->  Index Scan Backward using runs_pkey on runs ru (actual rows=20 loops=1)
               Filter: (engine = 'duckdb'::text)
               Rows Removed by Filter: 19
               Buffers: shared hit=4
         ->  Index Scan using property_results_pkey on property_results pr (actual rows=1 loops=20)
               Index Cond: ((run_id = ru.id) AND (run_id <= 2000) AND (property_id = 42))
               Buffers: shared hit=83
 Planning:
   Buffers: shared hit=67
 Planning Time: 1.061 ms
 Execution Time: 0.175 ms
(15 rows)

----- with a candidate index on (property_id, run_id DESC): REJECTED, no gain -----
                                         QUERY PLAN                                         
--------------------------------------------------------------------------------------------
 Limit (actual rows=20 loops=1)
   Buffers: shared hit=41 read=3
   ->  Merge Join (actual rows=20 loops=1)
         Merge Cond: (pr.run_id = ru.id)
         Buffers: shared hit=41 read=3
         ->  Index Scan using candidate_idx on property_results pr (actual rows=39 loops=1)
               Index Cond: ((property_id = 42) AND (run_id <= 2000))
               Buffers: shared hit=37 read=3
         ->  Index Scan Backward using runs_pkey on runs ru (actual rows=20 loops=1)
               Filter: (engine = 'duckdb'::text)
               Rows Removed by Filter: 19
               Buffers: shared hit=4
 Planning:
   Buffers: shared hit=26 read=1
 Planning Time: 0.854 ms
 Execution Time: 0.208 ms
(16 rows)


===== Q2: failure viewer filtered to a rarely-failing property, keyset page =====
----- as shipped: failures_property_id_idx -----
                                       QUERY PLAN                                        
-----------------------------------------------------------------------------------------
 Limit (actual rows=5 loops=1)
   Buffers: shared hit=7
   ->  Sort (actual rows=5 loops=1)
         Sort Key: id DESC
         Sort Method: quicksort  Memory: 25kB
         Buffers: shared hit=7
         ->  Bitmap Heap Scan on failures f (actual rows=5 loops=1)
               Recheck Cond: ((property_id = 110) AND (id < 70000))
               Heap Blocks: exact=4
               Buffers: shared hit=7
               ->  Bitmap Index Scan on failures_property_id_idx (actual rows=5 loops=1)
                     Index Cond: ((property_id = 110) AND (id < 70000))
                     Buffers: shared hit=3
 Planning:
   Buffers: shared hit=3
 Planning Time: 0.305 ms
 Execution Time: 0.127 ms
(17 rows)

----- without it -----
                                             QUERY PLAN                                             
----------------------------------------------------------------------------------------------------
 Limit (actual rows=5 loops=1)
   Buffers: shared hit=291
   ->  Sort (actual rows=5 loops=1)
         Sort Key: id DESC
         Sort Method: quicksort  Memory: 25kB
         Buffers: shared hit=291
         ->  Index Scan using failures_run_id_property_id_key on failures f (actual rows=5 loops=1)
               Index Cond: (property_id = 110)
               Filter: (id < 70000)
               Buffers: shared hit=291
 Planning:
   Buffers: shared hit=4
 Planning Time: 0.242 ms
 Execution Time: 1.545 ms
(14 rows)


===== Q3: the whole catalog query for one run (what the API executes) =====
                                                                 QUERY PLAN                                                                  
---------------------------------------------------------------------------------------------------------------------------------------------
 Limit (actual rows=110 loops=1)
   Buffers: shared hit=8410
   ->  Sort (actual rows=110 loops=1)
         Sort Key: p.name
         Sort Method: quicksort  Memory: 34kB
         Buffers: shared hit=8410
         ->  Nested Loop (actual rows=110 loops=1)
               Buffers: shared hit=8410
               ->  Index Scan using runs_pkey on runs this (actual rows=1 loops=1)
                     Index Cond: (id = 2000)
                     Buffers: shared hit=3
               ->  Nested Loop (actual rows=110 loops=1)
                     Buffers: shared hit=8407
                     ->  Merge Left Join (actual rows=110 loops=1)
                           Merge Cond: (r.property_id = f.property_id)
                           Buffers: shared hit=12
                           ->  Merge Join (actual rows=110 loops=1)
                                 Merge Cond: (r.property_id = p.id)
                                 Buffers: shared hit=8
                                 ->  Index Scan using property_results_pkey on property_results r (actual rows=110 loops=1)
                                       Index Cond: (run_id = 2000)
                                       Buffers: shared hit=5
                                 ->  Index Scan using properties_pkey on properties p (actual rows=110 loops=1)
                                       Buffers: shared hit=3
                           ->  Index Scan using failures_run_id_property_id_key on failures f (actual rows=36 loops=1)
                                 Index Cond: (run_id = 2000)
                                 Buffers: shared hit=4
                     ->  Aggregate (actual rows=1 loops=110)
                           Buffers: shared hit=8395
                           ->  Limit (actual rows=20 loops=110)
                                 Buffers: shared hit=8395
                                 ->  Merge Join (actual rows=20 loops=110)
                                       Merge Cond: (pr.run_id = ru.id)
                                       Buffers: shared hit=8395
                                       ->  Index Scan Backward using property_results_pkey on property_results pr (actual rows=39 loops=110)
                                             Index Cond: ((run_id <= r.run_id) AND (property_id = r.property_id))
                                             Buffers: shared hit=7955
                                       ->  Index Scan Backward using runs_pkey on runs ru (actual rows=20 loops=110)
                                             Filter: (engine = this.engine)
                                             Rows Removed by Filter: 19
                                             Buffers: shared hit=440
 Planning:
   Buffers: shared hit=90
 Planning Time: 2.462 ms
 Execution Time: 22.422 ms
(45 rows)

```
