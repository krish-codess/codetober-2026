-- Rollback of 0001. Destroys all data: take a backup first (docs/OPERATIONS.md).
DROP TABLE IF EXISTS idempotency_key, api_key, money_flow, manipulation_event, patch_impact,
    shock_attribution, shock, inflation_rate, patch_event, index_value, basket_link, basket_item,
    basket_period, item_price_daily, data_quality_daily, ingest_run, raw_manifest, index_series,
    activity_yield, activity_rate, activity, item, division, server, world CASCADE;
DROP VIEW IF EXISTS index_value_current, item_price_current;
DROP FUNCTION IF EXISTS forbid_mutation();
-- Group roles are cluster-level and owned by the bootstrap (docker/postgres-init.sh); they are
-- intentionally kept so that login users keep their memberships across a down/up cycle.
REVOKE ALL ON SCHEMA public FROM gs_pipeline_role, gs_api_role;
