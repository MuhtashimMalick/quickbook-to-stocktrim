"""run log: run_event (phase events of cycles and runs), push_exclusion (records a plan leaves out, per run), push_item.action/created_at. qb_app gets DML via the default privileges of docker/db/init-app-role.sh.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
# Frozen copies of EVENT_CODES / EXCLUSION_CODES (app/models.py) as of this revision: a new code needs a new migration.
EVENT_CODES = ('cycle_started', 'reads_queued', 'waiting_for_web_connector', 'reads_done', 'snapshot_fresh', 'dry_run_planned', 'real_run_started', 'cycle_finished', 'run_started', 'step_started', 'step_finished', 'run_finished', 'preflight_requested', 'run_cancel_requested', 'run_resumed', 'check_failed:read_timeout', 'check_failed:read_job_failed', 'check_failed:empty_entity', 'check_failed:stale_snapshot', 'check_failed:push_running', 'check_failed:foreign_records', 'check_failed:preflight_not_ok', 'check_failed:mass_vanish', 'check_failed:too_many_calls', 'check_failed:writes_disabled', 'check_failed:production_not_allowed', 'check_failed:shutdown', 'check_failed:lock_lost', 'check_failed:push_done_with_failures', 'check_failed:push_failed', 'check_failed:push_interrupted', 'check_failed:push_cancelled', 'check_failed:restart', 'check_failed:stale', 'check_failed:crashed', 'check_failed:run_not_found', 'check_failed:stamp_has_no_bom_step', 'check_failed:stamp_test_only', 'check_failed:shutting_down', 'check_failed:stocktrim_writes_disabled', 'check_failed:preflight_fingerprint_required', 'check_failed:plan_fingerprint_required', 'check_failed:no_completed_initial_run', 'check_failed:stamp_needs_sync_mode', 'check_failed:step_already_pushed', 'check_failed:acknowledge_foreign_mismatch', 'check_failed:foreign_in_plan_production', 'check_failed:preflight_fingerprint_mismatch', 'check_failed:mass_delete', 'check_failed:requeue_requires_step', 'check_failed:mass_requeue', 'check_failed:stamp_snapshot_newer', 'check_failed:plan_fingerprint_mismatch', 'check_failed:stamp_requires_all_present', 'check_failed:run_not_resumable', 'check_failed:target_mismatch', 'check_failed:run_not_active')
EXCLUSION_CODES = ('empty_key', 'whitespace_key', 'control_chars', 'duplicate_key', 'empty_list_id', 'parse_error', 'inactive_supplier', 'inactive_customer', 'unresolved_vendor', 'bom_cycle', 'bom_cycle_ancestor', 'bom_component_excluded', 'bom_component_unresolved', 'bom_self_reference', 'nonpositive_quantity', 'bom_quantity_precision', 'non_product', 'group', 'zero_quantity', 'closed_uninvoiced', 'vendor_not_pushed', 'no_lines', 'lines_non_product')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})".replace(":", r"\:")  # SQLAlchemy reads a bare ':name' in a CHECK as a bind parameter


def upgrade() -> None:
    op.add_column("push_item", sa.Column("action", sa.Text()))
    op.add_column("push_item", sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")))
    op.create_check_constraint("ck_push_item_action", "push_item", "action IN ('upsert', 'delete')")
    op.create_table(
        "push_exclusion",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text()),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("run_id", "entity", "key", "code"),
        sa.CheckConstraint(_in("code", EXCLUSION_CODES), name="ck_push_exclusion_code"),
    )
    op.create_table(
        "run_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_cycle.id", ondelete="CASCADE")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("push_run.id", ondelete="CASCADE")),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text()),
        sa.Column("counts", postgresql.JSONB()),
        sa.Column("actor", sa.Text()),
        sa.CheckConstraint("level IN ('info', 'warning', 'error')", name="ck_run_event_level"),
        sa.CheckConstraint(_in("code", EVENT_CODES), name="ck_run_event_code"),
    )
    op.create_index("ix_run_event_cycle", "run_event", ["cycle_id", "id"])
    op.create_index("ix_run_event_run", "run_event", ["run_id", "id"])


def downgrade() -> None:
    op.drop_table("run_event")
    op.drop_table("push_exclusion")
    op.drop_constraint("ck_push_item_action", "push_item")
    op.drop_column("push_item", "created_at")
    op.drop_column("push_item", "action")
