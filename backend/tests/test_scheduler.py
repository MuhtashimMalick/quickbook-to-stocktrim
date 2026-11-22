"""Offline tests of the scheduler (stage 12): synthetic data only, QuickBooks is qbwc-kit's FakeWebConnector/FakeQuickBooks, StockTrim an in-memory httpx MockTransport,
the clock and every sleep are fake (nothing really waits)."""
import json
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text

from app import appstate, jobs
from app import scheduler as sched_mod
from app.api.v1.preflight import get_stocktrim_client_kw
from app.db import get_engine, session_scope
from app.models import PushRun, QBRecord, RunEvent, SyncCycle
from app.preflight.schema import Finding, Verdict
from app.scheduler import LOCK_KEY, CycleBusy, Schedule, Scheduler, next_run, recover_cycles, send_alert
from app.sync import engine

from .conftest import HEADERS, QB, rs_error
from .test_push import finish, mk, post
from .test_push_stage10 import ROWS10, Fake10, dry, run_all

UTC = timezone.utc
EXTRA_CUSTOMERS = [("Customer", f"CX{i}", {"ListID": f"CX{i}", "FullName": f"SENTINEL-x{i}"}) for i in range(30)]


class Clock:
    def __init__(self, start: datetime = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)):
        self.t = start

    def now(self) -> datetime:
        return self.t


class H:
    """env + fake StockTrim + fake QuickBooks + a scheduler whose clock/sleep are fake. `on_poll(h)` runs at every wait poll (default: the Web Connector connects once)."""

    def __init__(self, clean_db, tmp_path, rows=ROWS10, fake: Optional[Fake10] = None, **settings: Any):
        self.fake = fake or Fake10()
        self.env = mk(clean_db, tmp_path, self.fake, rows=[])
        self.env.settings.alert_webhook_url = SecretStr("https://hooks.example.test/SECRET-PATH")
        for k, v in settings.items():
            setattr(self.env.settings, k, v)
        self.qb = QB(entities=self._entities(rows))
        self.clock, self.polls, self.alerts = Clock(), 0, []
        self.on_poll: Callable[["H"], None] = lambda h: h.connect() if h.polls == 1 else None
        self.sch = Scheduler(self.env.settings, self.env.app.dependency_overrides[get_stocktrim_client_kw](), self.clock.now, self._sleep,
                             http_kw={"transport": httpx.MockTransport(self._webhook)})
        self.env.app.state.scheduler = self.sch

    @staticmethod
    def _entities(rows) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for e, _, p in rows:
            out[e].append(dict(p))
        return out

    def _webhook(self, req: httpx.Request) -> httpx.Response:
        self.alerts.append(json.loads(req.content))
        return httpx.Response(200)

    def _sleep(self, seconds: float) -> None:
        self.polls += 1
        self.clock.t += timedelta(seconds=seconds)
        engine.join(30)  # a running push finishes "while we sleep"
        self.on_poll(self)

    def connect(self, responder: Optional[Callable[[str], str]] = None) -> None:
        self.env.run(responder or self.qb)

    def baseline(self) -> None:
        """Initial load of all steps (reads through the Web Connector, then the push): the sync gate needs a completed real run."""
        self.env.client.post("/api/v1/jobs/all-reads", headers=HEADERS)
        self.connect()
        assert run_all(self.env, tuple(engine.STEPS))["status"] == "done"

    def cycle(self, trigger: str = "schedule") -> SyncCycle:
        self.polls = 0  # the Web Connector connects at the first poll of every cycle
        cid, conn = self.sch._begin(trigger)
        self.sch._execute(cid, conn)
        with session_scope() as s:
            return s.get(SyncCycle, cid)


@pytest.fixture
def h(clean_db, tmp_path):
    return H(clean_db, tmp_path)


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    orig = engine.start
    monkeypatch.setattr(engine, "start", lambda *a, **k: (seen.append((a, k)), orig(*a, **k))[1])
    return seen


def products(h: H, n: int) -> None:
    """Change n products in the fake QuickBooks (a changed QuantityOnHand = a changed push payload)."""
    for rec in h.qb.entities["ItemInventory"][:n]:
        rec["QuantityOnHand"] = str(int(float(rec["QuantityOnHand"])) + 100)


def n_jobs(job_type: Optional[str] = None) -> int:
    return len([j for j in jobs.list_jobs(limit=500) if job_type in (None, j.job_type)])


# ---- a full cycle ------------------------------------------------------------------------------

