"""Offline tests of the push engine (stage 9): synthetic data only, StockTrim is an in-memory httpx MockTransport, nothing really sleeps."""
import asyncio
import json
import logging
import threading
import uuid
from collections import Counter
from typing import Any, Callable, Optional

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import main
from app.api.v1.preflight import get_stocktrim_client_kw
from app.config import Settings
from app.db import session_scope
from app.models import PushItem, PushRun, QBRecord
from app.preflight.report import preflight_client
from app.stocktrim.client import StockTrimClient
from app.stocktrim.limiter import RateLimiter
from app.sync import engine

from .conftest import HEADERS, PASSWORD, TOKEN, USER, Env

ROWS = [
    ("Vendor", "V1", {"ListID": "V1", "Name": "SUPA", "CompanyName": "SENTINEL-co-a"}),
    ("Vendor", "V2", {"ListID": "V2", "Name": "SUPB", "CompanyName": "SENTINEL-co-b"}),
    ("Vendor", "V3", {"ListID": "V3", "Name": "BADSUP", "CompanyName": "SENTINEL-co-bad"}),
    ("ItemInventory", "I1", {"ListID": "I1", "Name": "P1", "SalesDesc": "SENTINEL-d1", "QuantityOnHand": "5", "PurchaseCost": "1.5", "SalesPrice": "3", "PrefVendorRef": {"ListID": "V1"}}),
    ("ItemInventory", "I2", {"ListID": "I2", "Name": "P2", "SalesDesc": "SENTINEL-d2", "QuantityOnHand": "-2", "PrefVendorRef": {"ListID": "V2"}}),
    ("ItemInventory", "I3", {"ListID": "I3", "Name": "P3", "SalesDesc": "SENTINEL-d3", "QuantityOnHand": "-4", "PrefVendorRef": {"ListID": "V3"}}),
    ("ItemInventory", "I4", {"ListID": "I4", "Name": "P4", "SalesDesc": "SENTINEL-d4", "QuantityOnHand": "1"}),
    ("ItemInventory", "I5", {"ListID": "I5", "Name": "P5", "SalesDesc": "SENTINEL-d5", "QuantityOnHand": "7", "ReorderPoint": "2", "PrefVendorRef": {"ListID": "V1"}}),
    ("ItemInventory", "I6", {"ListID": "I6", "Name": "P6", "SalesDesc": "SENTINEL-d6", "QuantityOnHand": "-1"}),
]
PRODUCT_KEYS = {"P1", "P2", "P3", "P4", "P5", "P6"}
SUPPLIER_KEYS = {"SUPA", "SUPB", "BADSUP"}


