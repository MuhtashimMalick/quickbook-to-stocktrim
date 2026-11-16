"""push foundation: bom_edge, push_run, push_item (qb_app gets DML via the default privileges of docker/db/init-app-role.sh)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "bom_edge",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("parent_sku", sa.Text(), nullable=False),
        sa.Column("component_sku", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("parent_hash", sa.Text(), nullable=False),
        sa.Column("pushed_at", TS, nullable=False),
        sa.UniqueConstraint("target", "parent_sku", "component_sku"),
        sa.CheckConstraint("quantity > 0", name="ck_bom_edge_quantity_positive"),
    )
    op.create_table(
        "push_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text()),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("preflight_fingerprint", sa.Text()),
        sa.Column("summary", postgresql.JSONB()),
        sa.CheckConstraint("status IN ('running', 'done', 'failed', 'interrupted', 'cancelled')", name="ck_push_run_status"),
    )
    op.create_index("ux_push_run_one_running", "push_run", [sa.text("(1)")], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_table(
        "push_item",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("run_id", "entity", "key"),
        sa.CheckConstraint("status IN ('pending', 'done', 'failed', 'skipped')", name="ck_push_item_status"),
        sa.CheckConstraint("entity IN ('supplier', 'product', 'bom_edge', 'customer')", name="ck_push_item_entity"),
        sa.CheckConstraint("attempts >= 0", name="ck_push_item_attempts_nonneg"),
    )
    op.create_index("ix_push_item_run_entity_status", "push_item", ["run_id", "entity", "status"])


def downgrade() -> None:
    op.drop_table("push_item")
    op.drop_table("push_run")
    op.drop_table("bom_edge")