def test_full_cycle_done_queues_once_and_never_acknowledges(h, calls):
    h.baseline()
    before = sum(h.fake.posts.values())
    base_jobs = n_jobs()
    products(h, 2)
    row = h.cycle()
    assert (row.status, row.reason, row.trigger) == ("done", None, "schedule") and row.finished_at and row.push_run_id
    assert n_jobs() - base_jobs == 7 and n_jobs("sites") == 1  # the 7 read types, once each; sites is not scheduled
    assert row.summary["read_jobs"] == {"queued": 7, "reused": 0} and row.summary["planned_calls"] == 2
    assert row.summary["push"] == {"run_id": str(row.push_run_id), "status": "done", "items": {"done": 2, "failed": 0}}
    assert sum(h.fake.posts.values()) - before == 2  # exactly the two changed products were pushed
    with session_scope() as s:
        run = s.get(PushRun, row.push_run_id)
        assert (run.status, run.options["dry_run"], run.options["mode"], run.requested_by) == ("done", False, "sync", "scheduler")
    assert isinstance(row.summary["warnings"], dict)
    # the dry-run first, then the real run with ITS fingerprints; nothing but (settings, steps, dry_run, fp, plan_fp, kw, mode) is ever passed
    (d_args, d_kw), (r_args, r_kw) = calls[-2], calls[-1]
    assert d_kw == {"requested_by": "scheduler"} and set(r_kw) == {"requested_by", "history_since"} and len(d_args) == len(r_args) == 7  # no acknowledge_* / stamp_only; the real run only adds the dry-run window
    assert r_kw["history_since"].isoformat() == run.options["history_since"]
    assert d_args[2] is True and r_args[2] is False and d_args[6] == r_args[6] == "sync" and list(d_args[1]) == list(engine.STEPS)
    assert r_args[3:5] == (calls_fp(h, 0), calls_fp(h, 1))
    assert appstate.get_state("last_cycle")["status"] == "done" and h.alerts == []


def calls_fp(h: H, i: int) -> str:
    with session_scope() as s:
        run = s.scalars(select(PushRun).order_by(PushRun.started_at.desc())).first()  # the real run: carries both fingerprints
        return (run.preflight_fingerprint, run.options["plan_fingerprint"])[i]


def test_nothing_to_push_is_done(h):
    h.baseline()
    row = h.cycle()
    assert row.status == "done" and row.summary["planned_calls"] == 0 and row.push_run_id  # an empty real run still completes


# ---- abort paths ---------------------------------------------------------------------------------

def test_read_timeout_aborts_and_pending_jobs_are_not_duplicated(h):
    h.baseline()
    base = n_jobs()
    h.on_poll = lambda h: None  # the Web Connector never connects
    jobs.create_job("customers")  # still queued from an earlier cycle
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "read_timeout") and row.summary["read_jobs_pending"] == 7
    assert row.summary["read_jobs"] == {"queued": 6, "reused": 1} and n_jobs("customers") == 2 and n_jobs() - base == 7  # the earlier job is waited for, not doubled
    assert h.clock.t - datetime(2026, 11, 18, 1, 0, tzinfo=UTC) >= timedelta(hours=6) and h.polls >= 6 * 120
    again = h.cycle()  # all 7 are still queued: nothing new is queued
    assert again.summary["read_jobs"] == {"queued": 0, "reused": 7} and n_jobs() - base == 7
    assert [a["cycle_status"] for a in h.alerts] == ["aborted", "aborted"]
    assert not any(row.push_run_id for row in (row, again))


def test_read_job_error_aborts(h):
    h.baseline()
    h.on_poll = lambda h: h.connect(lambda xml: rs_error("CustomerQueryRs", 3000) if "CustomerQueryRq" in xml else h.qb(xml))
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "read_job_failed") and row.summary["read_jobs"]["queued"] == 7 and "customers" in row.summary["failed_jobs"]
    assert h.alerts[0]["reason"] == "read_job_failed"


def test_stale_snapshot_aborts(h):
    h.baseline()

    def reads_then_backdate(h: H) -> None:
        h.connect()
        with session_scope() as s:
            s.execute(text("UPDATE qb_records SET synced_at = '2000-01-01' WHERE entity = 'ItemInventory'"))
    h.on_poll = reads_then_backdate
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "stale_snapshot") and row.summary["stale_entities"] >= 1 and row.push_run_id is None


def test_no_completed_initial_run_aborts(h):
    h.connect()  # reads only: there is no completed real push yet, so a sync diff has no baseline
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "no_completed_initial_run")


def test_foreign_records_abort(clean_db, tmp_path):
    h = H(clean_db, tmp_path)
    h.baseline()
    h.fake.existing = 2  # StockTrim now holds two products we never pushed
    writes = len(h.fake.writes)
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "foreign_records") and row.push_run_id is None and len(h.fake.writes) == writes


def test_mass_requeue_aborts_and_is_never_acknowledged(clean_db, tmp_path, calls):
    h = H(clean_db, tmp_path, rows=[*ROWS10, *EXTRA_CUSTOMERS])
    h.baseline()
    h.fake.customers.clear()  # looks like a wipe
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "mass_requeue") and row.push_run_id is None
    assert all(len(a) == 7 and set(k) <= {"requested_by", "history_since"} for a, k in calls[-1:])


