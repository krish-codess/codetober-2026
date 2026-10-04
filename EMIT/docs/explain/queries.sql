-- One query per index, exactly as the application issues it. Captured output: docs/explain/plans.txt
-- Regenerate (after a load test, so the tables have realistic volume):
--   docker compose exec -T postgres psql -U lastticket_owner -d lastticket -f - < docs/explain/queries.sql > docs/explain/plans.txt
\pset pager off
ANALYZE;
-- Parameters, resolved once so the plans below see literals, as they do from the application:
-- the busiest event, one of its sections, one of its buyers, and an instant two seconds into its stream.
SELECT event_id AS ev FROM reservation GROUP BY event_id ORDER BY count(*) DESC LIMIT 1 \gset
SELECT inventory_id AS inv, user_id AS usr FROM reservation WHERE event_id = :'ev' LIMIT 1 \gset
SELECT min(e.occurred_at) + interval '2 seconds' AS early FROM inventory_event e JOIN inventory i ON i.id = e.inventory_id WHERE i.event_id = :'ev' AND e.type = 'HOLD_PLACED' \gset
SELECT relname AS "table", n_live_tup AS rows FROM pg_stat_user_tables ORDER BY relname;

\echo
\echo == 1. reservation_due (partial: status = HELD) -- expiry sweeper, every second
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, inventory_id FROM reservation WHERE status = 'HELD' AND expires_at <= now() ORDER BY expires_at LIMIT 200;

\echo
\echo == 2. position of a whole event at a past instant (no dedicated index: V3 dropped inventory_event_as_of, see 2b)
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT i.id, coalesce(max(e.version), 0), coalesce(sum(e.total_delta), 0), coalesce(sum(e.held_delta), 0), coalesce(sum(e.sold_delta), 0)
  FROM inventory i LEFT JOIN inventory_event e ON e.inventory_id = i.id AND e.occurred_at <= :'early'
 WHERE i.event_id = :'ev' GROUP BY i.id;

\echo
\echo == 2b. inventory_event_version UNIQUE (inventory_id, version) -- position of one section at a past instant
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT coalesce(max(version), 0), coalesce(sum(total_delta), 0), coalesce(sum(held_delta), 0), coalesce(sum(sold_delta), 0)
  FROM inventory_event WHERE inventory_id = :'inv' AND occurred_at <= :'early';

\echo
\echo == 3. outbox_unpublished (partial: published_at IS NULL) -- relay, every 200 ms
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, topic, msg_key, payload FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 500;

\echo
\echo == 4. reservation_by_user (user_id, created_at DESC, id DESC) -- "my reservations", keyset page
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, status, created_at FROM reservation
 WHERE user_id = :'usr' AND (created_at, id) < (now(), 'ffffffff-ffff-ffff-ffff-ffffffffffff')
 ORDER BY created_at DESC, id DESC LIMIT 21;

\echo
\echo == 5. reservation_idempotency UNIQUE (user_id, idempotency_key) -- replay check on every reservation
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id FROM reservation WHERE user_id = :'usr' AND idempotency_key = 'no-such-key';

\echo
\echo == 6. reservation_one_active_hold UNIQUE (event_id, user_id) WHERE status = HELD -- enforced on insert; shown via lookup
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT 1 FROM reservation WHERE event_id = :'ev' AND user_id = 'nobody' AND status = 'HELD';

\echo
\echo == 7. inventory_natural_key UNIQUE (event_id, ticket_type, section) -- availability read and intake lookup
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, ticket_type, section, price_cents, total - held - sold AS available, total FROM inventory WHERE event_id = :'ev' ORDER BY ticket_type, section;

\echo
\echo == 8. inventory_event_version UNIQUE (inventory_id, version) -- the optimistic lock; also serves the drift check
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT 1 FROM inventory_event WHERE inventory_id = :'inv' AND version = 1;

\echo
\echo == 9. attempt_outcome_quarantined (partial: status = QUARANTINED) -- quarantine review list
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT r.id, r.batch_id, r.line_no, o.reason FROM attempt_outcome o JOIN raw_attempt r ON r.id = o.raw_attempt_id
 WHERE o.status = 'QUARANTINED' AND o.raw_attempt_id > 0 ORDER BY o.raw_attempt_id LIMIT 51;
