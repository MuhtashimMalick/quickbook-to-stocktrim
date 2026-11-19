"""Stage B5 (the stored sync schedule), offline: synthetic data, injected clock, no real sleeps."""
import time
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, select

from app import appstate
from app.db import session_scope
from app.models import RunEvent, SyncCycle
from app.reasons import CATALOG
from app.scheduler import Schedule, Scheduler, env_schedule, load_schedule, next_run, prev_run

from .conftest import HEADERS
from .test_auth import add_user, bearer, fast_hash, login, token_of  # noqa: F401 (fast_hash: autouse fixture)
from .test_exposure import XFF, proxied  # noqa: F401 (proxied: fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BODY = {"enabled": True, "every_days": 3, "time": "02:00", "tz": "UTC", "start_date": "2026-11-20"}


def sch(every, start, hhmm="02:00", tz="UTC", enabled=True, **kw) -> Schedule:
    return Schedule(enabled=enabled, every_days=every, time=hhmm, tz=tz, start_date=date.fromisoformat(start), **kw)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def store(s: Schedule) -> None:
    appstate.set_state("schedule", s.model_dump(mode="json"))


def runs(s: Schedule, now: datetime, n: int = 3) -> list[datetime]:
    out = []
    for _ in range(n):
        out.append(now := next_run(now, s))
    return [r.astimezone(UTC) for r in out]


# ---- next_run / prev_run -------------------------------------------------------------------------
def test_every_n_days_across_a_month_boundary_in_utc():
    assert runs(sch(3, "2026-01-30"), utc(2026, 1, 31)) == [utc(2026, 2, 2, 2), utc(2026, 2, 5, 2), utc(2026, 2, 8, 2)]
    assert runs(sch(7, "2026-01-26"), utc(2026, 1, 27)) == [utc(2026, 2, 2, 2), utc(2026, 2, 9, 2), utc(2026, 2, 16, 2)]  # weekly
    assert runs(sch(1, "2026-01-01"), utc(2026, 1, 31, 3)) == [utc(2026, 2, 1, 2), utc(2026, 2, 2, 2), utc(2026, 2, 3, 2)]
    assert next_run(utc(2026, 2, 2, 2), sch(3, "2026-01-30")) == utc(2026, 2, 5, 2)  # strictly after a slot


def test_anchor_in_the_future_in_the_past_and_a_first_slot_that_already_passed():
    s = sch(3, "2026-11-20")
    assert runs(s, NOW)[0] == utc(2026, 11, 20, 2) and prev_run(NOW, s) is None  # not started yet: no slot before the anchor
    assert runs(sch(3, "2025-06-02"), NOW) == [utc(2026, 11, 21, 2), utc(2026, 11, 24, 2), utc(2026, 11, 27, 2)]  # aligned to the old anchor (2025-06-02 + 3k)
    assert runs(sch(3, "2026-11-18"), NOW)[0] == utc(2026, 11, 21, 2)  # saved today after 02:00: first run is the next slot
    assert runs(sch(3, "2026-11-18", "13:00"), NOW)[0] == utc(2026, 11, 18, 13)  # still ahead today: runs today
    assert prev_run(utc(2026, 11, 21, 3), sch(3, "2026-11-18")) == utc(2026, 11, 21, 2) and prev_run(utc(2026, 11, 20, 3), sch(3, "2026-11-18")) == utc(2026, 11, 18, 2)


