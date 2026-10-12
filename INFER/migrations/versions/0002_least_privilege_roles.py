"""Least-privilege group roles. Login users are created by ops (deploy/init-roles.sh) as members of these.

The API reads everything and writes exactly one table: the policies it creates. It cannot touch experiment data,
model results or scores. The batch job reads and writes data but owns nothing, so it cannot change the schema.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'uplift_api_role') THEN
                CREATE ROLE uplift_api_role NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'uplift_batch_role') THEN
                CREATE ROLE uplift_batch_role NOLOGIN;
            END IF;
        END $$;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO uplift_api_role;
        GRANT INSERT ON policy TO uplift_api_role;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO uplift_batch_role;
        """
    )


def downgrade() -> None:
    # The roles are cluster-wide and may be used by another database; only this database's grants are removed.
    op.execute(
        """
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM uplift_api_role;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM uplift_batch_role;
        """
    )
