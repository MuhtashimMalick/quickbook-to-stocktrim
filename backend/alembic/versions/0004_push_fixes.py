"""push fixes: run status done_with_failures (column widened), push_item.ref (QuickBooks ListID)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""
import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OLD = "status IN ('running', 'done', 'failed', 'interrupted', 'cancelled')"
NEW = "status IN ('running', 'done', 'done_with_failures', 'failed', 'interrupted', 'cancelled')"


def _swap(check: str, width: int) -> None:
    op.drop_constraint("ck_push_run_status", "push_run", type_="check")
    op.alter_column("push_run", "status", type_=sa.String(width), existing_nullable=False)
    op.create_check_constraint("ck_push_run_status", "push_run", check)


def upgrade() -> None:  # existing rows all satisfy the wider check
    _swap(NEW, 24)
    op.add_column("push_item", sa.Column("ref", sa.Text()))


def downgrade() -> None:
    op.drop_column("push_item", "ref")
    op.drop_constraint("ck_push_run_status", "push_run", type_="check")
    op.execute("UPDATE push_run SET status = 'failed' WHERE status = 'done_with_failures'")  # still resumable under the old code
    op.alter_column("push_run", "status", type_=sa.String(16), existing_nullable=False)
    op.create_check_constraint("ck_push_run_status", "push_run", OLD)