@pytest.mark.parametrize("tz,start,hhmm,now,expected", [
    # US spring forward 2026-03-08: 02:30 does not exist -> the shifted instant (03:30 CDT)
    ("America/Chicago", "2026-03-05", "02:30", utc(2026, 3, 6), [utc(2026, 3, 8, 8, 30), utc(2026, 3, 11, 7, 30), utc(2026, 3, 14, 7, 30)]),
    # US fall back 2026-11-01: 01:30 happens twice -> the first (CDT)
    ("America/Chicago", "2026-10-29", "01:30", utc(2026, 10, 30), [utc(2026, 11, 1, 6, 30), utc(2026, 11, 4, 7, 30), utc(2026, 11, 7, 7, 30)]),
    # UK spring forward 2026-03-29: 01:30 does not exist -> 02:30 BST
    ("Europe/London", "2026-03-26", "01:30", utc(2026, 3, 27), [utc(2026, 3, 29, 1, 30), utc(2026, 4, 1, 0, 30), utc(2026, 4, 4, 0, 30)]),
    # UK fall back 2026-10-25: 01:30 happens twice -> the first (BST)
    ("Europe/London", "2026-10-22", "01:30", utc(2026, 10, 23), [utc(2026, 10, 25, 0, 30), utc(2026, 10, 28, 1, 30), utc(2026, 10, 31, 1, 30)]),
])
def test_dst_in_real_zones(tz, start, hhmm, now, expected):
    assert runs(sch(3, start, hhmm, tz), now) == expected


# ---- loading ---------------------------------------------------------------------------------------
def test_without_a_stored_schedule_the_env_default_is_used_and_nothing_is_written(env):
    env.settings.sync_daily_at, env.settings.scheduler_tz = "03:15", "America/Chicago"
    s, source = load_schedule(env.settings)
    assert source == "env_default" and (s.enabled, s.every_days, s.time, s.tz) == (True, 1, "03:15", "America/Chicago") and s == env_schedule(env.settings)
    assert appstate.get_state("schedule") is None
    g = env.get("/api/v1/schedule").json()
    assert (g["source"], g["every_days"], g["time"], g["tz"], g["ceiling_enabled"]) == ("env_default", 1, "03:15", "America/Chicago", False)
    assert appstate.get_state("schedule") is None


# ---- catch-up and the loop -----------------------------------------------------------------------------
@pytest.fixture
def loop(env):
    """A Scheduler with a fake clock; `ran` records scheduled cycles (nothing really runs); `hook` is the sleep."""
    class L:
        clock, ran, hook = NOW, [], None

    L.s = Scheduler(env.settings, clock=lambda: L.clock, sleep=lambda secs: L.hook(secs))
    L.s._run_scheduled = lambda: (L.ran.append(L.clock), L.s._stop.set())
    return L


def test_catch_up_for_an_n_day_schedule_runs_once_and_only_on_slot_days(loop):
    s = sch(3, "2026-11-16", updated_at=utc(2026, 11, 1))  # slots: Nov 16, 19, 22
    loop.clock = utc(2026, 11, 19, 2, 30)
    assert loop.s._missed_slot(s) is True
    loop.clock = utc(2026, 11, 18, 2, 30)  # not a slot day: the last slot (Nov 16) is long past the read timeout
    assert loop.s._missed_slot(s) is False
    loop.clock = utc(2026, 11, 19, 9, 0)  # 7 h after the slot > 6 h read timeout
    assert loop.s._missed_slot(s) is False
    loop.clock = utc(2026, 11, 19, 2, 30)
    assert loop.s._missed_slot(sch(3, "2026-11-16", updated_at=utc(2026, 11, 19, 2, 10))) is False  # saved after the slot: its first run is the next slot
    with session_scope() as db:
        db.add(SyncCycle(trigger="schedule", status="done", started_at=utc(2026, 11, 19, 2, 0)))
    assert loop.s._missed_slot(s) is False  # a cycle started at the slot: no double run


def test_loop_catches_up_once_at_start_from_the_stored_schedule(loop):
    store(sch(3, "2026-11-16", updated_at=utc(2026, 11, 1)))
    loop.clock = utc(2026, 11, 19, 2, 30)
    loop.hook = lambda secs: loop.s._stop.set()
    loop.s._loop()
    assert loop.ran == [utc(2026, 11, 19, 2, 30)]
    loop.s._stop.clear()
    loop.ran.clear()
    with session_scope() as db:
        db.add(SyncCycle(trigger="schedule", status="done", started_at=utc(2026, 11, 19, 2, 31)))
    loop.s._loop()
    assert loop.ran == []


