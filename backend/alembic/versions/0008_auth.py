"""admin-UI auth: app_user and refresh_token. qb_app gets DML via the default privileges of docker/db/init-app-role.sh (no DDL).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""
import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(40), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", TS),
        sa.Column("last_login_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("username ~ '^[a-z0-9._-]{3,40}$'", name="ck_app_user_username"),
    )
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("ip", sa.Text()),
        sa.Column("user_agent", sa.Text()),
    )
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])


def downgrade() -> None:
    op.drop_table("refresh_token")
    op.drop_table("app_user")
