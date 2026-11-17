"""Offline tests of the sales-order and purchase-order steps (stage 13): synthetic data only, StockTrim is an in-memory httpx MockTransport."""
import json
import logging
import uuid
from collections import Counter
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select, text

from alembic import command
from app.db import session_scope
from app.models import QBRecord
from app.sync import engine
from app.sync.mappers import purchase_orders, sales_order_lines

from .conftest import admin_engine, alembic_cfg, create_database
from .test_push import finish, go, items, mk
from .test_push_stage10 import ALL, ROWS10, Fake10, dry

STEPS = ("sales_orders", "purchase_orders")
ALL13 = (*ALL, *STEPS)
RECENT = (date.today() - timedelta(days=10)).isoformat()
OLD = (date.today() - timedelta(days=365 * 5)).isoformat()
SKUS = {"I1": "P1", "I2": "P2", "I4": "P4", "I5": "P5"}
CUSTOMERS = {"C1", "C2"}


def so_line(i: str, item: str, qty: str, rate: str = "3", invoiced: str = "0") -> dict:
    return {"TxnLineID": i, "ItemRef": {"ListID": item}, "Quantity": qty, "Rate": rate, "Invoiced": invoiced}


SO = [
    {"TxnID": "T1", "TxnDate": RECENT, "CustomerRef": {"ListID": "C1", "FullName": "SENTINEL-c1"}, "IsManuallyClosed": "false", "SalesOrderLineRet": [
        so_line("L1", "I1", "5"), so_line("L2", "X1", "1"), so_line("L3", "I2", "0")],  # L2: shipping (not a product), L3: zero quantity
     "SalesOrderLineGroupRet": {"TxnLineID": "G", "SalesOrderLineRet": [so_line("L4", "I1", "2")]}},
    {"TxnID": "T2", "TxnDate": RECENT, "CustomerRef": {"ListID": "C3"}, "IsManuallyClosed": "true", "SalesOrderLineRet": [  # C3 is not a pushed customer
        so_line("L1", "I1", "10", invoiced="4"), so_line("L2", "I2", "3", invoiced="0")]},
    {"TxnID": "T3", "TxnDate": OLD, "CustomerRef": {"ListID": "C1"}, "SalesOrderLineRet": so_line("L1", "I1", "1")},  # outside the window
    {"TxnID": "T4", "TxnDate": RECENT, "CustomerRef": {"ListID": "C2"}, "SalesOrderLineRet": so_line("L1", "I4", "1.5", "2.345")},  # a single line is a dict, not a list
]


def po_line(i: str, item: str, qty: str, got: str, rate: str = "2") -> dict:
    return {"TxnLineID": i, "ItemRef": {"ListID": item}, "Quantity": qty, "ReceivedQuantity": got, "Rate": rate}


def po(txn: str, vendor: str, lines: list, **kw: Any) -> dict:
    return {"TxnID": txn, "RefNumber": f"R-{txn}", "TxnDate": RECENT, "ExpectedDate": RECENT, "VendorRef": {"ListID": vendor}, "IsManuallyClosed": "false", "IsFullyReceived": "false",
            "PurchaseOrderLineRet": lines, **kw}


PO = [
    po("O1", "V1", [po_line("1", "I1", "10", "4"), po_line("2", "I2", "3", "3"), po_line("3", "X1", "1", "0"), po_line("4", "I1", "2", "0")]),  # open: remaining 6 + 2 summed per product
    po("O2", "V2", [po_line("1", "I4", "5", "5")], IsFullyReceived="true"),
    po("O3", "V1", [po_line("1", "I5", "8", "2")], IsManuallyClosed="true"),
    po("O4", "V8", [po_line("1", "I1", "1", "0")]),  # vendor not pushed (inactive)
    po("O5", "V1", [po_line("1", "I1", "3", "3")]),  # open but everything received
]
ORDER_ROWS = [("Vendor", "V8", {"ListID": "V8", "Name": "SUPINACT", "IsActive": "false"}),
              *(("SalesOrder", o["TxnID"], o) for o in SO), *(("PurchaseOrder", o["TxnID"], o) for o in PO)]
ROWS = [*ROWS10, *ORDER_ROWS]