def test_loop_rereads_the_schedule_at_every_wake_up(loop):
    store(sch(1, "2026-11-01", updated_at=utc(2026, 11, 1)))
    loop.clock = utc(2026, 11, 18, 1, 59)
    slept: list[float] = []

    def hook(secs):
        slept.append(secs)
        if len(slept) == 1:  # an admin saves a weekly schedule starting tomorrow while the loop sleeps; the old 02:00 slot then passes
            store(sch(7, "2026-11-19", updated_at=utc(2026, 11, 18, 1, 59)))
            loop.clock = utc(2026, 11, 18, 2, 0, 30)
        else:
            loop.s._stop.set()
    loop.hook = hook
    loop.s._loop()
    assert loop.ran == [] and slept == [60, 60]  # the old slot never fires; the next one is tomorrow


def test_disabled_schedule_pauses_automatic_runs_and_run_now_still_works(env, loop, monkeypatch):
    store(sch(1, "2026-11-01", enabled=False, updated_at=utc(2026, 11, 1)))
    loop.clock = utc(2026, 11, 18, 2, 30)  # a slot was missed, yet nothing runs
    n: list[float] = []
    loop.hook = lambda secs: (n.append(secs), loop.s._stop.set() if len(n) > 2 else None)
    loop.s._loop()
    assert loop.ran == [] and n == [60, 60, 60]
    env.settings.scheduler_enabled = True  # the ceiling allows it
    cid = uuid.uuid4()
    monkeypatch.setattr(env.app.state.scheduler, "trigger_now", lambda actor="cli": cid)
    r = env.client.post("/api/v1/scheduler/run-now", headers=HEADERS)
    assert r.status_code == 202 and r.json()["cycle_id"] == str(cid)


def test_ceiling_off_means_no_automatic_cycles_whatever_the_database_says(env):
    store(sch(1, "2026-11-01", updated_at=utc(2026, 11, 1)))  # enabled=true
    assert env.settings.scheduler_enabled is False
    with env.client as c:  # lifespan: the scheduler would start here if the ceiling allowed it
        assert not env.app.state.scheduler.running
        g = c.get("/api/v1/schedule", headers=HEADERS).json()
        h = c.get("/api/v1/health", headers=HEADERS).json()["scheduler"]
    assert (g["enabled"], g["ceiling_enabled"], g["source"]) == (True, False, "database") and g["next_runs"]
    assert (h["enabled"], h["schedule_enabled"], h["next_run_at"]) == (False, True, None)


def test_wake_cuts_the_idle_wait_short(env):
    s = Scheduler(env.settings)
    s.wake()
    t = time.monotonic()
    s._nap(30)
    assert time.monotonic() - t < 2 and not s._wake.is_set()


# ---- API -----------------------------------------------------------------------------------------------
@pytest.fixture
def api(env, proxied):  # noqa: F811
    add_user()
    env.app.state.scheduler.clock = lambda: NOW
    return proxied, bearer(token_of(login(proxied)))


def put(api, body):
    return api[0].put("/api/v1/schedule", json=body, headers=api[1])


def test_put_stores_logs_wakes_and_get_returns_the_same(env, api):
    r = put(api, BODY)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["enabled"], b["every_days"], b["time"], b["tz"], b["start_date"], b["source"], b["updated_by"]) == (True, 3, "02:00", "UTC", "2026-11-20", "database", "alice")
    assert [x["utc"] for x in b["next_runs"]] == ["2026-11-20T02:00:00Z", "2026-11-23T02:00:00Z", "2026-11-26T02:00:00Z"]
    assert (b["ceiling_enabled"], b["allow_production"], b["last_cycle"]) == (False, False, None)
    assert api[0].get("/api/v1/schedule", headers=api[1]).json() == b
    assert env.app.state.scheduler._wake.is_set()  # the loop re-reads at once
    with session_scope() as db:
        ev = db.scalars(select(RunEvent).where(RunEvent.code == "schedule_changed")).all()
    assert len(ev) == 1 and ev[0].counts == {"every_days": 3, "enabled": 1} and ev[0].actor == "alice" and "schedule_changed" in CATALOG
    hl = api[0].get("/api/v1/health", headers=api[1]).json()["scheduler"]
    assert (hl["time"], hl["every_days"], hl["source"], hl["schedule_enabled"]) == ("02:00", 3, "database", True)
    st = api[0].get("/api/v1/settings", headers=api[1]).json()
    assert st["schedule"] == {"enabled": True, "every_days": 3, "time": "02:00", "tz": "UTC", "source": "database"} and "sync_daily_at" not in st


