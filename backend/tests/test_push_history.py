"""The sales-order window start is fixed per run (options.history_since): a resume after midnight rebuilds the same plan. Synthetic data, fake clock, no StockTrim."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import update

from app.db import session_scope
from app.models import PushItem, PushRun
from app.sync import engine
from app.sync.engine import plan_fingerprint

from .test_push import HEADERS, finish, go, mk, post
from .test_push_orders import ROWS, Fake13, so_line
from .test_push_stage10 import dry

STEPS = ("sales_orders",)
TODAY = date.today()
WINDOW = 1096  # round(365.25 * 3)
SINCE = (TODAY - timedelta(days=WINDOW)).isoformat()


def edge_row():
    """An order one day inside today's window: in today's plan, aged out two days later."""
    d = (TODAY - timedelta(days=WINDOW - 1)).isoformat()
    return ("SalesOrder", "TE", {"TxnID": "TE", "TxnDate": d, "CustomerRef": {"ListID": "C1"}, "SalesOrderLineRet": so_line("L1", "I1", "2")})


def clock(monkeypatch, days: int) -> None:
    day = TODAY + timedelta(days=days)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(day.year, day.month, day.day, 3, 0, tzinfo=tz)
    monkeypatch.setattr(engine, "datetime", Later)


def plan(env, since=None):
    return engine.build_plan(env.app.state.settings, STEPS, since).plan


def fail_run(run_id: str, options_without: tuple[str, ...] = ()) -> None:
    """Simulate a run that died before sending TE:L1 (and, for a legacy run, one that never stored history_since)."""
    with session_scope() as s:
        opts = {k: v for k, v in s.get(PushRun, run_id).options.items() if k not in options_without}
        s.execute(update(PushRun).where(PushRun.id == run_id).values(status="failed", options=opts))
        s.execute(update(PushItem).where(PushItem.run_id == run_id, PushItem.key == "TE:L1").values(status="pending"))


def loaded(clean_db, tmp_path):
    fake = Fake13()
    env = mk(clean_db, tmp_path, fake, rows=[*ROWS, edge_row()])
    run_id = go(env, STEPS).json()["run_id"]
    out = finish(env, run_id)
    assert out["status"] == "done" and out["counts"]["sales_order_line"] == {"done": 4}
    return env, fake, run_id


def test_pinned_since_gives_the_same_fingerprint_on_any_day(clean_db, tmp_path, monkeypatch):
    env = mk(clean_db, tmp_path, Fake13(), rows=[*ROWS, edge_row()])
    pinned = TODAY - timedelta(days=WINDOW)
    a = plan_fingerprint(plan(env, pinned), list(STEPS))
    clock(monkeypatch, 30)
    assert plan_fingerprint(plan(env, pinned), list(STEPS)) == a and len(plan(env, pinned).sales_orders) == 4
    assert len(plan(env).sales_orders) == 3 and plan_fingerprint(plan(env), list(STEPS)) != a  # unpinned = the new day's window: the edge order aged out


@pytest.mark.parametrize("days", [1, 30])
def test_resume_after_the_clock_moved_matches_and_resends_only_the_pending_line(clean_db, tmp_path, monkeypatch, days):
    env, fake, run_id = loaded(clean_db, tmp_path)
    with session_scope() as s:
        assert s.get(PushRun, run_id).options["history_since"] == SINCE
    fail_run(run_id)
    before = len(fake.bulk)
    clock(monkeypatch, days)
    assert env.client.post(f"/api/v1/push/runs/{run_id}/resume", headers=HEADERS).status_code == 200
    out = finish(env, run_id)
    assert out["status"] == "done" and out["counts"]["sales_order_line"] == {"done": 4}  # the aged-out line was laid out at creation: still part of the run
    assert len(fake.bulk) - before == 1 and fake.bulk[-1]["saleOrderLineItems"][0]["externalReferenceId"] == "TE:L1"


def test_legacy_run_without_a_stored_date_derives_it_from_started_at(clean_db, tmp_path, monkeypatch):
    env, fake, run_id = loaded(clean_db, tmp_path)
    fail_run(run_id, ("history_since",))
    clock(monkeypatch, 2)  # by now the edge order is outside today's window
    assert env.client.post(f"/api/v1/push/runs/{run_id}/resume", headers=HEADERS).status_code == 200
    assert finish(env, run_id)["status"] == "done" and fake.bulk[-1]["saleOrderLineItems"][0]["externalReferenceId"] == "TE:L1"


def test_fresh_run_on_a_new_day_uses_the_new_date(clean_db, tmp_path, monkeypatch):
    env = mk(clean_db, tmp_path, Fake13(), rows=[*ROWS, edge_row()])
    clock(monkeypatch, 5)
    d = dry(env, STEPS)
    assert d["history_since"] == (TODAY + timedelta(days=5 - WINDOW)).isoformat() and d["plan"]["sales_orders"]["lines"] == 3


def test_real_run_with_the_dry_runs_date_matches_after_midnight(clean_db, tmp_path, monkeypatch):
    env = mk(clean_db, tmp_path, Fake13(), rows=[*ROWS, edge_row()])
    d = dry(env, STEPS)
    assert d["history_since"] == SINCE and d["plan"]["sales_orders"]["lines"] == 4
    clock(monkeypatch, 3)
    out = finish(env, post(env, steps=list(STEPS), dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"],
                           history_since=d["history_since"]).json()["run_id"])
    assert out["status"] == "done" and out["counts"]["sales_order_line"] == {"done": 4}


def test_real_run_without_the_date_after_midnight_still_mismatches(clean_db, tmp_path, monkeypatch):
    env = mk(clean_db, tmp_path, Fake13(), rows=[*ROWS, edge_row()])
    d = dry(env, STEPS)
    clock(monkeypatch, 3)
    out = finish(env, post(env, steps=list(STEPS), dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"]).json()["run_id"])
    assert out["status"] == "failed" and out["summary"]["error"] == "plan_fingerprint_mismatch"  # as before: dry-run again


@pytest.mark.parametrize("value", [(TODAY + timedelta(days=1)).isoformat(), (TODAY - timedelta(days=365 * 11 + 5)).isoformat(), "21/11/2026", "nope"])
def test_history_since_is_validated(clean_db, tmp_path, value):
    env = mk(clean_db, tmp_path, Fake13(), rows=ROWS)
    assert post(env, steps=list(STEPS), history_since=value).status_code == 422
    assert post(env, steps=list(STEPS), history_since=TODAY.isoformat(), bogus=1).status_code == 422