class Fake:
    """In-memory StockTrim. Records everything; knobs: bad_suppliers (400), reject_negative (400 StockTrimOnHand), clamp_negative (silent),
    alter (readback differences), on_product(n, body) -> Response|None (n = running count of product POSTs), on_supplier(codes) (called before
    the supplier POST is judged), existing (non-empty account)."""

    def __init__(self, existing: int = 0, bad_suppliers=(), reject_negative=False, clamp_negative=False, alter: Optional[dict] = None,
                 on_product: Optional[Callable[[int, dict], Optional[httpx.Response]]] = None, on_supplier: Optional[Callable[[list[str]], None]] = None):
        self.lock, self.products, self.suppliers, self.log = threading.Lock(), {}, {}, []
        self.existing, self.bad, self.reject_neg, self.clamp_neg, self.alter, self.on_product = existing, set(bad_suppliers), reject_negative, clamp_negative, alter or {}, on_product
        self.posts, self.n, self.writes, self.order, self.on_supplier = Counter(), 0, [], [], on_supplier

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q, path = dict(req.url.params), req.url.path
        with self.lock:
            self.log.append((req.method, path))
            if req.method != "GET":
                self.writes.append((req.method, path))
        if req.method == "GET":
            if path == "/api/Products" and "code" in q:
                got = self.products.get(q["code"])
                return httpx.Response(200, json=[{**got, **self.alter.get(q["code"], {})}]) if got else httpx.Response(404, json={"title": "Not Found"})
            if path == "/api/Products":
                page = int(q.get("pageNo") or 1)
                return httpx.Response(200, json=[{"productId": f"X{i}", "productCodeReadable": f"X{i}"} for i in range(self.existing)] if page == 1 else [])
            return httpx.Response(200, json=[])  # customers, purchase orders, suppliers bulk
        body = json.loads(req.content)
        if path == "/api/Suppliers":
            codes = [b["supplierCode"] for b in body]
            if self.on_supplier:
                self.on_supplier(codes)
            with self.lock:
                if self.bad & set(codes):
                    return httpx.Response(400, json={"errors": {"SupplierCode": ["SENTINEL-bad-supplier-msg"]}})
                self.order += [("S", c) for c in codes]
                self.suppliers.update({c["supplierCode"]: c for c in body})
            return httpx.Response(200, json=[{"supplierCode": c} for c in codes])
        if path == "/api/Products":
            with self.lock:
                self.n += 1
                n = self.n
            if self.on_product and (resp := self.on_product(n, body)):
                return resp
            stock = body["stockOnHand"]
            if stock < 0 and self.reject_neg:
                return httpx.Response(400, json={"errors": {"StockTrimOnHand": ["SENTINEL-neg-msg"]}})
            with self.lock:
                self.posts[body["productId"]] += 1
                self.order.append(("P", body["productId"]))
                self.products[body["productCodeReadable"]] = {**body, "stockOnHand": max(stock, 0) if self.clamp_neg else stock}
            return httpx.Response(200, json=self.products[body["productCodeReadable"]])
        return httpx.Response(404)


def mk(clean_db, tmp_path, fake: Fake, writes: bool = True, rows=ROWS) -> Env:
    eng = create_engine(clean_db)
    with Session(eng) as s:
        s.add_all(QBRecord(entity=e, qb_id=i, payload=p) for e, i, p in rows)
        s.commit()
    eng.dispose()
    env = Env(Settings(_env_file=None, qbwc_username=USER, qbwc_password=PASSWORD, admin_token=TOKEN, qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), database_url=clean_db,
                       fixtures_dir=tmp_path / "fx", log_dir=tmp_path / "logs", stocktrim_max_rps=50, stocktrim_auth_id="id-1", stocktrim_auth_signature="sig-1",
                       stocktrim_writes_enabled=writes))
    kw = {"transport": httpx.MockTransport(fake), "max_attempts": 1, "sleep": lambda s: None, "limiter": RateLimiter(10000, sleep=lambda s: None)}
    env.app.dependency_overrides[get_stocktrim_client_kw] = lambda: kw
    return env


@pytest.fixture(autouse=True)
def _no_stray_run():
    yield
    engine.join(30)
    assert not engine._ACTIVE.locked(), "a test left the push lock held"


def get_stocktrim_client_kw_override(env: Env) -> dict:
    return env.app.dependency_overrides[get_stocktrim_client_kw]()


def post(env: Env, **body: Any) -> httpx.Response:
    return env.client.post("/api/v1/push", json=body, headers=HEADERS)


def dry(env: Env, steps=("suppliers", "products")) -> dict:
    r = post(env, steps=list(steps))
    assert r.status_code == 200, r.text
    return r.json()