def test_blocking_verdict_aborts(h, monkeypatch):
    h.baseline()
    orig = engine.start

    def bad(*a, **k):
        d = orig(*a, **k)
        return {**d, "verdict": Verdict(ok_to_push=False, blocking=[Finding(code="quarantined", count=1)], warnings=[])}
    monkeypatch.setattr(engine, "start", bad)
    row = h.cycle()
    assert (row.status, row.reason, row.summary["blocking"]) == ("aborted", "preflight_not_ok", ["quarantined"])


def test_planned_calls_over_the_cap_abort(h):
    h.baseline()
    products(h, 2)
    h.env.settings.sync_max_planned_calls = 1
    row = h.cycle()
    assert (row.status, row.reason, row.summary["planned_calls"]) == ("aborted", "too_many_calls", 2) and row.push_run_id is None


def test_writes_flag_off_ends_dry_run_only(h):
    h.baseline()
    products(h, 1)
    h.env.settings.stocktrim_writes_enabled = False
    before = sum(h.fake.posts.values())
    row = h.cycle()
    assert (row.status, row.reason, row.push_run_id) == ("dry_run_only", "writes_disabled", None) and sum(h.fake.posts.values()) == before
    assert [a["cycle_status"] for a in h.alerts] == ["dry_run_only"]


def test_production_needs_scheduler_allow_production(clean_db, tmp_path):
    h = H(clean_db, tmp_path, stocktrim_environment="production")
    h.baseline()
    products(h, 1)
    before = sum(h.fake.posts.values())
    row = h.cycle()
    assert (row.status, row.reason) == ("dry_run_only", "production_not_allowed") and sum(h.fake.posts.values()) == before
    h.env.settings.scheduler_allow_production = True
    products(h, 1)
    row = h.cycle()
    assert (row.status, row.reason) == ("done", None)


def test_push_with_failures_fails_the_cycle(clean_db, tmp_path):
    fail = {"on": False}
    fake = Fake10(on_product=lambda n, b: httpx.Response(400, json={"errors": {"Other": ["SENTINEL-msg"]}}) if fail["on"] and b["productCodeReadable"] == "P1" else None)
    h = H(clean_db, tmp_path, fake=fake)
    h.baseline()
    products(h, 1)  # P1 is the first item
    fail["on"] = True
    row = h.cycle()
    assert (row.status, row.reason) == ("failed", "push_done_with_failures") and row.push_run_id and row.summary["push"]["items"] == {"done": 0, "failed": 1}
    assert h.alerts[0]["cycle_status"] == "failed" and h.alerts[0]["environment"] == "test" and h.alerts[0]["cycle_id"] == str(row.id)


def test_a_crash_is_recorded_by_type_only(h, monkeypatch):
    monkeypatch.setattr(jobs, "create_job", lambda *a, **k: 1 / 0)
    row = h.cycle()
    assert (row.status, row.reason) == ("failed", "ZeroDivisionError") and h.alerts


def test_shutdown_during_the_read_wait_interrupts(h):
    h.on_poll = lambda h: h.sch._stop.set()
    row = h.cycle()
    assert (row.status, row.reason) == ("interrupted", "shutdown")


# ---- lock, startup recovery, loop ---------------------------------------------------------------

def test_advisory_lock_prevents_a_second_cycle_and_a_stale_row_is_interrupted(h):
    cid, conn = h.sch._begin("manual")
    with pytest.raises(CycleBusy):
        h.sch._begin("schedule")
    h.sch._release(conn)  # the lock is free but the row is still `running` (a crashed process): holding the lock proves nobody is alive
    cid2, conn2 = h.sch._begin("schedule")
    h.sch._release(conn2)
    with session_scope() as s:
        old, new = s.get(SyncCycle, cid), s.get(SyncCycle, cid2)
    assert (old.status, old.reason) == ("interrupted", "stale") and old.finished_at and new.status == "running"
    # the lock itself held by another connection: no cycle, and recover_cycles leaves the `running` row alone
    with get_engine().connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        assert recover_cycles() == 0
        with pytest.raises(CycleBusy):
            h.sch._begin("manual")
        other.execute(text("SELECT pg_advisory_unlock_all()"))
    with session_scope() as s:
        assert s.get(SyncCycle, cid2).status == "running"
    assert recover_cycles() == 1


def test_stale_running_cycle_is_interrupted_at_startup(env):
    with session_scope() as s:
        s.add(SyncCycle(trigger="schedule", status="running"))
    with TestClient(env.app):
        pass
    with session_scope() as s:
        row = s.scalars(select(SyncCycle)).one()
    assert (row.status, row.reason) == ("interrupted", "restart") and row.finished_at


