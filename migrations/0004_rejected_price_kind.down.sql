ALTER TABLE manipulation_event DROP CONSTRAINT manipulation_event_kind_check;
UPDATE manipulation_event SET kind = 'hampel_reject' WHERE kind = 'rejected_price';
ALTER TABLE manipulation_event ADD CONSTRAINT manipulation_event_kind_check
    CHECK (kind IN ('extreme_listing', 'hampel_reject', 'thin_market_spike'));