def test_sales_order_lines_rules() -> None:
    out, st = sales_order_lines(SO, SKUS, CUSTOMERS, date.today() - timedelta(days=365))
    assert st == {"orders": 3, "lines": 3, "non_product": 1, "group": 1, "zero_quantity": 1, "closed_uninvoiced": 1}  # window, skip reasons counted, group lines ignored
    reqs = dict(out)
    assert list(reqs) == ["T1:L1", "T2:L1", "T4:L1"]  # reference = TxnID:TxnLineID
    a, closed, c = reqs["T1:L1"], reqs["T2:L1"], reqs["T4:L1"]
    assert (a.product_id, a.quantity, a.unit_price, a.customer_code, a.external_reference_id) == ("P1", 5, 3, "C1", "T1:L1")
    assert a.order_date.isoformat() == f"{RECENT}T00:00:00+00:00"
    assert closed.quantity == 4 and closed.customer_code is None  # closed order: the invoiced quantity; unpushed customer is not sent
    assert c.quantity == 1.5 and c.unit_price == 2.35 and "location_code" not in c.payload() and "customerName" not in c.payload()
    assert sales_order_lines(SO, SKUS, CUSTOMERS, date.today() - timedelta(days=365 * 10))[1]["orders"] == 4


def test_purchase_order_rules() -> None:
    out, st = purchase_orders(PO, SKUS, {"V1": "SUPA", "V2": "SUPB"})
    assert st == {"sent": 1, "received": 2, "vendor_not_pushed": 1, "no_lines": 1, "lines_non_product": 1}
    reqs = dict(out)
    assert list(reqs) == ["QB-O1", "QB-O2", "QB-O3"]
    o1 = reqs["QB-O1"]
    assert (o1.status, o1.supplier.supplier_code, o1.reference_number, o1.client_reference_number) == ("Sent", "SUPA", "R-O1", "QB-O1")
    assert [(ln.product_id, ln.quantity, ln.unit_price) for ln in o1.purchase_order_line_items] == [("P1", 8, 2)]  # remaining 6 + 2 of the same product; zero-remaining and non-product dropped
    assert o1.purchase_order_line_items[0].eta_date.isoformat() == f"{RECENT}T00:00:00+00:00" and o1.order_date.isoformat() == f"{RECENT}T00:00:00+00:00"
    assert [(r.status, r.purchase_order_line_items[0].quantity) for r in (reqs["QB-O2"], reqs["QB-O3"])] == [("Received", 5), ("Received", 8)]  # history: the full ordered quantity
    assert all("stockOnOrder" not in json.dumps(r.payload()) for r in reqs.values())


class Fake13(Fake10):
    """Fake10 + sales orders (bulk and single, upsert by reference) + purchase orders (upsert by clientReferenceNumber, listed by V2)."""

    def __init__(self, *a: Any, reject_bulk: bool = False, **kw: Any):
        super().__init__(*a, **kw)
        self.so: dict[str, dict] = {}
        self.pos: list = []
        self.po_store: dict[str, dict] = {}
        self.bulk: list[dict] = []
        self.po_posts: Counter = Counter()
        self.reject_bulk = reject_bulk

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, dict(req.url.params)
        if path in ("/api/SalesOrdersBulk", "/api/SalesOrders", "/api/PurchaseOrders") and req.method == "POST":
            body = json.loads(req.content)
            with self.lock:
                self.log.append((req.method, path))
                self.writes.append((req.method, path))
                if path == "/api/SalesOrdersBulk":
                    if self.reject_bulk:
                        return httpx.Response(400, json={"title": "No customer could be found"})
                    self.bulk.append(body)
                    self.so.update({ln["externalReferenceId"]: {**ln, "customerCode": body.get("customerCode")} for ln in body["saleOrderLineItems"]})
                elif path == "/api/SalesOrders":
                    self.so[body["externalReferenceId"]] = body
                else:
                    self.po_posts[body["clientReferenceNumber"]] += 1
                    self.po_store[body["clientReferenceNumber"]] = body
            return httpx.Response(200, json={"id": 1, "productId": "x"})
        if path == "/api/V2/PurchaseOrders":
            self.log.append((req.method, path))
            rows = [{"clientReferenceNumber": k, "status": v["status"]} for k, v in self.po_store.items()] + [{"status": "Draft"} for _ in self.pos]
            return httpx.Response(200, json=[r for r in rows if q.get("status") in (None, r["status"])] if int(q.get("page") or 1) in (0, 1) else [])
        return super().__call__(req)