def go(env: Env, steps=("suppliers", "products"), expect: int = 200) -> httpx.Response:
    d = dry(env, steps)
    r = post(env, steps=list(steps), dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
    assert r.status_code == expect, r.text
    return r


def finish(env: Env, run_id: str) -> dict:
    engine.join(30)
    r = env.get(f"/api/v1/push/runs/{run_id}")
    assert r.status_code == 200, r.text
    return r.json()


def items(entity: Optional[str] = None) -> list[PushItem]:
    with session_scope() as s:
        rows = list(s.scalars(select(PushItem).order_by(PushItem.id)))
        s.expunge_all()
    return [r for r in rows if entity in (None, r.entity)]


def test_happy_path_dry_run_then_real(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    d = dry(env)
    assert d["dry_run"] and d["plan"] == {"suppliers": 3, "products": 6, "bom_edges": 0, "customers": 0, "negative_stock": 3, "stock_adjusted": 0, "dropped_supplier_links": 0}
    assert d["verdict"]["ok_to_push"] and d["plan_fingerprint"] and d["preflight_fingerprint"]
    assert fake.writes == [] and items() == []  # the dry-run wrote nothing anywhere and no push_item rows
    with session_scope() as s:
        row = s.scalars(select(PushRun)).one()
        assert (row.status, row.options["dry_run"]) == ("done", True)
    run_id = go(env).json()["run_id"]
    out = finish(env, run_id)
    assert out["status"] == "done" and out["counts"] == {"supplier": {"done": 3}, "product": {"done": 6}} and out["error_codes"] == {}
    assert {k for e, k in fake.order if e == "S"} == SUPPLIER_KEYS and {k for e, k in fake.order if e == "P"} == PRODUCT_KEYS
    assert [e for e, _ in fake.order] == sorted(e for e, _ in fake.order)[::-1]  # every supplier before every product ("S" > "P")
    assert set(fake.posts.values()) == {1}
    assert {(i.entity, i.key) for i in items()} == {("supplier", k) for k in SUPPLIER_KEYS} | {("product", k) for k in PRODUCT_KEYS}  # keys only
    assert out["summary"]["negative_stock_stored_as_is"] == 3 and out["summary"]["negative_stock_silently_altered"] == 0
    assert out["summary"]["readback"]["mismatch_fields"] == {}


def test_dry_run_guard_refuses_writes(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    dry(env)
    assert all(m == "GET" for m, _ in fake.log) and fake.log
    client = preflight_client(env.app.state.settings, get_stocktrim_client_kw_override(env))  # the client the dry-run uses
    with pytest.raises(RuntimeError, match="read-only"):
        client.request("POST", "/api/Products", json={"productId": "X"})
    assert fake.writes == []  # refused before it left the machine


def test_gates(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake, writes=False)
    fp = dry(env)["preflight_fingerprint"]
    assert post(env, steps=["suppliers"], dry_run=False, preflight_fingerprint=fp, plan_fingerprint="p").status_code == 403  # writes disabled
    assert post(env, steps=["customers"]).status_code == 422 and post(env, steps=["products"], bogus=1).status_code == 422
    env.app.state.settings.stocktrim_writes_enabled = True
    assert post(env, steps=["suppliers"], dry_run=False, plan_fingerprint="p").status_code == 422  # pre-flight fingerprint required
    assert post(env, steps=["suppliers"], dry_run=False, preflight_fingerprint=fp).status_code == 422  # plan fingerprint required for a real run
    assert post(env, steps=["suppliers"], dry_run=False, preflight_fingerprint="0" * 64, plan_fingerprint="p").status_code == 409  # mismatch
    assert fake.writes == [] and [i for i in items()] == []
    assert not engine._ACTIVE.locked()


def test_non_empty_account_refused(clean_db, tmp_path):
    fake = Fake(existing=2)
    env = mk(clean_db, tmp_path, fake)
    d = dry(env)
    assert not d["verdict"]["ok_to_push"]
    assert post(env, steps=["suppliers"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"]).json()["detail"] == "preflight_not_ok"
    assert fake.writes == []


def test_one_active_run(clean_db, tmp_path):
    release = threading.Event()
    fake = Fake(on_product=lambda n, b: release.wait(10) and None)
    env = mk(clean_db, tmp_path, fake)
    d = dry(env)
    run_id = post(env, steps=["suppliers", "products"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"]).json()["run_id"]
    try:
        r = post(env, steps=["suppliers"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
        assert (r.status_code, r.json()["detail"]) == (409, "push_running")
    finally:
        release.set()
    assert finish(env, run_id)["status"] == "done"
    # the database refuses a second running row on its own (partial unique index)
    with session_scope() as s:
        s.add(PushRun(target="t", status="running"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(PushRun(target="t", status="running"))


def test_cancel_then_resume_sends_each_key_once(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake(on_product=lambda n, b: engine.cancel(next(iter(engine._CANCELS))) if n == 3 else None)
    env = mk(clean_db, tmp_path, fake)
    run_id = go(env).json()["run_id"]
    first = finish(env, run_id)
    assert first["status"] == "cancelled" and 0 < first["counts"]["product"]["done"] < 6 and first["counts"]["product"].get("pending", 0) > 0
    sent_before = dict(fake.posts)
    r = env.client.post(f"/api/v1/push/runs/{run_id}/resume", headers=HEADERS)
    assert r.status_code == 200 and r.json()["run_id"] == run_id
    out = finish(env, run_id)
    assert out["status"] == "done" and out["counts"] == {"supplier": {"done": 3}, "product": {"done": 6}}
    assert set(fake.posts) == PRODUCT_KEYS and set(fake.posts.values()) == {1}  # nothing done before the cancel was sent again
    assert all(fake.posts[k] == 1 for k in sent_before)
    assert len([m for m, p in fake.log if m == "POST" and p == "/api/Suppliers"]) == 2  # suppliers are not re-sent either (chunks of 2: 3 suppliers)


def test_resume_refusals(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake(on_product=lambda n, b: engine.cancel(next(iter(engine._CANCELS))) if n == 1 else None)
    env = mk(clean_db, tmp_path, fake)
    run_id = go(env).json()["run_id"]
    assert finish(env, run_id)["status"] == "cancelled"
    url = f"/api/v1/push/runs/{run_id}/resume"
    env.app.state.settings.stocktrim_writes_enabled = False
    assert env.client.post(url, headers=HEADERS).status_code == 403
    env.app.state.settings.stocktrim_writes_enabled = True
    with session_scope() as s:  # plan changed since the run (a QuickBooks record changed)
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == "I1")).one()
        rec.payload = {**rec.payload, "SalesPrice": "99"}
    r = env.client.post(url, headers=HEADERS)
    assert (r.status_code, r.json()["detail"]) == (409, "plan_fingerprint_mismatch") and not engine._ACTIVE.locked()
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == "I1")).one()
        rec.payload = {**rec.payload, "SalesPrice": "3"}
        s.execute(update(PushRun).where(PushRun.id == uuid.UUID(run_id)).values(target="other|account"))
    r = env.client.post(url, headers=HEADERS)
    assert (r.status_code, r.json()["detail"]) == (409, "target_mismatch")
    assert env.client.post(f"/api/v1/push/runs/{uuid.uuid4()}/resume", headers=HEADERS).status_code == 404
    assert env.client.post(f"/api/v1/push/runs/{uuid.uuid4()}/cancel", headers=HEADERS).status_code == 404
    assert env.client.post(f"/api/v1/push/runs/{run_id}/cancel", headers=HEADERS).status_code == 409  # not active
    with session_scope() as s:
        s.execute(update(PushRun).where(PushRun.id == uuid.UUID(run_id)).values(status="done"))
    assert env.client.post(url, headers=HEADERS).json()["detail"] == "run_not_resumable"


@pytest.mark.parametrize("status,code", [(401, "auth"), (500, "unavailable")])
def test_fatal_error_aborts_run(clean_db, tmp_path, monkeypatch, status, code):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake(on_product=lambda n, b: httpx.Response(status, json={"title": "SENTINEL-x"}) if n >= 3 else None)
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    prod = {i.key: i for i in items("product")}
    assert out["status"] == "failed" and out["summary"]["fatal"] == code
    assert not [i for i in prod.values() if i.status == "pending"]  # nothing is left pending: unattempted items are skipped/aborted
    assert any(i.status == "failed" and i.error_code == code for i in prod.values())
    assert all(i.error_code == "aborted" for i in prod.values() if i.status == "skipped") and Counter(i.status for i in prod.values())["done"] < 6
    first_fail = fake.n
    fake.on_product = None  # resume retries failed + aborted items only
    assert env.client.post(f"/api/v1/push/runs/{out['run_id']}/resume", headers=HEADERS).status_code == 200
    assert finish(env, out["run_id"])["counts"]["product"] == {"done": 6} and first_fail >= 3 and set(fake.posts.values()) == {1}


def test_429_defers_the_shared_limiter_for_all_workers():
    now, slept = [100.0], []
    lim = RateLimiter(5, clock=lambda: now[0], sleep=slept.append)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}) if len(calls) == 1 else httpx.Response(200, json=[])

    c = StockTrimClient("https://x.example", "id", "sig", transport=httpx.MockTransport(handler), limiter=lim, sleep=lambda s: None)
    c.request("GET", "/api/Customers")
    assert lim._next >= 107.0  # the one 429 pushed the next slot of EVERY caller of this limiter
    lim.acquire()
    assert slept and slept[-1] >= 6.9  # a different worker now waits the Retry-After too


def test_chunk_400_falls_back_per_supplier_and_drops_links(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake(bad_suppliers={"BADSUP"})
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    sup = {i.key: i for i in items("supplier")}
    assert [k for k, i in sup.items() if i.status == "failed"] == ["BADSUP"] and sup["BADSUP"].error_code == "rejected_400"  # only the bad one
    assert sup["SUPA"].status == sup["SUPB"].status == "done"
    assert out["status"] == "done_with_failures" and out["counts"]["supplier"] == {"done": 2, "failed": 1} and out["summary"]["items_failed"] == 1
    p3, p1 = fake.products["P3"], fake.products["P1"]
    assert "supplierCode" not in p3 and "suppliers" not in p3  # never a link to a supplier that is not done
    assert p1["supplierCode"] == "SUPA"
    assert {i.key: i.error_code for i in items("product") if i.error_code} == {"P3": "supplier_link_dropped"}
    assert "BADSUP" not in fake.suppliers


def test_negative_stock_stored_as_is(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    assert [fake.products[k]["stockOnHand"] for k in ("P2", "P3", "P6")] == [-2, -4, -1]
    assert out["summary"]["negative_stock_rejected"]["count"] == 0
    assert out["summary"]["negative_stock_stored_as_is"] == 3 and out["summary"]["negative_stock_silently_altered"] == 0


def test_negative_stock_rejected_then_clamped(clean_db, tmp_path):
    fake = Fake(reject_negative=True)
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    assert out["status"] == "done" and [fake.products[k]["stockOnHand"] for k in ("P2", "P3", "P6")] == [0, 0, 0]
    rej = out["summary"]["negative_stock_rejected"]
    assert rej["count"] >= 1 and rej["clamp_switched"] and set(rej["list_ids"]) <= {"I2", "I3", "I6"}  # ListIDs, not names
    first = next(i for i in items("product") if i.error_code == "negative_stock_rejected")
    assert first.detail["negative_stock_rejected"] == {"status": 400, "fields": ["StockTrimOnHand"]}  # status + field names only
    assert "SENTINEL" not in json.dumps([i.detail for i in items()])
    assert out["counts"]["product"] == {"done": 6} and out["summary"]["negative_stock_silently_altered"] == 0


def test_negative_stock_silently_clamped_is_detected(clean_db, tmp_path):
    fake = Fake(clamp_negative=True)
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    assert out["summary"]["negative_stock_silently_altered"] == 3 and out["summary"]["negative_stock_stored_as_is"] == 0
    assert out["summary"]["readback"]["mismatch_fields"] == {"stock_on_hand": 3}


def test_readback_counts_mismatches_per_field(clean_db, tmp_path):
    fake = Fake(alter={"P1": {"name": "changed", "cost": 9}, "P5": {"name": "other", "minimumShelfLevel": 99, "supplierCode": "ZZ", "suppliers": []}})
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    assert out["summary"]["readback"]["mismatch_fields"] == {"name": 2, "cost": 1, "minimum_shelf_level": 1, "supplier": 1}
    assert out["summary"]["readback"]["sample_checked"] == 3 and out["summary"]["readback"]["negative_checked"] == 3  # P1, P4, P5 ordinary (<25: all)


def test_readback_compares_exactly(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake(alter={"P1": {"cost": 1.504, "price": 3.004}, "P5": {"stockOnHand": 7.000001}}))  # no tolerance any more
    assert finish(env, go(env).json()["run_id"])["summary"]["readback"]["mismatch_fields"] == {"cost": 1, "price": 1, "stock_on_hand": 1}


def test_startup_recovery_marks_running_interrupted(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake())
    with session_scope() as s:
        s.add(PushRun(target="t", status="running"))
    assert engine.recover_push_runs() == 1 and engine.recover_push_runs() == 0
    with session_scope() as s:
        assert s.scalars(select(PushRun.status)).one() == "interrupted"
    assert env  # app built


def test_progress_and_logs_leak_nothing_and_codes_are_a_closed_set(clean_db, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    caplog.set_level(logging.DEBUG)
    fake = Fake(bad_suppliers={"BADSUP"}, reject_negative=True, alter={"P1": {"name": "SENTINEL-stored-other"}})
    env = mk(clean_db, tmp_path, fake)
    d = dry(env)
    run_id = post(env, steps=["suppliers", "products"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"]).json()["run_id"]
    progress = env.get(f"/api/v1/push/runs/{run_id}")
    engine.join(30)
    out = env.get(f"/api/v1/push/runs/{run_id}").json()
    everything = json.dumps([out, progress.json(), d, [(i.key, i.status, i.error_code, i.detail) for i in items()]]) + caplog.text
    assert "SENTINEL" not in everything and "co-a" not in everything
    assert {s["ref"] for s in out["samples"]} <= {"V1", "V2", "V3", "I1", "I2", "I3", "I4", "I5", "I6"} and len(out["samples"]) <= engine.SAMPLE_LIMIT
    assert {i.error_code for i in items() if i.error_code} <= engine.ERROR_CODES and {i.error_code for i in items() if i.error_code}
    assert set(out) == {"run_id", "status", "dry_run", "steps", "started_at", "updated_at", "finished_at", "counts", "error_codes", "eta_seconds", "summary", "samples"}


def test_endpoints_require_the_admin_token(env):
    rid = uuid.uuid4()
    for method, path in (("post", "/api/v1/push"), ("get", f"/api/v1/push/runs/{rid}"), ("post", f"/api/v1/push/runs/{rid}/cancel"), ("post", f"/api/v1/push/runs/{rid}/resume")):
        assert getattr(env.client, method)(path).status_code == 401
        assert getattr(env.client, method)(path, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_item_counts_group_by_entity_and_status(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake())
    out = finish(env, go(env).json()["run_id"])
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(PushItem)) == sum(sum(v.values()) for v in out["counts"].values()) == 9
    assert out["eta_seconds"] is None  # finished


# ---------------------------------------------------------------- review fixes
def resume(env: Env, run_id: str, expect: int = 200) -> httpx.Response:
    r = env.client.post(f"/api/v1/push/runs/{run_id}/resume", headers=HEADERS)
    assert r.status_code == expect, r.text
    return r


def test_thread_crash_fails_the_run_and_releases_the_lock(clean_db, tmp_path, monkeypatch):
    env = mk(clean_db, tmp_path, Fake())
    d = dry(env)
    monkeypatch.setattr(engine, "build_plan", lambda settings: (_ for _ in ()).throw(KeyError("SENTINEL-boom")))
    out = finish(env, post(env, steps=["suppliers", "products"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"]).json()["run_id"])
    assert out["status"] == "failed" and out["summary"]["error"] == "KeyError" and "SENTINEL" not in json.dumps(out)
    assert not engine._ACTIVE.locked() and items() == []
    resume(env, out["run_id"], 409)  # no items were written: not resumable (it would "succeed" doing nothing)


def test_db_failure_after_a_200_resends_exactly_that_item(clean_db, tmp_path, monkeypatch):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    orig, boom = engine._mark, []

    def flaky(run_id, entity, keys, status, *a):
        if entity == "product" and status == "done" and keys == ["P1"] and not boom:
            boom.append(1)
            raise RuntimeError("db went away")
        return orig(run_id, entity, keys, status, *a)

    monkeypatch.setattr(engine, "_mark", flaky)
    out = finish(env, go(env).json()["run_id"])
    assert out["status"] == "done_with_failures" and fake.posts["P1"] == 1  # the POST happened, the DB write did not
    assert {i.key: i.error_code for i in items("product") if i.status == "failed"} == {"P1": "internal"}
    before = dict(fake.posts)
    resume(env, out["run_id"])
    assert finish(env, out["run_id"])["status"] == "done"
    assert fake.posts["P1"] == 2 and all(fake.posts[k] == before[k] for k in PRODUCT_KEYS - {"P1"})


def test_double_resume_launches_once(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    release = threading.Event()
    fake = Fake(on_product=lambda n, b: engine.cancel(next(iter(engine._CANCELS))) if n == 1 else None)
    env = mk(clean_db, tmp_path, fake)
    run_id = go(env).json()["run_id"]
    assert finish(env, run_id)["status"] == "cancelled"
    fake.on_product = lambda n, b: release.wait(10) and None
    resume(env, run_id)
    try:
        r = resume(env, run_id, 409)  # the run is `running` again
        assert r.json()["detail"] == "run_not_resumable"
    finally:
        release.set()
    assert finish(env, run_id)["status"] == "done" and set(fake.posts.values()) == {1}


def test_suppliers_only_run_cancelled_during_the_fallback_ends_cancelled(clean_db, tmp_path):
    fake = Fake(bad_suppliers={"BADSUP"}, on_supplier=lambda codes: engine.cancel(next(iter(engine._CANCELS))) if len(codes) == 3 else None)
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env, ("suppliers",)).json()["run_id"])
    assert out["status"] == "cancelled" and out["counts"] == {"supplier": {"pending": 3}}


def test_done_with_failures_resume_resends_only_failed_keys_and_relinks(clean_db, tmp_path):
    fake = Fake(bad_suppliers={"BADSUP"})
    env = mk(clean_db, tmp_path, fake)
    out = finish(env, go(env).json()["run_id"])
    assert out["status"] == "done_with_failures" and out["summary"]["items_failed"] == 1
    assert "supplierCode" not in fake.products["P3"]
    supplier_posts, product_posts = len([1 for m, p in fake.log if m == "POST" and p == "/api/Suppliers"]), dict(fake.posts)
    fake.bad.clear()  # the supplier is fine now
    resume(env, out["run_id"])
    out = finish(env, out["run_id"])
    assert out["status"] == "done" and out["summary"]["items_failed"] == 0 and out["counts"] == {"supplier": {"done": 3}, "product": {"done": 6}}
    assert len([1 for m, p in fake.log if m == "POST" and p == "/api/Suppliers"]) == supplier_posts + 1  # only BADSUP was re-sent
    assert fake.suppliers["BADSUP"] and fake.products["P3"]["supplierCode"] == "BADSUP"  # P3 was re-pushed WITH its link
    assert fake.posts["P3"] == product_posts["P3"] + 1 and all(fake.posts[k] == 1 for k in PRODUCT_KEYS - {"P3"})
    assert not [i for i in items("product") if i.error_code]  # the warning is gone with the re-push
    assert out["summary"]["duration_seconds"] >= 0


def test_products_only_run_after_a_suppliers_run_keeps_the_links(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    assert finish(env, go(env, ("suppliers",)).json()["run_id"])["status"] == "done"
    out = finish(env, go(env, ("products",)).json()["run_id"])
    assert out["status"] == "done" and fake.products["P1"]["supplierCode"] == "SUPA" and fake.products["P3"]["supplierCode"] == "BADSUP"
    assert not [i for i in items("product") if i.error_code == "supplier_link_dropped"] and out["summary"]["readback"]["mismatch_fields"] == {}


def test_stale_plan_fingerprint_is_refused_before_any_item_is_written(clean_db, tmp_path):
    fake = Fake()
    env = mk(clean_db, tmp_path, fake)
    d = dry(env)
    r = post(env, steps=["suppliers", "products"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint="0" * 64)
    out = finish(env, r.json()["run_id"])
    assert out["status"] == "failed" and out["summary"]["error"] == "plan_fingerprint_mismatch"
    assert items() == [] and fake.writes == [] and not engine._ACTIVE.locked()


def test_samples_carry_list_ids_not_names_and_failures_come_first(clean_db, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(engine, "SAMPLE_LIMIT", 2)
    caplog.set_level(logging.DEBUG)
    rows = [(e, i, {**p, "Name": "SENTINEL-VENDOR"} if i == "V3" else p) for e, i, p in ROWS]
    fake = Fake(bad_suppliers={"SENTINEL-VENDOR"}, on_product=lambda n, b: httpx.Response(400, json={"title": "x"}) if b["productId"] == "P6" else None)
    env = mk(clean_db, tmp_path, fake, rows=rows)
    out = finish(env, go(env).json()["run_id"])
    assert out["status"] == "done_with_failures"
    assert {i.key: i.error_code for i in items() if i.error_code} == {"SENTINEL-VENDOR": "rejected_400", "P3": "supplier_link_dropped", "P6": "rejected_400"}
    # by id the dropped-link warning (P3) would come second; the real failures go first
    assert out["samples"] == [{"entity": "supplier", "ref": "V3", "error_code": "rejected_400"}, {"entity": "product", "ref": "I6", "error_code": "rejected_400"}]
    assert "SENTINEL-VENDOR" not in json.dumps(out) + caplog.text


def test_clean_shutdown_cancels_the_active_run_and_waits(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    release, state = threading.Event(), {}
    fake = Fake(on_product=lambda n, b: release.wait(10) and None)
    env = mk(clean_db, tmp_path, fake)

    async def life():
        async with main.lifespan(env.app):
            state["run_id"] = go(env).json()["run_id"]
            threading.Timer(0.3, release.set).start()  # the in-flight chunk finishes while the shutdown waits
        state["locked_after_shutdown"] = engine._ACTIVE.locked()

    asyncio.run(life())
    assert state["locked_after_shutdown"] is False  # the worker was joined inside the shutdown
    out = env.get(f"/api/v1/push/runs/{state['run_id']}").json()
    assert out["status"] == "cancelled" and out["counts"]["product"].get("pending", 0) > 0
    release.clear()
    fake.on_product = None
    resume(env, state["run_id"])  # and it is resumable
    assert finish(env, state["run_id"])["status"] == "done"


def test_no_detail_is_sql_null_not_json_null(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake())
    finish(env, go(env).json()["run_id"])
    with session_scope() as s:
        assert s.scalar(text("SELECT count(*) FROM push_item WHERE detail = 'null'::jsonb")) == 0
        assert s.scalar(text("SELECT count(*) FROM push_item WHERE detail IS NULL")) >= 3  # the suppliers carry no detail
