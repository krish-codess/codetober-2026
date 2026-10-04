-- Least privilege: the application connects as a role that owns nothing. Table-level grants are in later migrations.
-- Roles are cluster-wide, so this must be idempotent across databases on the same server.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${app_user}') THEN
    CREATE ROLE ${app_user} LOGIN PASSWORD '${app_password}';
  ELSE
    ALTER ROLE ${app_user} LOGIN PASSWORD '${app_password}';
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO ${app_user};
