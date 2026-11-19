"""The sync schedule (stage B5), stored in app_state['schedule']: GET it, PUT a new one. Counts, codes and timestamps only."""
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ... import runlog
from ...appstate import set_state
from ...db import session_scope
from ...models import SyncCycle
from ...scheduler import Schedule, ScheduleIn, load_schedule, next_run
from ...tz import tz_of
from .ops import M

router = APIRouter()
MAX_FUTURE_DAYS = 365  # start_date may be at most this far AHEAD of today; any past date is fine (it only sets the phase)


class RunTime(M):
    utc: datetime
    local: datetime  # in the schedule's tz


class LastCycle(M):
    id: UUID
    status: str
    started_at: datetime


class ScheduleOut(M):
    enabled: bool
    every_days: Literal[1, 3, 7]
    time: str
    tz: str
    start_date: Optional[str]  # null for the env default and for daily schedules: the anchor is meaningless there
    updated_at: Optional[datetime]
    updated_by: Optional[str]
    source: Literal["database", "env_default"]
    next_runs: list[RunTime]  # the next 3 (empty while the schedule is switched off)
    ceiling_enabled: bool  # SCHEDULER_ENABLED: false = automatic cycles are disabled on the server whatever is stored here
    allow_production: bool
    last_cycle: Optional[LastCycle]


def _out(request: Request) -> ScheduleOut:
    st, sch = request.app.state.settings, request.app.state.scheduler
    schedule, source = load_schedule(st)
    runs: list[datetime] = []
    while schedule.enabled and len(runs) < 3:
        runs.append(next_run(runs[-1] if runs else sch.clock(), schedule))
    with session_scope() as s:
        last = s.execute(select(SyncCycle.id, SyncCycle.status, SyncCycle.started_at).order_by(SyncCycle.started_at.desc()).limit(1)).first()
    return ScheduleOut(**schedule.model_dump(exclude={"start_date"}), start_date=None if source == "env_default" or schedule.every_days == 1 else schedule.start_date.isoformat(), source=source,
                       next_runs=[RunTime(utc=r.astimezone(timezone.utc), local=r) for r in runs], ceiling_enabled=st.scheduler_enabled, allow_production=st.scheduler_allow_production,
                       last_cycle=last and LastCycle(id=last.id, status=last.status, started_at=last.started_at))


@router.get("/schedule", response_model=ScheduleOut)
def get_schedule(request: Request) -> Any:
    return _out(request)


@router.put("/schedule", response_model=ScheduleOut)
def put_schedule(body: ScheduleIn, request: Request) -> Any:
    """Validates, stores, logs a `schedule_changed` event (counts only) and wakes the scheduler loop so it applies at once. start_date defaults to today in the schedule's tz."""
    sch = request.app.state.scheduler
    now = sch.clock()
    today = now.astimezone(tz_of(body.tz)).date()
    start = body.start_date or today
    if start - today > timedelta(days=MAX_FUTURE_DAYS):
        raise HTTPException(422, "start_date_out_of_range")
    new = Schedule(**body.model_dump(exclude={"start_date"}), start_date=start, updated_at=now, updated_by=request.state.actor)
    set_state("schedule", new.model_dump(mode="json"))
    runlog.event("schedule_changed", actor=request.state.actor, counts={"every_days": new.every_days, "enabled": int(new.enabled)})
    sch.wake()
    return _out(request)
