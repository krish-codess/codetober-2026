-- Rollback of V3: put the index back.
CREATE INDEX inventory_event_as_of ON inventory_event (inventory_id, occurred_at) INCLUDE (total_delta, held_delta, sold_delta);
DELETE FROM flyway_schema_history WHERE version = '3';
