REVOKE ALL ON mid_countries, vessels, positions, vessel_latest, coverage_cells, dark_events, quarantine
    FROM ghost_ingest, ghost_detect, ghost_api;
-- Roles are cluster-wide and may hold grants in other databases, so they are not dropped.
