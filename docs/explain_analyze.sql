-- Evidence for the secondary index that shipped, and for one that was rejected. Run against a scratch database that already
-- has the migrations applied (see README, "Index evidence"). It loads a synthetic
-- history of 2,000 runs x 110 properties, then runs each query with and without
-- the index in question. Output is committed as docs/explain_analyze.md.
\set ON_ERROR_STOP on
\timing off
\pset pager off

INSERT INTO runs (idempotency_key, subject, engine, seed, max_examples, status, started_at,
                  finished_at, examples, rows_generated, duration_ms, candidates,
                  candidates_held, gate_ok)
SELECT 'synthetic-' || g, 'jaffle', CASE WHEN g % 2 = 0 THEN 'duckdb' ELSE 'postgres' END, g,
       200, 'completed', now() - (2000 - g) * interval '1 hour',
       now() - (2000 - g) * interval '1 hour' + interval '1 minute', 200, 1500, 60000, 97, 60,
       true
FROM generate_series(1, 2000) g;

INSERT INTO properties (subject, name, source, description)
SELECT 'jaffle', 'property_' || lpad(g::text, 3, '0'),
       CASE WHEN g <= 13 THEN 'declared' ELSE 'discovered' END, 'synthetic property ' || g
FROM generate_series(1, 110) g;

-- Roughly a third of the properties are falsified in every run. Property 110 is the
-- rare one: it failed in the five oldest runs only.
INSERT INTO property_results (run_id, property_id, passed, failed, vacuous, status, confidence)
SELECT r.id, p.id,
       CASE WHEN x.bad THEN 60 ELSE 150 END, CASE WHEN x.bad THEN 90 ELSE 0 END, 50,
       CASE WHEN x.bad THEN 'falsified' ELSE 'held' END, CASE WHEN x.bad THEN 0 ELSE 0.98 END
FROM runs r CROSS JOIN properties p
CROSS JOIN LATERAL (SELECT p.id % 3 = 0 OR (p.id = 110 AND r.id <= 5) AS bad) x;

INSERT INTO failures (run_id, property_id, minimal_dataset, minimal_rows, shrunk,
                      shrink_calls, shrink_ms)
SELECT run_id, property_id, '{"raw_customers": [], "raw_orders": [], "raw_payments": []}',
       2, true, 60, 1500
FROM property_results WHERE status = 'falsified' ORDER BY run_id, property_id;

ANALYZE;
SELECT (SELECT count(*) FROM runs) AS runs, (SELECT count(*) FROM property_results) AS results,
       (SELECT count(*) FROM failures) AS failures;

\echo
\echo ===== Q1: last 20 results of one property on one engine (catalog history) =====
\echo ----- as shipped: primary key (run_id, property_id) only -----
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT pr.status FROM property_results pr JOIN runs ru ON ru.id = pr.run_id
WHERE pr.property_id = 42 AND pr.run_id <= 2000 AND ru.engine = 'duckdb'
ORDER BY pr.run_id DESC LIMIT 20;

BEGIN;
CREATE INDEX candidate_idx ON property_results (property_id, run_id DESC);
\echo ----- with a candidate index on (property_id, run_id DESC): REJECTED, no gain -----
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT pr.status FROM property_results pr JOIN runs ru ON ru.id = pr.run_id
WHERE pr.property_id = 42 AND pr.run_id <= 2000 AND ru.engine = 'duckdb'
ORDER BY pr.run_id DESC LIMIT 20;
ROLLBACK;

\echo
\echo ===== Q2: failure viewer filtered to a rarely-failing property, keyset page =====
\echo ----- as shipped: failures_property_id_idx -----
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT f.id FROM failures f
WHERE f.property_id = 110 AND f.id < 70000 ORDER BY f.id DESC LIMIT 51;

BEGIN;
DROP INDEX failures_property_id_idx;
\echo ----- without it -----
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT f.id FROM failures f
WHERE f.property_id = 110 AND f.id < 70000 ORDER BY f.id DESC LIMIT 51;
ROLLBACK;

\echo
\echo ===== Q3: the whole catalog query for one run (what the API executes) =====
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT p.name, r.status, r.confidence, f.id, h.runs, h.falsified
FROM property_results r
JOIN properties p ON p.id = r.property_id
JOIN runs this ON this.id = r.run_id
LEFT JOIN failures f ON f.run_id = r.run_id AND f.property_id = r.property_id
CROSS JOIN LATERAL (
    SELECT count(*) AS runs, count(*) FILTER (WHERE x.status = 'falsified') AS falsified
    FROM (SELECT pr.status FROM property_results pr JOIN runs ru ON ru.id = pr.run_id
          WHERE pr.property_id = r.property_id AND pr.run_id <= r.run_id
            AND ru.engine = this.engine
          ORDER BY pr.run_id DESC LIMIT 20) x) h
WHERE r.run_id = 2000 ORDER BY p.name LIMIT 201;