def test_put_defaults_the_anchor_to_today_in_the_zone_and_the_first_slot_is_the_next_one(api):
    b = put(api, {"enabled": True, "every_days": 3, "time": "02:00", "tz": "UTC"}).json()  # NOW = 2026-11-18 12:00: today's 02:00 has passed
    assert b["start_date"] == "2026-11-18" and b["next_runs"][0]["utc"] == "2026-11-21T02:00:00Z"
    b = put(api, {"enabled": True, "every_days": 7, "time": "08:00", "tz": "Pacific/Kiritimati"}).json()  # UTC+14: it is already 2026-11-19 there
    assert b["start_date"] == "2026-11-19" and b["next_runs"][0]["local"].endswith("+14:00")
    assert put(api, {**BODY, "enabled": False}).json()["next_runs"] == []


def test_direct_cli_put_records_cli_as_the_actor(env):
    r = env.client.put("/api/v1/schedule", json=BODY, headers=HEADERS)
    assert r.status_code == 200 and r.json()["updated_by"] == "cli"


@pytest.mark.parametrize("patch", [
    {"time": "24:00"}, {"time": "7:5"}, {"time": " 02:00"}, {"time": "02:00 "}, {"time": "02:00\n"}, {"time": "02:60"}, {"time": "٠٢:٠٠"}, {"time": ""},
    {"tz": "Mars/Base"}, {"tz": ""}, {"tz": "../etc/passwd"}, {"tz": "/etc/passwd"}, {"tz": "A" * 65}, {"tz": "utc "},
    {"every_days": 2}, {"every_days": 0}, {"every_days": "3 days"}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None},
    {"start_date": "2024-01-01"}, {"start_date": "2028-01-01"}, {"start_date": "tomorrow"}, {"extra": 1}, {"updated_by": "mallory"}, {"source": "database"},
])
def test_put_validation_matrix(api, patch):
    assert put(api, {**BODY, **patch}).status_code == 422
    assert appstate.get_state("schedule") is None  # nothing stored


def test_put_boundaries_and_missing_fields(api):
    assert put(api, {**BODY, "time": "00:00", "start_date": "2025-11-18"}).status_code == 200  # exactly 365 days back
    assert put(api, {**BODY, "time": "23:59", "start_date": "2027-11-18"}).status_code == 200  # exactly 365 days ahead
    assert put(api, {**BODY, "start_date": "2027-11-19"}).status_code == 422
    for k in ("enabled", "every_days", "time", "tz"):
        assert put(api, {a: v for a, v in BODY.items() if a != k}).status_code == 422, k
    assert put(api, [BODY]).status_code == 422


def test_both_routes_need_a_bearer_and_ignore_the_admin_token_when_proxied(env, proxied):  # noqa: F811
    add_user()
    for m in ("GET", "PUT"):
        for h in ({}, HEADERS, bearer("garbage")):
            assert proxied.request(m, "/api/v1/schedule", headers=h, json=BODY if m == "PUT" else None).status_code == 401, (m, h)
    tok = bearer(token_of(login(proxied)))
    assert proxied.get("/api/v1/schedule", headers=tok).status_code == 200
    assert proxied.put("/api/v1/schedule", headers=tok, json=BODY).status_code == 200
    assert appstate.get_state("schedule")["updated_by"] == "alice"


def test_no_leakage_and_a_last_cycle_summary(api):
    with session_scope() as db:
        db.add(SyncCycle(trigger="manual", status="aborted", reason="SENTINEL-reason", summary={"x": "SENTINEL-summary"}, started_at=utc(2026, 11, 17, 2)))
    b = put(api, BODY)
    assert "SENTINEL" not in b.text and "SENTINEL" not in api[0].get("/api/v1/schedule", headers=api[1]).text
    assert set(b.json()["last_cycle"]) == {"id", "status", "started_at"} and b.json()["last_cycle"]["status"] == "aborted"
    with session_scope() as db:
        db.execute(delete(SyncCycle))
