"""Daily sync cycle: queue the QuickBooks read jobs, wait for the client's Web Connector to run them, check the snapshot is fresh, then a sync-mode dry-run,
automatic checks and (only if all pass) the real run with the dry-run's fingerprints. One in-process thread (single uvicorn worker) + a Postgres advisory lock, so a
second process can never run a cycle at the same time.
Never passed to the engine: acknowledge_foreign, acknowledge_requeue, stamp_only. Logs, alerts, cycle rows carry short codes and counts only (never record contents)."""
import logging
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import jobs
from .appstate import set_state
from .config import Settings
from .db import get_engine, session_scope
from .models import Job, PushRun, QBRecord, SyncCycle, utcnow
from .quickbooks.tasks import ENTITY_ALIASES, READ_JOB_TYPES
from .sync import engine
from .tz import tz_of

log = logging.getLogger("app.scheduler")
LOCK_KEY = 0x51425343  # pg advisory lock id of "a cycle is running"
POLL_SECONDS = 30
PUSH_FAILED = {"done_with_failures", "failed", "interrupted", "cancelled"}


class CycleBusy(Exception):
    """Another cycle is running (here or in another process)."""


class _End(Exception):
    """A cycle outcome other than done: status + short reason code (+ counts for the summary)."""

    def __init__(self, status: str, reason: str, **summary: Any):
        self.status, self.reason, self.summary = status, reason, summary
        super().__init__(reason)


def next_run(now: datetime, hhmm: str, tz: "timezone | ZoneInfo") -> datetime:
    """The next HH:MM wall-clock time in `tz` strictly after `now`. A time that does not exist (spring-forward gap) runs at the shifted instant; an ambiguous one (fall back) at its first occurrence."""
    local = now.astimezone(tz)
    h, m = map(int, hhmm.split(":"))
    for days in (0, 1):
        cand = datetime.combine(local.date() + timedelta(days=days), time(h, m), tzinfo=tz).astimezone(timezone.utc).astimezone(tz)  # UTC round-trip normalises a gap time
        if cand > now:
            return cand
    raise AssertionError("unreachable")  # pragma: no cover


def send_alert(settings: Settings, status: str, reason: str, **http_kw: Any) -> None:
    """ERROR log line always; a Slack-compatible webhook POST when ALERT_WEBHOOK_URL is set. Never raises, never carries record contents."""
    log.error("sync cycle %s: %s", status, reason)
    if not settings.alert_webhook_url:
        return
    try:
        with httpx.Client(timeout=5, **http_kw) as c:
            c.post(settings.alert_webhook_url.get_secret_value(), json={"text": f"QuickBook sync cycle {status}: {reason}", "cycle_status": status, "reason": reason}).raise_for_status()
    except Exception as e:  # noqa: BLE001 - an alert that cannot be sent is logged (type only: the URL is a secret)
        log.error("alert webhook failed: %s", type(e).__name__)


def recover_cycles() -> int:
    """Startup: one worker, so any `running` cycle is stale."""
    with session_scope() as s:
        n = s.execute(update(SyncCycle).where(SyncCycle.status == "running").values(status="interrupted", reason="restart", finished_at=utcnow())).rowcount
    if n:
        log.warning("marked %d stale sync cycle(s) interrupted after restart", n)
    return n


def _update_cycle(cid: UUID, **values: Any) -> None:
    with session_scope() as s:
        s.execute(update(SyncCycle).where(SyncCycle.id == cid).values(**values))


def item_counts(run_id: UUID) -> dict[str, int]:
    """Push items by status over all steps."""
    out: dict[str, int] = {}
    for per in engine.counts(run_id).values():
        for st, n in per.items():
            out[st] = out.get(st, 0) + n
    return out