def test_scheduler_thread_starts_and_stops_with_the_app(make_env):
    env = make_env()
    env.settings.scheduler_enabled = True
    with TestClient(env.app):
        assert env.app.state.scheduler.running
        thread = env.app.state.scheduler._thread
    assert not thread.is_alive() and not env.app.state.scheduler.running
    off = make_env()
    with TestClient(off.app):
        assert not off.app.state.scheduler.running  # disabled by default


def test_loop_waits_for_the_daily_time_then_runs_one_cycle(h):
    h.env.settings.sync_daily_at = "02:00"
    ran: list[str] = []
    h.sch._begin = lambda trigger: (ran.append(trigger), (None, None))[1]
    h.sch._execute = lambda *a: h.sch._stop.set()
    h.clock.t = datetime(2026, 11, 18, 1, 58, tzinfo=UTC)
    slept: list[float] = []
    h.sch.sleep = lambda s: (slept.append(s), setattr(h.clock, "t", h.clock.t + timedelta(seconds=s)))
    h.sch._loop()
    assert ran == ["schedule"] and slept == [60, 60] and h.clock.t == datetime(2026, 11, 18, 2, 0, tzinfo=UTC)


def daily(hhmm, tz):
    return Schedule(enabled=True, every_days=1, time=hhmm, tz="UTC" if tz is UTC else tz.key, start_date=date(2020, 1, 1))


def test_next_run_uses_the_timezone_and_survives_dst():
    utc, toronto = UTC, ZoneInfo("America/Toronto")
    assert next_run(datetime(2026, 11, 18, 1, 0, tzinfo=utc), daily("02:00", utc)) == datetime(2026, 11, 18, 2, 0, tzinfo=utc)
    assert next_run(datetime(2026, 11, 18, 2, 0, tzinfo=utc), daily("02:00", utc)) == datetime(2026, 11, 19, 2, 0, tzinfo=utc)  # strictly after
    assert next_run(datetime(2026, 11, 18, 1, 0, tzinfo=utc), daily("02:00", toronto)) == datetime(2026, 11, 18, 7, 0, tzinfo=utc)  # EST = UTC-5
    assert next_run(datetime(2026, 7, 1, 1, 0, tzinfo=utc), daily("02:00", toronto)) == datetime(2026, 7, 1, 6, 0, tzinfo=utc)  # EDT = UTC-4
    gap = next_run(datetime(2026, 3, 8, 6, 0, tzinfo=utc), daily("02:30", toronto))  # 02:30 does not exist on 2026-03-08: it runs at the shifted instant, once
    assert gap.astimezone(utc) == datetime(2026, 3, 8, 7, 30, tzinfo=utc) and gap.hour == 3
    assert next_run(gap, daily("02:30", toronto)).astimezone(utc) == datetime(2026, 3, 9, 6, 30, tzinfo=utc)
    fall = next_run(datetime(2026, 11, 1, 4, 0, tzinfo=utc), daily("01:30", toronto))  # 01:30 happens twice on 2026-11-01: the first one
    assert fall.astimezone(utc) == datetime(2026, 11, 1, 5, 30, tzinfo=utc)
    assert next_run(fall, daily("01:30", toronto)).astimezone(utc) == datetime(2026, 11, 2, 6, 30, tzinfo=utc)  # not again an hour later


def test_settings_are_validated(tmp_path):
    from pydantic import ValidationError

    from app.config import Settings
    base = dict(_env_file=None, qbwc_username="u", qbwc_password="p", admin_token="t", qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), database_url="postgresql+psycopg://x/y")
    assert Settings(**base).scheduler_enabled is False and Settings(**base).sync_daily_at == "02:00"
    for bad in ({"sync_daily_at": "25:00"}, {"sync_daily_at": "2:00"}, {"scheduler_tz": "Mars/Base"}, {"scheduler_read_timeout_hours": 0}):
        with pytest.raises(ValidationError):
            Settings(**base, **bad)
    with pytest.raises(ValueError, match="unknown"):
        Scheduler(Settings(**base, scheduler_read_jobs="customers,bogus"))


# ---- alerts --------------------------------------------------------------------------------------

