"""scheduler: sync_cycle (one row per daily/manual cycle) and app_state (last_qbwc_contact, last_cycle). qb_app gets DML via the default privileges of docker/db/init-app-role.sh.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sync_cycle",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("push_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id", ondelete="SET NULL")),
        sa.Column("summary", postgresql.JSONB()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('running', 'done', 'dry_run_only', 'aborted', 'failed', 'interrupted')", name="ck_sync_cycle_status"),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name="ck_sync_cycle_trigger"),
    )
    op.create_index("ux_sync_cycle_one_running", "sync_cycle", [sa.text("(1)")], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_table("sync_cycle")