def loaded(clean_db, tmp_path, **kw: Any):
    fake = Fake13(**kw)
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    out = finish(env, go(env, ALL13).json()["run_id"])
    assert out["status"] == "done" and out["summary"]["mode"] == "initial", out
    return env, fake, out


def sync(env, steps=STEPS) -> dict:
    return finish(env, go(env, steps).json()["run_id"])


def edit_order(entity: str, txn: str, **fields: Any) -> None:
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.entity == entity, QBRecord.qb_id == txn)).one()
        rec.payload = {**rec.payload, **fields}


def test_dry_run_counts_and_no_writes(clean_db, tmp_path):
    fake = Fake13()
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    d = dry(env, STEPS)
    assert d["plan"]["sales_orders"] == {"orders": 3, "lines": 3, "non_product": 1, "group": 1, "zero_quantity": 1, "closed_uninvoiced": 1}
    assert d["plan"]["purchase_orders"] == {"sent": 1, "received": 2, "vendor_not_pushed": 1, "no_lines": 1, "lines_non_product": 1}
    assert fake.writes == [] and "sales_orders" not in dry(env, ("suppliers",))["plan"]


def test_full_load_uses_bulk_per_customer_and_one_post_per_po(clean_db, tmp_path):
    env, fake, out = loaded(clean_db, tmp_path)
    assert out["counts"]["sales_order_line"] == {"done": 3} and out["counts"]["purchase_order"] == {"done": 3}
    assert sorted(((b.get("customerCode"), len(b["saleOrderLineItems"])) for b in fake.bulk), key=str) == [("C1", 1), ("C2", 1), (None, 1)]  # one customer per call
    assert all("customerCode" not in ln for b in fake.bulk for ln in b["saleOrderLineItems"])
    assert set(fake.so) == {"T1:L1", "T2:L1", "T4:L1"} and set(fake.po_posts.values()) == {1}
    assert {s["status"] for s in fake.po_store.values()} == {"Sent", "Received"}
    assert {(i.entity, i.key, i.ref) for i in items() if i.entity in ("sales_order_line", "purchase_order")} == {
        ("sales_order_line", "T1:L1", "T1"), ("sales_order_line", "T2:L1", "T2"), ("sales_order_line", "T4:L1", "T4"),
        ("purchase_order", "QB-O1", "O1"), ("purchase_order", "QB-O2", "O2"), ("purchase_order", "QB-O3", "O3")}  # refs are TxnIDs
    kinds = [p for m, p in fake.log if m in ("POST", "PUT") and p in ("/api/Customers", "/api/SalesOrdersBulk", "/api/PurchaseOrders")]
    assert kinds.index("/api/Customers") < kinds.index("/api/SalesOrdersBulk") < kinds.index("/api/PurchaseOrders")  # push order


