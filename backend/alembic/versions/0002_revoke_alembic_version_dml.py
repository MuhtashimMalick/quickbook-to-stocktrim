"""qb_app (least-privilege runtime role, if present) must not write alembic_version

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qb_app') THEN "
               "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM qb_app; END IF; END $$")


def downgrade() -> None:
    pass
