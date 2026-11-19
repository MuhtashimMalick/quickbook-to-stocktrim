"""Run log: phase events (run_event) and retention. Codes, counts and business keys only: never names, payloads, messages or secrets. Writing an event never breaks the caller."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, exists, func, select

from .config import Settings
from .db import session_scope
from .models import PushExclusion, PushItem, PushRun, RunEvent

log = logging.getLogger("app.runlog")
DRY_RUN_DAYS, REAL_RUN_DAYS = 30, 180  # push_run retention; run_event follows HISTORY_RETENTION_DAYS
EXCLUSION_KEEP_RUNS = 3  # push_exclusion rows survive only for the newest N runs per target (dry + real): consecutive runs repeat the same rows


def event(code: str, *, cycle_id: Optional[UUID] = None, run_id: Optional[UUID] = None, level: str = "info", entity: Optional[str] = None,
          counts: Optional[dict[str, Any]] = None, actor: Optional[str] = None) -> None:
    """`counts`: only its int values are kept."""
    ints = {k: v for k, v in (counts or {}).items() if isinstance(v, int)}
    try:
        with session_scope() as s:
            s.add(RunEvent(code=code, cycle_id=cycle_id, run_id=run_id, level=level, entity=entity, counts=ints or None, actor=actor))
    except Exception as e:  # noqa: BLE001 - the log is never worth a failed cycle or run
        log.error("run event %s not written: %s", code, type(e).__name__)


def prune(settings: Settings, now: Optional[datetime] = None) -> dict[str, int]:
    """Delete run_event older than HISTORY_RETENTION_DAYS, dry-run push_runs older than 30 days and real ones older than 180 (items and exclusions cascade), and the exclusion rows of every run but the newest 3 per target - except what a later
    stage still needs: a resumable run (interrupted/failed with pending items), the latest run per target, and the latest completed real run per target (the sync baseline)."""
    from .sync.engine import _REAL  # noqa: PLC0415 - the engine imports this module

    now = now or datetime.now(timezone.utc)
    dry = PushRun.options["dry_run"].as_boolean().is_(True)
    latest = select(PushRun.id).distinct(PushRun.target).order_by(PushRun.target, PushRun.started_at.desc())
    baseline = select(PushRun.id).where(*_REAL).distinct(PushRun.target).order_by(PushRun.target, PushRun.started_at.desc())
    resumable = select(PushRun.id).where(PushRun.status.in_(("interrupted", "failed")), exists().where(PushItem.run_id == PushRun.id, PushItem.status == "pending"))
    with session_scope() as s:
        events = s.execute(delete(RunEvent).where(RunEvent.at < now - timedelta(days=settings.history_retention_days))).rowcount
        runs = s.execute(delete(PushRun).where(
            ((dry & (PushRun.started_at < now - timedelta(days=DRY_RUN_DAYS))) | (~dry & (PushRun.started_at < now - timedelta(days=REAL_RUN_DAYS)))),
            PushRun.status != "running", PushRun.id.not_in(latest), PushRun.id.not_in(baseline), PushRun.id.not_in(resumable))).rowcount
        rank = select(PushRun.id, func.row_number().over(partition_by=PushRun.target, order_by=PushRun.started_at.desc()).label("n")).subquery()
        newest = select(rank.c.id).where(rank.c.n <= EXCLUSION_KEEP_RUNS)
        excl = s.execute(delete(PushExclusion).where(PushExclusion.run_id.not_in(newest), PushExclusion.run_id.not_in(resumable))).rowcount  # the run rows and items stay
    return {"run_events": events, "push_runs": runs, "push_exclusions": excl}
