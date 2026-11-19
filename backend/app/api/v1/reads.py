"""Read-only endpoints of the admin UI (stage B4): cycles, events, settings, reason catalog, overview. Counts, codes, timestamps and short labels only:
never secrets, StockTrim credentials, payloads, raw QuickBooks records or error bodies."""
import os
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select, tuple_

from ...db import session_scope
from ...models import AppUser, Job, PushRun, RunEvent, SyncCycle
from ...quickbooks.tasks import READ_JOB_TYPES
from ...reasons import CATALOG, describe
from ...scheduler import item_counts, load_schedule
from .ops import CycleOut, Freshness, Health, M, PushRunHealth, SchedulerOut, health
from .push import PushRunOut

router = APIRouter()


class ReasonOut(M):
    title: str
    explanation: str
    next_step: str


def before_clause(s: Any, model: Any, before: Optional[str]) -> list[Any]:
    """Keyset cursor for lists ordered by (started_at desc, id desc): `before` is an ISO timestamp or the id of the last row already seen."""
    if before is None:
        return []
    try:
        ts = datetime.fromisoformat(before)
        return [model.started_at < (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))]
    except ValueError:
        pass
    try:
        uid = UUID(before)
    except ValueError:
        raise HTTPException(422, "invalid_before") from None
    ts = s.scalar(select(model.started_at).where(model.id == uid))
    if ts is None:
        raise HTTPException(422, "unknown_before")
    return [tuple_(model.started_at, model.id) < tuple_(ts, uid)]


# ---- cycles
class JobBrief(M):
    id: int
    job_type: str
    status: str
    finished_at: Optional[datetime]


class CycleDetail(CycleOut):
    reason_info: Optional[ReasonOut]
    jobs: list[JobBrief]  # the read jobs created while the cycle ran
    push_run: Optional[PushRunOut]
    wrote_to_stocktrim: bool  # a real (non-dry-run) push of the cycle completed at least one item


@router.get("/cycles", response_model=list[CycleOut])
def list_cycles(limit: int = Query(50, ge=1, le=100), before: Optional[str] = Query(None, max_length=64)) -> Any:
    with session_scope() as s:
        rows = s.scalars(select(SyncCycle).where(*before_clause(s, SyncCycle, before)).order_by(SyncCycle.started_at.desc(), SyncCycle.id.desc()).limit(limit)).all()
        return [CycleOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/cycles/{cycle_id}", response_model=CycleDetail)
def get_cycle(request: Request, cycle_id: UUID) -> Any:
    from ...sync import engine  # noqa: PLC0415 - same lazy import as the push routes

    with session_scope() as s:
        c = s.get(SyncCycle, cycle_id)
        if c is None:
            raise HTTPException(404, "cycle_not_found")
        end = [Job.created_at <= c.finished_at] if c.finished_at else []
        jobs = s.scalars(select(Job).where(Job.created_at >= c.started_at, Job.job_type.in_(READ_JOB_TYPES), *end).order_by(Job.id).limit(100)).all()
        out = CycleOut.model_validate(c, from_attributes=True).model_dump() | {"reason_info": c.reason and describe(c.reason), "jobs": jobs, "push_run": None, "wrote_to_stocktrim": False}
    if c.push_run_id:
        try:
            run = out["push_run"] = engine.progress(request.app.state.settings, c.push_run_id)
        except engine.PushError:  # the run was pruned
            return CycleDetail.model_validate(out, from_attributes=True)
        out["wrote_to_stocktrim"] = not run["dry_run"] and sum(v.get("done", 0) for v in run["counts"].values()) > 0
    return CycleDetail.model_validate(out, from_attributes=True)


# ---- events
class EventOut(M):
    id: int
    at: datetime
    cycle_id: Optional[UUID]
    run_id: Optional[UUID]
    level: str
    code: str
    title: str
    entity: Optional[str]
    counts: Optional[dict[str, int]]
    actor: Optional[str]


class EventsOut(M):
    items: list[EventOut]
    last_id: int  # pass as after_id on the next poll


@router.get("/events", response_model=EventsOut)
def list_events(cycle_id: Optional[UUID] = None, run_id: Optional[UUID] = None, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)) -> Any:
    q = select(RunEvent).where(RunEvent.id > after_id).order_by(RunEvent.id).limit(limit)
    if cycle_id:
        q = q.where(RunEvent.cycle_id == cycle_id)
    if run_id:
        q = q.where(RunEvent.run_id == run_id)
    with session_scope() as s:
        rows = s.scalars(q).all()
    items = [EventOut(**{c: getattr(r, c) for c in ("id", "at", "cycle_id", "run_id", "level", "code", "entity", "counts", "actor")}, title=describe(r.code)["title"]) for r in rows]
    return EventsOut(items=items, last_id=rows[-1].id if rows else after_id)


