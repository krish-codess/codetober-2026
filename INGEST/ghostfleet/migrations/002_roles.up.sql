-- Least privilege: one login per service, each able to touch only what it needs.
-- Passwords are set from the environment by `ghostfleet migrate`, never written here.
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['ghost_ingest', 'ghost_detect', 'ghost_api'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', r);
        END IF;
    END LOOP;
END $$;

-- Sink: writes vessels, positions, latest and quarantine. Cannot touch detections.
GRANT SELECT, INSERT, UPDATE ON vessels, vessel_latest TO ghost_ingest;
GRANT SELECT, INSERT ON positions TO ghost_ingest;
GRANT SELECT, INSERT, UPDATE ON quarantine TO ghost_ingest;

-- Detector: reads positions, owns the derived tables' contents.
GRANT SELECT ON positions, vessels, vessel_latest TO ghost_detect;
GRANT SELECT, INSERT, UPDATE, DELETE ON dark_events, coverage_cells TO ghost_detect;

-- API: read-only, everywhere.
GRANT SELECT ON mid_countries, vessels, positions, vessel_latest, coverage_cells, dark_events, quarantine TO ghost_api;
