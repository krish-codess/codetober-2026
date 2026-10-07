"""Least-privilege group roles. Login users are created by ops and made members of these.

The API can read payloads and record views and shares; it cannot write a payload or change what
is active. The batch job can do that; it cannot read who viewed or shared what.

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
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'wrapped_api_role') THEN
                CREATE ROLE wrapped_api_role NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'wrapped_batch_role') THEN
                CREATE ROLE wrapped_batch_role NOLOGIN;
            END IF;
        END $$;

        GRANT SELECT ON generation_runs, active_runs, card_types, wrapped_payloads, payload_cards TO wrapped_api_role;
        GRANT SELECT, INSERT ON card_views TO wrapped_api_role;
        GRANT SELECT, INSERT, UPDATE ON shares TO wrapped_api_role;

        GRANT SELECT, INSERT, UPDATE, DELETE ON generation_runs, active_runs, wrapped_payloads, payload_cards
            TO wrapped_batch_role;
        GRANT SELECT, INSERT, UPDATE ON card_types TO wrapped_batch_role;
        """
    )


def downgrade() -> None:
    # Roles are cluster-wide and may be used by another database, so only this database's grants are removed.
    op.execute(
        """
        REVOKE ALL ON generation_runs, active_runs, card_types, wrapped_payloads, payload_cards, card_views, shares
            FROM wrapped_api_role, wrapped_batch_role;
        """
    )
