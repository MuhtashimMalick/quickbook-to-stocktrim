"""pushed_item: incremental-sync ledger (qb_app gets DML via the default privileges of docker/db/init-app-role.sh), backfilled from done, non-dry-run push_item rows with a NULL
payload_hash, so the first sync run re-sends everything once (every write is a proven upsert; ~70 min on the full data set).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pushed_item",
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text()),
        sa.Column("payload_hash", sa.Text()),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id", ondelete="SET NULL")),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("target", "entity", "key"),
        sa.CheckConstraint("entity IN ('supplier', 'product', 'customer')", name="ck_pushed_item_entity"),
    )
    op.execute("""
        INSERT INTO pushed_item (target, entity, key, ref, payload_hash, last_run_id, pushed_at)
        SELECT DISTINCT ON (r.target, i.entity, i.key) r.target, i.entity, i.key, i.ref, NULL, r.id, i.updated_at
        FROM push_item i JOIN push_run r ON r.id = i.run_id
        WHERE i.status = 'done' AND i.entity IN ('supplier', 'product', 'customer') AND (r.options->>'dry_run') IS DISTINCT FROM 'true'
        ORDER BY r.target, i.entity, i.key, i.updated_at DESC
    """)


def downgrade() -> None:
    op.drop_table("pushed_item")
