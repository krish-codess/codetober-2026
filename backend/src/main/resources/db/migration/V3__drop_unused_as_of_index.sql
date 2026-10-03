-- inventory_event_as_of (inventory_id, occurred_at) INCLUDE (deltas) was added in V2 for point-in-time reconstruction.
-- EXPLAIN ANALYZE on a loaded database (docs/explain/plans.txt, queries 2 and 2b) shows the planner never picks it:
-- occurred_at is monotonic in version within an aggregate, so the UNIQUE (inventory_id, version) index already finds an
-- aggregate's events, and an event-wide query reads most of the stream anyway. An index that only costs writes goes.
DROP INDEX inventory_event_as_of;