def test_alert_webhook_payload_and_failures(h, caplog):
    h.env.settings.alert_webhook_url = SecretStr("https://hooks.example.test/SECRET-PATH")
    with caplog.at_level(logging.ERROR):
        send_alert(h.env.settings, "aborted", "stale_snapshot", transport=httpx.MockTransport(h._webhook))
    assert h.alerts == [{"text": "QuickBook sync cycle aborted: stale_snapshot", "cycle_status": "aborted", "reason": "stale_snapshot", "environment": "test", "cycle_id": None}]
    assert "sync cycle aborted: stale_snapshot" in caplog.text
    caplog.clear()

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("SECRET-PATH unreachable")
    with caplog.at_level(logging.ERROR):
        send_alert(h.env.settings, "failed", "x", transport=httpx.MockTransport(boom))  # must not raise
        send_alert(h.env.settings, "failed", "x", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert "alert webhook failed: ConnectError" in caplog.text and "alert webhook failed: HTTPStatusError" in caplog.text and "SECRET-PATH" not in caplog.text
    h.env.settings.alert_webhook_url = None
    send_alert(h.env.settings, "failed", "x", transport=httpx.MockTransport(boom))  # no URL: log only, no request


def test_alert_has_no_record_contents(h):
    h.baseline()
    h.fake.existing = 2
    h.cycle()
    assert h.alerts and "SENTINEL" not in json.dumps(h.alerts)
    with session_scope() as s:
        assert "SENTINEL" not in json.dumps([c.summary for c in s.scalars(select(SyncCycle))]) and "SENTINEL" not in json.dumps(appstate.get_state("last_cycle"))


# ---- endpoints -----------------------------------------------------------------------------------

def test_run_now(h):
    h.env.settings.scheduler_enabled = False
    assert h.env.client.post("/api/v1/scheduler/run-now", headers=HEADERS).status_code == 403
    h.env.settings.scheduler_enabled = True
    cid, conn = h.sch._begin("schedule")  # a cycle is running
    r = h.env.client.post("/api/v1/scheduler/run-now", headers=HEADERS)
    assert (r.status_code, r.json()["detail"]) == (409, "cycle_running")
    h.sch._release(conn)
    recover_cycles()
    h.on_poll = lambda h: None  # no Web Connector: the manual cycle times out
    r = h.env.client.post("/api/v1/scheduler/run-now", headers=HEADERS)
    assert r.status_code == 202 and r.json()["status"] == "running"
    h.sch._worker.join(30)
    with session_scope() as s:
        row = s.get(SyncCycle, uuid.UUID(r.json()["cycle_id"]))
    assert (row.trigger, row.status, row.reason) == ("manual", "aborted", "read_timeout")
    assert h.fake.writes == []  # nothing was written to StockTrim


# ---- health ----------------------------------------------------------------------------------------

def test_health_shape_and_content(h):
    h.baseline()
    h.env.settings.scheduler_enabled = True
    h.cycle()
    r = h.env.get("/api/v1/health")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"database", "environment", "stocktrim_writes_enabled", "scheduler", "last_qbwc_contact", "last_qbwc_contact_age_seconds", "last_qbwc_auth_refused", "last_cycle", "jobs", "snapshot",
                      "push_run", "stocktrim"} and b["stocktrim"] is None  # the probe only runs with ?probe=true
    assert b["database"] is True and b["environment"] == "test" and b["stocktrim_writes_enabled"] is True
    assert b["scheduler"] == {"enabled": True, "running": False, "time": "02:00", "every_days": 1, "tz": "UTC", "schedule_enabled": True, "source": "env_default", "next_run_at": b["scheduler"]["next_run_at"]} and b["scheduler"]["next_run_at"]
    assert b["last_qbwc_contact"] and 0 <= b["last_qbwc_contact_age_seconds"] < 120
    assert b["last_cycle"]["status"] == "done" and b["last_cycle"]["reason"] is None and b["last_cycle"]["finished_at"]
    assert b["jobs"] == {} and set(b["snapshot"]["ItemInventory"]) == {"synced_at_max", "records"} and b["snapshot"]["ItemInventory"]["records"] == 6
    assert b["push_run"]["status"] == "done" and b["push_run"]["dry_run"] is False
    assert "SENTINEL" not in r.text


def test_health_when_nothing_ever_happened(env):
    b = env.get("/api/v1/health").json()
    assert (b["database"], b["last_qbwc_contact"], b["last_cycle"], b["push_run"], b["snapshot"], b["scheduler"]["next_run_at"]) == (True, None, None, None, {}, None)


def test_health_counts_open_jobs(env):
    jobs.create_job("customers")
    running = jobs.create_job("vendors")
    jobs.mark_running(running.id)
    assert env.get("/api/v1/health").json()["jobs"] == {"queued": 1, "running": 1}