class Scheduler:
    def __init__(self, settings: Settings, client_kw: Optional[dict[str, Any]] = None, clock: Optional[Callable[[], datetime]] = None,
                 sleep: Optional[Callable[[float], Any]] = None, http_kw: Optional[dict[str, Any]] = None):
        """clock/sleep/http_kw are injected by tests (no real waiting, mock webhook transport); client_kw = extra StockTrimClient arguments (tests: mock transport)."""
        self.settings, self.kw, self.http_kw = settings, client_kw or {}, http_kw or {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self.sleep = sleep or self._stop.wait
        self.tz = tz_of(settings.scheduler_tz)
        self.types = [t.strip() for t in settings.scheduler_read_jobs.split(",") if t.strip()]
        if unknown := [t for t in self.types if t not in READ_JOB_TYPES]:
            raise ValueError(f"SCHEDULER_READ_JOBS has unknown job type(s): {unknown}")
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None  # a manual cycle

    # ---- lifecycle
    def start(self) -> None:
        if self.settings.scheduler_enabled and not self._thread:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in (self._thread, self._worker):
            if t:
                t.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            due = next_run(self.clock(), self.settings.sync_daily_at, self.tz)
            while (rem := (due - self.clock()).total_seconds()) > 0:
                self.sleep(min(rem, 60))
                if self._stop.is_set():
                    return
            try:
                self._execute(*self._begin("schedule"))
            except CycleBusy:
                log.warning("scheduled sync cycle skipped: another cycle is running")
            except Exception as e:  # noqa: BLE001
                log.error("scheduled sync cycle could not start: %s", type(e).__name__)

    # ---- one cycle
    def trigger_now(self) -> UUID:
        """Manual trigger: claims the cycle here (CycleBusy -> 409), runs it in the background."""
        cid, conn = self._begin("manual")
        self._worker = threading.Thread(target=self._execute, args=(cid, conn), name="sync-cycle", daemon=False)
        self._worker.start()
        return cid

    def _begin(self, trigger: str) -> tuple[UUID, Any]:
        """Take the advisory lock (a dedicated connection holds it for the whole cycle) and insert the `running` row."""
        conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            if not conn.scalar(select(func.pg_try_advisory_lock(LOCK_KEY))):
                raise CycleBusy
        except BaseException:
            conn.close()
            raise
        try:
            row = SyncCycle(trigger=trigger, status="running")
            with session_scope() as s:
                s.add(row)
            return row.id, conn
        except IntegrityError as e:  # the partial unique index: a stale `running` row
            self._release(conn)
            raise CycleBusy from e
        except BaseException:
            self._release(conn)
            raise

    @staticmethod
    def _release(conn: Any) -> None:
        try:
            conn.scalar(select(func.pg_advisory_unlock(LOCK_KEY)))
        finally:
            conn.close()

    def _execute(self, cid: UUID, conn: Any) -> None:
        ctx: dict[str, Any] = {"summary": {}}
        status, reason = "failed", "crashed"
        try:
            self._cycle(cid, ctx)
            status, reason = "done", None
        except _End as e:
            status, reason = e.status, e.reason
            ctx["summary"] |= e.summary
        except Exception as e:  # noqa: BLE001 - the cycle row carries the exception TYPE only
            status, reason = "failed", type(e).__name__
        try:
            _update_cycle(cid, status=status, reason=reason, finished_at=utcnow(), summary=ctx["summary"])
            set_state("last_cycle", {"id": str(cid), "status": status, "reason": reason, "finished_at": utcnow().isoformat(), "summary": ctx["summary"]})
            if status != "done":
                send_alert(self.settings, status, reason or "-", **self.http_kw)
            else:
                log.info("sync cycle %s done %s", cid, ctx["summary"])
        finally:
            self._release(conn)

    def _wait(self, until: Callable[[], bool], deadline: Optional[datetime] = None) -> bool:
        """Poll until(); False on the deadline. Shutdown ends the cycle `interrupted`."""
        while not until():
            if deadline and self.clock() >= deadline:
                return False
            self.sleep(POLL_SECONDS)
            if self._stop.is_set():
                raise _End("interrupted", "shutdown")
        return True

    def _cycle(self, cid: UUID, ctx: dict[str, Any]) -> None:
        s_, kw = self.settings, self.kw
        with session_scope() as s:
            started = s.get(SyncCycle, cid).started_at
        # (a) queue the read jobs once: a type that is still queued/running from an earlier cycle is waited for, not queued again
        with session_scope() as s:
            pending = {j.job_type: j.id for j in s.scalars(select(Job).where(Job.job_type.in_(self.types), Job.status.in_(("queued", "running"))))}
        ids = [pending.get(t) or jobs.create_job(t).id for t in self.types]
        ctx["summary"]["read_jobs"] = {"queued": len(ids) - len(pending), "reused": len(pending)}
        # (b) wait for the Web Connector to run them
        deadline = self.clock() + timedelta(hours=s_.scheduler_read_timeout_hours)
        if not self._wait(lambda: self._reads_done(ids), deadline):
            raise _End("aborted", "read_timeout", read_jobs_pending=self._open_jobs(ids))
        # (c) freshness: every pulled entity must have been rewritten since the cycle began
        # ponytail: an entity with no rows (empty in QuickBooks) is skipped; a job that finished `done` already proved the read
        with session_scope() as s:
            newest = dict(s.execute(select(QBRecord.entity, func.max(QBRecord.synced_at)).group_by(QBRecord.entity)).all())
        stale = [t for t in self.types if t != "sites" and (m := max((newest[e] for e in ENTITY_ALIASES[t] if e in newest), default=None)) is not None and m <= started]
        if stale:
            raise _End("aborted", "stale_snapshot", stale_entities=len(stale))
        # (d) sync: dry-run, automatic checks, real run
        steps = list(engine.STEPS)
        try:
            d = engine.start(s_, steps, True, None, None, kw, "sync")  # the scheduler never passes acknowledge_foreign / acknowledge_requeue / stamp_only
        except engine.PushError as e:
            raise _End("aborted", e.code) from None  # no_completed_initial_run, mass_requeue, requeue_requires_step, ...
        v = d["verdict"]
        ctx["summary"]["planned_calls"] = d["planned_calls"]
        ctx["summary"]["sync"] = {st: {k: n for k, n in c.items() if isinstance(n, int)} for st, c in d["sync"].items()}
        if v.foreign_hash or any(w.code == "foreign_in_plan" for w in v.warnings):
            raise _End("aborted", "foreign_records")
        if not v.ok_to_push or v.blocking:
            raise _End("aborted", "preflight_not_ok", blocking=[f.code for f in v.blocking])
        if d["planned_calls"] > s_.sync_max_planned_calls:
            raise _End("aborted", "too_many_calls")
        if not s_.stocktrim_writes_enabled:
            raise _End("dry_run_only", "writes_disabled")
        if s_.stocktrim_environment == "production" and not s_.scheduler_allow_production:
            raise _End("dry_run_only", "production_not_allowed")
        try:
            r = engine.start(s_, steps, False, d["preflight_fingerprint"], d["plan_fingerprint"], kw, "sync")
        except engine.PushError as e:
            raise _End("aborted", e.code) from None
        run_id = r["run_id"]
        _update_cycle(cid, push_run_id=run_id)
        self._wait(lambda: self._push_status(run_id) != "running")
        status = self._push_status(run_id)
        ctx["summary"]["push"] = {"status": status, "items": item_counts(run_id)}
        if status in PUSH_FAILED:
            raise _End("failed", f"push_{status}")

    # ---- small DB reads
    def _reads_done(self, ids: list[int]) -> bool:
        with session_scope() as s:
            rows = list(s.execute(select(Job.job_type, Job.status).where(Job.id.in_(ids))))
        if bad := [t for t, st in rows if st in ("error", "unsupported") and t != "sites"]:
            raise _End("aborted", "read_job_failed", failed_jobs=bad)
        return all(st in jobs.TERMINAL for _, st in rows)

    @staticmethod
    def _open_jobs(ids: list[int]) -> int:
        with session_scope() as s:
            return s.scalar(select(func.count()).select_from(Job).where(Job.id.in_(ids), Job.status.notin_(list(jobs.TERMINAL)))) or 0

    @staticmethod
    def _push_status(run_id: UUID) -> str:
        with session_scope() as s:
            return s.scalar(select(PushRun.status).where(PushRun.id == run_id)) or "failed"
