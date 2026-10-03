DROP INDEX manipulation_event_feed;
CREATE INDEX manipulation_event_feed ON manipulation_event (server_id, day DESC, item_id, kind);
