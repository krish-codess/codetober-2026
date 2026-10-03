-- Rollback of V2. Destroys all data in these tables: take a backup first (docs/operations.md).
DROP TABLE IF EXISTS attempt_outcome, raw_attempt, sale_stat, dead_letter, processed_message, outbox,
                     reservation, inventory_event, inventory, sale_event;
DELETE FROM flyway_schema_history WHERE version = '2';