# ---- reason catalog
@router.get("/reason-codes", response_model=dict[str, ReasonOut])
def reason_codes() -> Any:
    return CATALOG


# ---- settings
class UserOut(M):
    username: str
    is_active: bool
    last_login_at: Optional[datetime]


class ScheduleSummary(M):
    enabled: bool
    every_days: int
    time: str
    tz: str
    source: str  # database | env_default


class SettingsOut(M):
    environment: Literal["test", "production"]
    stocktrim_writes_enabled: bool
    scheduler_enabled: bool
    scheduler_allow_production: bool
    schedule: ScheduleSummary
    sync_max_planned_calls: int
    stocktrim_host: Optional[str]  # hostname only: never credentials
    stocktrim_max_rps: float
    read_jobs: list[str]
    sales_history_years: int
    negative_stock_policy: str
    alert_webhook_configured: bool
    history_retention_days: int
    version: Optional[str]  # APP_VERSION or GIT_SHA from the environment, when the image sets one
    users: list[UserOut]


@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request) -> Any:
    st, sch = request.app.state.settings, request.app.state.scheduler
    schedule, source = load_schedule(st)
    with session_scope() as s:
        users = [UserOut.model_validate(u, from_attributes=True) for u in s.scalars(select(AppUser).order_by(AppUser.username))]
    return SettingsOut(
        environment=st.stocktrim_environment, stocktrim_writes_enabled=st.stocktrim_writes_enabled, scheduler_enabled=st.scheduler_enabled,
        scheduler_allow_production=st.scheduler_allow_production, schedule=ScheduleSummary(source=source, **schedule.model_dump(include={"enabled", "every_days", "time", "tz"})), sync_max_planned_calls=st.sync_max_planned_calls,
        stocktrim_host=urlparse(st.stocktrim_base_url).hostname, stocktrim_max_rps=st.stocktrim_max_rps, read_jobs=sch.types, sales_history_years=st.sales_history_years,
        negative_stock_policy=st.negative_stock_policy, alert_webhook_configured=st.alert_webhook is not None, history_retention_days=st.history_retention_days,
        version=os.environ.get("APP_VERSION") or os.environ.get("GIT_SHA") or None, users=users)


# ---- overview
class Running(M):
    id: UUID
    since: datetime
    phase: Optional[str]  # code of the newest run event


class Overview(M):
    database: bool
    scheduler: SchedulerOut
    last_cycle: Optional[CycleOut]
    last_qbwc_contact: Optional[datetime]
    last_qbwc_contact_age_seconds: Optional[int]
    jobs: dict[str, int]
    snapshot: dict[str, Freshness]
    running_cycle: Optional[Running]
    running_push: Optional[Running]
    last_real_push_run: Optional[PushRunHealth]


def _running(s: Any, model: Any, event_col: Any) -> Optional[Running]:
    row = s.scalars(select(model).where(model.status == "running").limit(1)).first()
    if row is None:
        return None
    return Running(id=row.id, since=row.started_at, phase=s.scalar(select(RunEvent.code).where(event_col == row.id).order_by(RunEvent.id.desc()).limit(1)))


@router.get("/overview", response_model=Overview)
def overview(request: Request) -> Any:
    h: Health = health(request)  # the /health queries are the single source: no duplicates here
    out: dict[str, Any] = h.model_dump(include=set(Overview.model_fields) & set(Health.model_fields)) | {"running_cycle": None, "running_push": None, "last_real_push_run": None}
    if h.database:
        with session_scope() as s:
            out["running_cycle"], out["running_push"] = _running(s, SyncCycle, RunEvent.cycle_id), _running(s, PushRun, RunEvent.run_id)
            real = s.scalars(select(PushRun).where(PushRun.options["dry_run"].as_boolean().is_(False)).order_by(PushRun.started_at.desc()).limit(1)).first()
        if real:
            out["last_real_push_run"] = {"run_id": real.id, "status": real.status, "dry_run": False, "started_at": real.started_at, "finished_at": real.finished_at, "counts": item_counts(real.id)}
    return Overview.model_validate(out)
