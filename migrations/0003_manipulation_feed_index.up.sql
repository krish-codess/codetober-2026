-- 0003: the integrity feed pages newest-first with a keyset on (day, item_id, kind), all DESC.
-- The 0001 index stored item_id/kind ASC, so Postgres had to re-sort each day's rows (incremental
-- sort). Matching the index order to the query order lets the LIMIT stop after N index entries.
-- Evidence: docs/explain/manipulation_feed.txt
DROP INDEX manipulation_event_feed;
CREATE INDEX manipulation_event_feed ON manipulation_event (server_id, day DESC, item_id DESC, kind DESC);
