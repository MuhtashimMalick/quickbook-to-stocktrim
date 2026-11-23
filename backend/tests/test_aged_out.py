"""Aged-out vs vanished sales-order lines: synthetic data only, StockTrim is an in-memory MockTransport, the window start is simulated by patching engine.history_start."""
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from app.db import session_scope
from app.models import PushedItem, QBRecord
from app.sync import engine

from .test_push import finish, go, mk
from .test_push_orders import OLD, RECENT, Fake13, so_line
from .test_push_stage10 import ROWS10, dry
from .test_scheduler import H

D0 = date(2024, 1, 1)
N = 150
STEPS = ("suppliers", "products", "bom", "customers", "sales_orders")


def so(txn: str, day: str, *lines: str) -> dict:
    return {"TxnID": txn, "TxnDate": day, "CustomerRef": {"ListID": "C1"}, "IsManuallyClosed": "false", "SalesOrderLineRet": [so_line(i, "I1", "1") for i in lines]}


def rows(orders: list[dict]) -> list:
    return [*ROWS10, *(("SalesOrder", o["TxnID"], o) for o in orders)]


# order A<i> is dated D0+i; P0 has two lines and stays in the window for the whole test
ORDERS = [*(so(f"A{i}", (D0 + timedelta(days=i)).isoformat(), "L1") for i in range(N)), so("P0", (D0 + timedelta(days=300)).isoformat(), "L1", "L2")]


@pytest.fixture
def since(monkeypatch):
    box = [D0]
    monkeypatch.setattr(engine, "history_start", lambda settings, today=None: box[0])
    return box


@pytest.fixture
def env(clean_db, tmp_path, since):
    e = mk(clean_db, tmp_path, Fake13(), rows=rows(ORDERS))
    assert finish(e, go(e, STEPS).json()["run_id"])["status"] == "done"
    return e


def so_stats(env) -> dict:
    return dry(env, ("sales_orders",))["sync"]["sales_orders"]


def test_lines_age_out_over_120_days_and_never_vanish(env, since):
    for day in range(1, 121):
        since[0] = D0 + timedelta(days=day)
        s = so_stats(env)
        assert (s["vanished"], s["aged_out"], s["unchanged"], s["vanished_list_ids"]) == (0, day, N + 2 - day, [])
    out = finish(env, go(env, ("sales_orders",)).json()["run_id"])  # the real run carries both fields too and writes nothing
    assert out["status"] == "done" and out["counts"] == {} and out["summary"]["sync"]["sales_orders"]["aged_out"] == 120 and out["summary"]["sync"]["sales_orders"]["vanished"] == 0


def test_an_order_deleted_in_quickbooks_is_vanished_not_aged_out(env, since):
    since[0] = D0 + timedelta(days=50)  # A0..A49 are outside the window
    with session_scope() as s:
        s.execute(delete(QBRecord).where(QBRecord.qb_id.in_(["A0", "A100"])))  # one aged, one in-window order deleted
    s = so_stats(env)
    assert (s["vanished"], s["aged_out"], s["vanished_list_ids"]) == (2, 49, ["A0", "A100"])


def test_a_line_removed_from_an_order_in_the_window_is_vanished(env):
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == "P0")).one()
        rec.payload = {**rec.payload, "SalesOrderLineRet": rec.payload["SalesOrderLineRet"][:1]}
    s = so_stats(env)
    assert (s["vanished"], s["aged_out"], s["unchanged"], s["vanished_list_ids"]) == (1, 0, N + 1, ["P0"])


# ---- scheduler -------------------------------------------------------------------------------------

def test_a_cycle_with_2000_aged_out_lines_is_not_aborted(clean_db, tmp_path):
    h = H(clean_db, tmp_path, rows=rows([so("S1", RECENT, "L1")]), fake=Fake13())
    h.baseline()
    old = [so(f"OLD{i}", OLD, "L1") for i in range(2000)]
    h.qb.entities["SalesOrder"].extend(old)  # still in QuickBooks, outside the 3-year window; still in the ledger (and in StockTrim)
    with session_scope() as s:
        base = s.scalars(select(PushedItem).where(PushedItem.entity == "sales_order_line")).first()
        s.add_all(PushedItem(target=base.target, entity="sales_order_line", key=f"{o['TxnID']}:L1", ref=o["TxnID"], payload_hash="x", last_run_id=base.last_run_id) for o in old)
    row = h.cycle()
    assert (row.status, row.reason) == ("done", None), row.summary


def test_a_real_mass_disappearance_still_aborts(clean_db, tmp_path):
    orders = [so(f"M{i}", RECENT, "L1") for i in range(1000)]
    h = H(clean_db, tmp_path, rows=rows(orders), fake=Fake13())
    h.baseline()
    h.qb.entities["SalesOrder"] = h.qb.entities["SalesOrder"][100:]  # 10% of the orders are gone (a bad read)
    row = h.cycle()
    assert (row.status, row.reason, row.summary["vanished"]) == ("aborted", "mass_vanish", {"sales_orders": 100}) and row.push_run_id is None