def test_bulk_chunks_are_capped(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    extra = [("SalesOrder", "TX", {"TxnID": "TX", "TxnDate": RECENT, "CustomerRef": {"ListID": "C1"}, "SalesOrderLineRet": [so_line(f"M{i}", "I1", "1") for i in range(5)]})]
    fake = Fake13()
    env = mk(clean_db, tmp_path, fake, rows=[*ROWS, *extra])
    d = dry(env, ("sales_orders",))
    out = finish(env, go(env, ("sales_orders",)).json()["run_id"])
    assert out["status"] == "done" and len(fake.so) == 8
    assert sorted(len(b["saleOrderLineItems"]) for b in fake.bulk if b.get("customerCode") == "C1") == [2, 2, 2] and d["plan"]["sales_orders"]["lines"] == 8


def test_a_rejected_bulk_call_falls_back_to_one_post_per_line(clean_db, tmp_path):
    fake = Fake13(reject_bulk=True)
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    out = finish(env, go(env, ("sales_orders",)).json()["run_id"])
    assert out["status"] == "done" and out["counts"] == {"sales_order_line": {"done": 3}} and fake.bulk == [] and set(fake.so) == {"T1:L1", "T2:L1", "T4:L1"}


def test_unchanged_sync_makes_no_calls_and_a_changed_line_or_po_state_is_resent(clean_db, tmp_path):
    env, fake, _ = loaded(clean_db, tmp_path)
    before = len(fake.writes)
    d = dry(env, STEPS)
    assert d["mode"] == "sync" and d["planned_calls"] == 0 and d["sync"]["sales_orders"]["unchanged"] == 3 and d["sync"]["purchase_orders"]["unchanged"] == 3
    assert sync(env)["counts"] == {} and len(fake.writes) == before
    edit_order("SalesOrder", "T1", SalesOrderLineRet=[so_line("L1", "I1", "7"), so_line("L2", "X1", "1"), so_line("L3", "I2", "0")])  # quantity 5 -> 7
    edit_order("PurchaseOrder", "O1", IsFullyReceived="true")  # open -> received
    out = sync(env)
    assert out["counts"] == {"sales_order_line": {"done": 1}, "purchase_order": {"done": 1}}
    assert fake.so["T1:L1"]["quantity"] == 7 and len(fake.so) == 3
    assert fake.po_store["QB-O1"]["status"] == "Received" and fake.po_store["QB-O1"]["purchaseOrderLineItems"][0]["quantity"] == 12 and fake.po_posts["QB-O1"] == 2
    assert fake.po_posts["QB-O2"] == 1 and sync(env)["counts"] == {}


def test_pos_we_sent_do_not_block_sync_mode_but_foreign_ones_warn(clean_db, tmp_path):
    env, fake, _ = loaded(clean_db, tmp_path)
    v = dry(env, STEPS)["verdict"]
    assert v["ok_to_push"] and not [w for w in v["warnings"] if w["code"] == "purchase_orders_present"]
    fake.pos = [object()]  # a PO we did not send
    v = dry(env, STEPS)["verdict"]
    assert v["ok_to_push"] and [w["count"] for w in v["warnings"] if w["code"] == "purchase_orders_present"] == [1]


def test_order_steps_leak_nothing(clean_db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    env, fake, out = loaded(clean_db, tmp_path, reject_bulk=True)
    d = dry(env, STEPS)
    everything = json.dumps([d, out, [(i.key, i.ref, i.status, i.error_code, i.detail) for i in items()]]) + caplog.text
    assert "SENTINEL" not in everything


def test_migration_0006_widens_the_entity_checks_and_downgrades(db_url):
    name = f"qb_scratch_{uuid.uuid4().hex[:8]}"
    create_database(db_url, name)
    url = create_engine(db_url).url.set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    rid = uuid.uuid4()
    try:
        command.upgrade(alembic_cfg(url), "0005")
        with pytest.raises(Exception), eng.begin() as c:
            c.execute(text("INSERT INTO pushed_item (target, entity, key, pushed_at) VALUES ('T', 'purchase_order', 'k', now())"))
        command.upgrade(alembic_cfg(url), "head")
        with eng.begin() as c:
            c.execute(text("INSERT INTO push_run (id, target, started_at, status, options) VALUES (:i, 'T', now(), 'done', '{}')"), {"i": rid})
            for ent in ("sales_order_line", "purchase_order", "supplier"):
                c.execute(text("INSERT INTO push_item (run_id, entity, key, status, attempts, updated_at) VALUES (:r, :e, 'k', 'done', 1, now())"), {"r": rid, "e": ent})
                c.execute(text("INSERT INTO pushed_item (target, entity, key, pushed_at) VALUES ('T', :e, 'k', now())"), {"e": ent})
            with pytest.raises(Exception), c.begin_nested():
                c.execute(text("INSERT INTO pushed_item (target, entity, key, pushed_at) VALUES ('T', 'bom_edge', 'k', now())"))
        command.downgrade(alembic_cfg(url), "0005")
        with eng.connect() as c:  # the new entities are gone, the old ones stay, and the old CHECK is back
            assert [r[0] for r in c.execute(text("SELECT entity FROM pushed_item"))] == ["supplier"] and [r[0] for r in c.execute(text("SELECT entity FROM push_item"))] == ["supplier"]
        with pytest.raises(Exception), eng.begin() as c:
            c.execute(text("INSERT INTO pushed_item (target, entity, key, pushed_at) VALUES ('T', 'purchase_order', 'k2', now())"))
        command.upgrade(alembic_cfg(url), "head")
        with eng.connect() as c:  # grants: the app role keeps DML through the default privileges
            assert c.execute(text("SELECT count(*) FROM information_schema.table_privileges WHERE table_name = 'pushed_item' AND privilege_type = 'INSERT'")).scalar() >= 1
    finally:
        eng.dispose()
        with admin_engine(db_url).connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
