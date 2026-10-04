-- 0004: rejection is no longer Hampel-only (cross-server consensus decides first), so the event
-- kind 'hampel_reject' becomes 'rejected_price'; detail.reason says which rule fired.
ALTER TABLE manipulation_event DROP CONSTRAINT manipulation_event_kind_check;
UPDATE manipulation_event SET kind = 'rejected_price' WHERE kind = 'hampel_reject';
ALTER TABLE manipulation_event ADD CONSTRAINT manipulation_event_kind_check
    CHECK (kind IN ('extreme_listing', 'rejected_price', 'thin_market_spike'));
