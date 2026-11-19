"""Scheduler trigger and the health endpoint. Counts, codes and timestamps only: never record contents."""
import threading
from contextlib import closing
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text

from ...appstate import get_state
from ...db import session_scope
from ...models import Job, PushRun, QBRecord, SyncCycle, utcnow
from ...preflight.report import preflight_client
from ...scheduler import CycleBusy, env_schedule, item_counts, load_schedule, next_run
from ...stocktrim.api import StockTrimApi
from ...stocktrim.errors import StockTrimConfigError, StockTrimError

router = APIRouter()
_PROBING = threading.Lock()  # ponytail: one StockTrim probe per process; a second concurrent ?probe=true answers "busy" instead of piling up threads


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CycleStarted(M):
    cycle_id: UUID
    status: str


class SchedulerOut(M):
    enabled: bool
    running: bool  # the scheduler thread is alive
    time: str  # HH:MM in tz
    every_days: int
    tz: str
    schedule_enabled: bool  # the stored switch; `enabled` is the server ceiling (SCHEDULER_ENABLED)
    source: str  # database | env_default
    next_run_at: Optional[datetime] = None  # only when both are enabled


def scheduler_out(request: Request, schedule: Any, source: str) -> dict[str, Any]:
    st, sch = request.app.state.settings, request.app.state.scheduler
    return {"enabled": st.scheduler_enabled, "running": sch.running, "time": schedule.time, "every_days": schedule.every_days, "tz": schedule.tz, "schedule_enabled": schedule.enabled, "source": source,
            "next_run_at": next_run(sch.clock(), schedule) if st.scheduler_enabled and schedule.enabled else None}


class CycleOut(M):
    id: UUID
    status: str
    reason: Optional[str]
    trigger: str
    started_at: datetime
    finished_at: Optional[datetime]
    push_run_id: Optional[UUID]
    summary: Optional[dict[str, Any]] = None  # counts and short codes only


class Freshness(M):
    synced_at_max: Optional[datetime]
    records: int


class PushRunHealth(M):
    run_id: UUID
    status: str
    dry_run: bool
    started_at: datetime
    finished_at: Optional[datetime]
    counts: dict[str, int]  # item counts by status (all steps)


class Reachability(M):
    reachable: bool
    error: Optional[str] = None


class Health(M):
    database: bool
    environment: str
    stocktrim_writes_enabled: bool
    scheduler: SchedulerOut
    last_qbwc_contact: Optional[datetime] = None
    last_qbwc_contact_age_seconds: Optional[int] = None
    last_qbwc_auth_refused: Optional[datetime] = None  # the last refused Web Connector login (wrong credentials)
    last_cycle: Optional[CycleOut] = None
    jobs: dict[str, int] = {}  # queued / running QuickBooks jobs
    snapshot: dict[str, Freshness] = {}  # per QuickBooks entity
    push_run: Optional[PushRunHealth] = None
    stocktrim: Optional[Reachability] = None  # only with ?probe=true


@router.post("/scheduler/run-now", response_model=CycleStarted, status_code=202)
def run_now(request: Request) -> Any:
    """Start one cycle now (the same as the daily one). 409 while a cycle is running, 403 when SCHEDULER_ENABLED is false."""
    sch = request.app.state.scheduler
    if not sch.settings.scheduler_enabled:
        raise HTTPException(403, "scheduler_disabled")
    try:
        return {"cycle_id": sch.trigger_now(request.state.actor), "status": "running"}
    except CycleBusy:
        raise HTTPException(409, "cycle_running") from None


def _probe(request: Request) -> Reachability:
    """One GET (a supplier lookup) through the read-only client, a single attempt."""
    if not _PROBING.acquire(blocking=False):
        return Reachability(reachable=False, error="busy")
    try:
        with closing(preflight_client(request.app.state.settings, request.app.state.scheduler.kw, max_attempts=1)) as c:
            StockTrimApi(c).get_supplier("health-probe")
        return Reachability(reachable=True)
    except StockTrimConfigError:
        return Reachability(reachable=False, error="not_configured")
    except StockTrimError as e:
        return Reachability(reachable=False, error=type(e).__name__)
    finally:
        _PROBING.release()


@router.get("/health", response_model=Health)
def health(request: Request, probe: bool = False) -> Any:
    settings = request.app.state.settings
    out: dict[str, Any] = {"database": True, "environment": settings.stocktrim_environment, "stocktrim_writes_enabled": settings.stocktrim_writes_enabled}
    schedule, source = env_schedule(settings), "env_default"  # until the database answers
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            schedule, source = load_schedule(settings)
            out["jobs"] = dict(s.execute(select(Job.status, func.count()).where(Job.status.in_(("queued", "running"))).group_by(Job.status)).all())
            out["snapshot"] = {e: {"synced_at_max": m, "records": n} for e, m, n in s.execute(select(QBRecord.entity, func.max(QBRecord.synced_at), func.count()).group_by(QBRecord.entity))}
            out["last_cycle"] = s.scalars(select(SyncCycle).order_by(SyncCycle.started_at.desc()).limit(1)).first()
            run = s.scalars(select(PushRun).order_by(PushRun.started_at.desc()).limit(1)).first()
            out["push_run"] = run and {"run_id": run.id, "status": run.status, "dry_run": bool(run.options.get("dry_run")), "started_at": run.started_at,
                                       "finished_at": run.finished_at, "counts": item_counts(run.id)}
        if (contact := get_state("last_qbwc_contact")) and (at := datetime.fromisoformat(contact["at"])):
            out |= {"last_qbwc_contact": at, "last_qbwc_contact_age_seconds": max(0, int((utcnow() - at).total_seconds()))}
        if refused := get_state("last_qbwc_auth_refused"):
            out["last_qbwc_auth_refused"] = datetime.fromisoformat(refused["at"])
    except Exception:  # noqa: BLE001 - the endpoint exists to report this
        out["database"] = False
    out["scheduler"] = scheduler_out(request, schedule, source)
    if probe:
        out["stocktrim"] = _probe(request)
    return Health.model_validate(out, from_attributes=True)
