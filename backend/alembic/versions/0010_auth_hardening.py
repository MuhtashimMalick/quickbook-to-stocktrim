"""auth hardening: app_user.last_failed_at / tokens_valid_after, refresh_token.replaced_by_id (rotation successor).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19
"""
import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("last_failed_at", sa.DateTime(timezone=True)))
    op.add_column("app_user", sa.Column("tokens_valid_after", sa.DateTime(timezone=True)))
    op.add_column("refresh_token", sa.Column("replaced_by_id", sa.Integer(), sa.ForeignKey("refresh_token.id", ondelete="SET NULL")))


def downgrade() -> None:
    op.drop_column("refresh_token", "replaced_by_id")
    op.drop_column("app_user", "tokens_valid_after")
    op.drop_column("app_user", "last_failed_at")
