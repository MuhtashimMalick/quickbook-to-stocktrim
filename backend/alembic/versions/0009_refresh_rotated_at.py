"""refresh_token.rotated_at: set only when a refresh rotated the token (distinguishes a benign concurrent replay from logout/passwd revocation).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""
import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("rotated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("refresh_token", "rotated_at")
