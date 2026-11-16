"""baseline: jobs, sync_log, qb_records, po_link

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_message", sa.Text()),
        sa.Column("idempotency_key", sa.String(64), unique=True),
        sa.Column("ref_number", sa.String(11)),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("finished_at", TS),
    )
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "sync_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("entity", sa.String(60)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index("ix_sync_log_job_id", "sync_log", ["job_id"])

    op.create_table(
        "qb_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entity", sa.String(60), nullable=False),
        sa.Column("qb_id", sa.String(64), nullable=False),
        sa.Column("edit_sequence", sa.Text()),
        sa.Column("time_modified", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("synced_at", TS, nullable=False),
        sa.UniqueConstraint("entity", "qb_id"),
    )

    op.create_table(
        "po_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stocktrim_po_id", sa.String(64), nullable=False, unique=True),
        sa.Column("stocktrim_reference", sa.String(64)),
        sa.Column("qb_ref_number", sa.String(11), nullable=False, unique=True),
        sa.Column("qb_txn_id", sa.String(64), unique=True),
        sa.Column("qb_edit_sequence", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id")),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("po_link")
    op.drop_table("qb_records")
    op.drop_table("sync_log")
    op.drop_table("jobs")
