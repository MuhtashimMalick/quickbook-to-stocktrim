"""run_event may hold `schedule_changed` (the sync schedule is stored in app_state['schedule']: no table).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-19
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Frozen copy of EVENT_CODES (app/models.py) as of revision 0011; 0012 adds schedule_changed.
EVENT_CODES = ('cycle_started', 'reads_queued', 'waiting_for_web_connector', 'reads_done', 'snapshot_fresh', 'dry_run_planned', 'real_run_started', 'cycle_finished', 'run_started', 'step_started', 'step_finished', 'run_finished', 'preflight_requested', 'run_cancel_requested', 'run_resumed', 'check_failed:read_timeout', 'check_failed:read_job_failed', 'check_failed:empty_entity', 'check_failed:stale_snapshot', 'check_failed:push_running', 'check_failed:foreign_records', 'check_failed:preflight_not_ok', 'check_failed:mass_vanish', 'check_failed:too_many_calls', 'check_failed:writes_disabled', 'check_failed:production_not_allowed', 'check_failed:shutdown', 'check_failed:lock_lost', 'check_failed:push_done_with_failures', 'check_failed:push_failed', 'check_failed:push_interrupted', 'check_failed:push_cancelled', 'check_failed:restart', 'check_failed:stale', 'check_failed:crashed', 'check_failed:run_not_found', 'check_failed:stamp_has_no_bom_step', 'check_failed:stamp_test_only', 'check_failed:shutting_down', 'check_failed:stocktrim_writes_disabled', 'check_failed:preflight_fingerprint_required', 'check_failed:plan_fingerprint_required', 'check_failed:no_completed_initial_run', 'check_failed:stamp_needs_sync_mode', 'check_failed:step_already_pushed', 'check_failed:acknowledge_foreign_mismatch', 'check_failed:foreign_in_plan_production', 'check_failed:preflight_fingerprint_mismatch', 'check_failed:mass_delete', 'check_failed:requeue_requires_step', 'check_failed:mass_requeue', 'check_failed:stamp_snapshot_newer', 'check_failed:plan_fingerprint_mismatch', 'check_failed:stamp_requires_all_present', 'check_failed:run_not_resumable', 'check_failed:target_mismatch', 'check_failed:run_not_active')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})".replace(":", r"\:")  # a bare ':name' in a CHECK is read as a bind parameter


def upgrade() -> None:
    op.drop_constraint("ck_run_event_code", "run_event")
    op.create_check_constraint("ck_run_event_code", "run_event", _in("code", (*EVENT_CODES, "schedule_changed")))


def downgrade() -> None:
    op.execute("DELETE FROM run_event WHERE code = 'schedule_changed'")
    op.drop_constraint("ck_run_event_code", "run_event")
    op.create_check_constraint("ck_run_event_code", "run_event", _in("code", EVENT_CODES))