def test_health_probe_is_one_get_and_only_on_request(h):
    seen: list[tuple[str, str]] = []

    def stocktrim(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(404, json={"title": "Not Found"})  # an unknown supplier: the API answers 404, which is a healthy answer
    h.sch.kw = {**h.sch.kw, "transport": httpx.MockTransport(stocktrim)}
    assert h.env.get("/api/v1/health").json()["stocktrim"] is None and seen == []  # no StockTrim call without ?probe=true
    assert h.env.get("/api/v1/health?probe=true").json()["stocktrim"] == {"reachable": True, "error": None}
    assert seen == [("GET", "/api/Suppliers")]
    h.sch.kw = {**h.sch.kw, "transport": httpx.MockTransport(lambda r: httpx.Response(503))}
    assert h.env.get("/api/v1/health?probe=true").json()["stocktrim"] == {"reachable": False, "error": "StockTrimUnavailable"}
    h.env.settings.stocktrim_auth_id = None
    assert h.env.get("/api/v1/health?probe=true").json()["stocktrim"] == {"reachable": False, "error": "not_configured"}


def test_health_reports_a_database_failure(env, monkeypatch):
    from app.api.v1 import ops

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ops, "session_scope", boom)
    b = env.get("/api/v1/health").json()
    assert b["database"] is False and b["jobs"] == {} and b["scheduler"]["enabled"] is False


def test_last_qbwc_contact_only_for_successful_logins_refusals_are_kept_apart(env):
    svc = env.app.state.qbwc_service
    assert appstate.get_state("last_qbwc_contact") is None
    assert env.connector(password="wrong").run_update(QB()).authenticated is False
    assert appstate.get_state("last_qbwc_contact") is None and appstate.get_state("last_qbwc_auth_refused")  # a refused login is NOT contact
    health = env.get("/api/v1/health").json()
    assert health["last_qbwc_contact"] is None and health["last_qbwc_auth_refused"]
    env.run(QB())
    first = appstate.get_state("last_qbwc_contact")["at"]
    env.run(QB())
    assert appstate.get_state("last_qbwc_contact")["at"] == first  # within a minute: not written again
    svc._contact_at.pop("last_qbwc_contact")
    env.run(QB())
    assert appstate.get_state("last_qbwc_contact")["at"] > first
    refused = appstate.get_state("last_qbwc_auth_refused")["at"]
    env.connector(password="wrong").run_update(QB())
    assert appstate.get_state("last_qbwc_auth_refused")["at"] == refused  # throttled too


# ---- migration 0007 / leakage ------------------------------------------------------------------------

def test_cycle_table_checks(clean_db):
    from sqlalchemy.exc import IntegrityError
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO sync_cycle (started_at, status, trigger) VALUES (now(), 'done', 'manual')"))
        c.execute(text("INSERT INTO sync_cycle (started_at, status, trigger) VALUES (now(), 'running', 'schedule')"))
    for sql in ("INSERT INTO sync_cycle (started_at, status, trigger) VALUES (now(), 'bogus', 'manual')",
                "INSERT INTO sync_cycle (started_at, status, trigger) VALUES (now(), 'done', 'cron')",
                "INSERT INTO sync_cycle (started_at, status, trigger) VALUES (now(), 'running', 'manual')"):  # a second running cycle
        with pytest.raises(IntegrityError), get_engine().begin() as c:
            c.execute(text(sql))


def test_leakage_sentinel(h, caplog):
    """Every place a cycle writes (rows, state, alerts, logs, health) is free of the synthetic record contents."""
    h.baseline()
    with caplog.at_level(logging.DEBUG):
        h.fake.existing = 2
        h.cycle()
        products(h, 1)
        h.fake.existing = 0
        h.cycle()
        health = h.env.get("/api/v1/health?probe=true").text
    with session_scope() as s:
        stored = json.dumps([[c.reason, c.summary] for c in s.scalars(select(SyncCycle))]) + json.dumps(appstate.get_state("last_cycle"))
    assert not any("SENTINEL" in t for t in (stored, health, caplog.text, json.dumps(h.alerts)))


# ---- stage 12b: review fixes -----------------------------------------------------------------------------

def writes(h: H) -> int:
    return len(h.fake.writes)


def real_runs() -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(PushRun).where(PushRun.options["dry_run"].as_boolean().is_(False)))))


def after_dry_run(monkeypatch, fn: Callable[[], None]) -> None:
    """Run fn() right after the scheduler dry-run returned (between the dry-run and the real run)."""
    orig = engine.start

    def wrapped(*a, **k):
        out = orig(*a, **k)
        if a[2] is True:
            fn()
        return out
    monkeypatch.setattr(engine, "start", wrapped)


def test_empty_assemblies_read_aborts_and_writes_nothing(h):
    h.baseline()
    h.qb.entities["ItemInventoryAssembly"] = []  # QuickBooks returned no assemblies: a successful, empty read
    w = writes(h)
    row = h.cycle()
    assert (row.status, row.reason, row.summary["empty_entities"]) == ("aborted", "empty_entity", ["assemblies"]) and row.push_run_id is None
    assert writes(h) == w and h.alerts[0]["reason"] == "empty_entity"


