-- 0002: distinguish level shifts from transient spikes.
-- persistence = share of the shock-day move still present PERSISTENCE_DAYS later
--   ~1  the level moved and stayed (a patch-style shift)
--   ~0  the move fully reverted (a spike, often a thin-market event)
--   NULL the later days are not known yet (provisional)
ALTER TABLE shock ADD COLUMN persistence double precision;
COMMENT ON COLUMN shock.persistence IS 'share of the shock move retained 3 days later; NULL = not yet known';
