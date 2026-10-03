-- Rollback of V1. The role is left in place if other databases on the server still use it.
REVOKE USAGE ON SCHEMA public FROM ${app_user};
DELETE FROM flyway_schema_history WHERE version = '1';