def test_an_empty_entity_without_a_ledger_is_fine(h):
    h.baseline()
    h.qb.entities["Customer"] = []
    with session_scope() as s:  # nothing was ever pushed for customers
        s.execute(text("DELETE FROM pushed_item WHERE entity = 'customer'"))
    assert h.cycle().reason != "empty_entity"


def test_mass_vanish_aborts(clean_db, tmp_path):
    h = H(clean_db, tmp_path, rows=[*ROWS10, *EXTRA_CUSTOMERS])
    h.baseline()
    h.qb.entities["Customer"] = [c for c in h.qb.entities["Customer"] if not c["ListID"].startswith("CX")]
    row = h.cycle()
    assert (row.status, row.reason, row.summary["vanished"]) == ("aborted", "mass_vanish", {"customers": 30}) and row.push_run_id is None


def test_mass_delete_needs_the_exact_acknowledgement(clean_db, tmp_path):
    asm = [("ItemInventoryAssembly", f"AX{i}", {"ListID": f"AX{i}", "Name": f"ASMX{i}", "QuantityOnHand": "0", "ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": "I1"}, "Quantity": "1"}]}) for i in range(30)]
    env = mk(clean_db, tmp_path, Fake10(), rows=[*ROWS10, *asm])
    assert run_all(env)["status"] == "done"
    with session_scope() as s:
        s.execute(delete(QBRecord).where(QBRecord.qb_id.like("AX%")))  # 30 assemblies (and their 30 edges) gone from QuickBooks
    r = post(env, steps=["bom"])
    assert (r.status_code, r.json()["detail"]) == (409, {"code": "mass_delete", "to_delete": 30})
    assert post(env, steps=["bom"], acknowledge_delete={"bom": 29}).json()["detail"]["code"] == "mass_delete"
    assert post(env, steps=["bom"], acknowledge_delete={"products": 30}).status_code == 422  # typed
    ok = post(env, steps=["bom"], acknowledge_delete={"bom": 30}).json()
    assert ok["sync"]["bom"]["to_delete"] == 30
    body = {"steps": ["bom"], "dry_run": False, "preflight_fingerprint": ok["preflight_fingerprint"], "plan_fingerprint": ok["plan_fingerprint"]}
    assert post(env, **body).json()["detail"]["code"] == "mass_delete"  # the real run needs it too
    out = finish(env, post(env, **body, acknowledge_delete={"bom": 30}).json()["run_id"])
    assert out["status"] == "done" and out["counts"] == {"bom_edge": {"done": 30}}


def test_a_failing_bookkeeping_write_still_alerts_and_the_next_cycle_runs(h, monkeypatch):
    h.connect()  # reads only: no completed push, so the cycle aborts (no_completed_initial_run)
    orig, boom = sched_mod._update_cycle, {"n": 0}

    def flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("db hiccup")
        return orig(*a, **k)
    monkeypatch.setattr(sched_mod, "_update_cycle", flaky)
    first = h.cycle()
    assert first.status == "running" and [a["reason"] for a in h.alerts] == ["no_completed_initial_run"]  # the row could not be updated, the alert went out
    second = h.cycle()  # holding the lock proves the `running` row is stale: it is interrupted, the cycle runs
    assert (second.status, second.reason) == ("aborted", "no_completed_initial_run")
    with session_scope() as s:
        old = s.get(SyncCycle, first.id)
    assert (old.status, old.reason) == ("interrupted", "stale")


def test_a_failed_push_link_still_records_the_event_and_the_truth(h, monkeypatch):
    h.baseline()
    products(h, 1)
    orig = sched_mod._update_cycle

    def flaky(cid, **v):
        if "push_run_id" in v:
            raise RuntimeError("db hiccup")  # the crash window: the real run is already going
        return orig(cid, **v)
    monkeypatch.setattr(sched_mod, "_update_cycle", flaky)
    row = h.cycle()
    assert row.status == "done" and row.push_run_id is None and row.summary["push"]["items"] == {"done": 1, "failed": 0}
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.cycle_id == row.id, RunEvent.code == "real_run_started")) == 1
    from app.api.v1.reads import wrote_state  # noqa: PLC0415
    assert wrote_state(None, row.summary, True) == "yes"
    assert wrote_state(None, {"push": {"run_id": row.summary["push"]["run_id"], "items": {"done": 0, "failed": 0}}}, True) == "unknown"  # the cycle died right after the start


def test_a_start_failure_alerts(h):
    def boom(trigger):
        raise RuntimeError("x")
    h.sch._begin = boom
    h.sch._run_scheduled()
    assert [(a["cycle_status"], a["reason"]) for a in h.alerts] == [("failed", "start_RuntimeError")]


def test_shutdown_between_dry_run_and_real_run_interrupts(h, monkeypatch):
    h.baseline()
    products(h, 1)
    before = real_runs()
    after_dry_run(monkeypatch, h.sch._stop.set)
    row = h.cycle()
    assert (row.status, row.reason) == ("interrupted", "shutdown") and row.push_run_id is None and real_runs() == before


def test_no_new_real_run_while_shutting_down(h):
    h.baseline()
    products(h, 1)
    d = dry(h.env, tuple(engine.STEPS))
    with session_scope() as s:
        run_id = s.scalars(select(PushRun).where(PushRun.status == "done")).first().id
    h.sch.stop(timeout=1)  # sets the engine flag
    body = {"steps": list(engine.STEPS), "dry_run": False, "preflight_fingerprint": d["preflight_fingerprint"], "plan_fingerprint": d["plan_fingerprint"]}
    r = post(h.env, **body)
    assert (r.status_code, r.json()["detail"]) == (503, "shutting_down") and not engine._ACTIVE.locked()
    assert h.env.client.post(f"/api/v1/push/runs/{run_id}/resume", headers=HEADERS).status_code == 503


def test_missed_slot_runs_once_at_startup_only_without_a_cycle_since(h, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(h.sch, "_run_scheduled", lambda: (ran.append("x"), h.sch._stop.set()))
    h.clock.t = datetime(2026, 11, 18, 2, 30, tzinfo=UTC)  # the 02:00 slot passed 30 minutes ago and nothing ran
    h.sch._loop()
    assert ran == ["x"]
    h.sch._stop.clear()
    ran.clear()
    with session_scope() as s:
        s.add(SyncCycle(trigger="schedule", status="done", started_at=datetime(2026, 11, 18, 2, 0, tzinfo=UTC)))
    h.sch.sleep = lambda s_: h.sch._stop.set()  # (waiting for the next slot)
    h.sch._loop()
    assert ran == []  # a cycle started at the slot: no double run
    h.sch._stop.clear()
    with session_scope() as s:
        s.execute(delete(SyncCycle))
    h.clock.t = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # 7 h after the slot: beyond the read timeout (6 h)
    h.sch._loop()
    assert ran == []


def test_a_running_manual_push_stops_the_cycle_before_the_dry_run(h):
    h.baseline()
    with session_scope() as s:
        s.add(PushRun(target="x", status="running", options={}))
    h.fake.log.clear()
    row = h.cycle()
    assert (row.status, row.reason) == ("aborted", "push_running") and h.fake.log == []  # no StockTrim read: the dry-run never started


def test_a_plan_change_between_dry_run_and_real_run_fails_the_cycle(h, monkeypatch):
    h.baseline()
    products(h, 1)

    def change_customer() -> None:
        with session_scope() as s:
            rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == "C1")).one()
            rec.payload = {**rec.payload, "Phone": "SENTINEL-changed"}
    after_dry_run(monkeypatch, change_customer)
    w = writes(h)
    row = h.cycle()
    assert (row.status, row.reason) == ("failed", "push_failed") and row.summary["push"]["error"] == "plan_fingerprint_mismatch" and writes(h) == w
    assert h.alerts[0]["push_error"] == "plan_fingerprint_mismatch"


def test_a_killed_lock_connection_aborts_before_the_real_run(h, monkeypatch):
    h.baseline()
    products(h, 1)
    cid, conn = h.sch._begin("schedule")
    pid = conn.scalar(text("SELECT pg_backend_pid()"))

    def kill() -> None:
        with get_engine().connect() as c:
            c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
    after_dry_run(monkeypatch, kill)
    before, w = real_runs(), writes(h)
    h.sch._execute(cid, conn)
    with session_scope() as s:
        row = s.get(SyncCycle, cid)
    assert (row.status, row.reason) == ("aborted", "lock_lost") and real_runs() == before and writes(h) == w


def test_reads_done_needs_every_job(h):
    j = jobs.create_job("customers")
    jobs.set_job_status(j.id, "done")
    assert h.sch._reads_done([j.id]) is True and h.sch._reads_done([j.id, j.id + 99]) is False  # a vanished job is not "done"


def test_a_cycle_spanning_midnight_keeps_the_dry_runs_window(h, monkeypatch):
    h.baseline()
    products(h, 1)
    day = date.today() + timedelta(days=1)

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(day.year, day.month, day.day, 0, 5, tzinfo=tz)
    after_dry_run(monkeypatch, lambda: monkeypatch.setattr(engine, "datetime", Tomorrow))  # midnight passes between the dry-run and the real run
    row = h.cycle()
    assert (row.status, row.reason) == ("done", None)
    with session_scope() as s:
        runs = list(s.scalars(select(PushRun).where(PushRun.requested_by == "scheduler")))
    assert len(runs) == 2 and len({r.options["history_since"] for r in runs}) == 1
    assert runs[0].options["history_since"] != engine.history_start(h.env.settings, day).isoformat()
