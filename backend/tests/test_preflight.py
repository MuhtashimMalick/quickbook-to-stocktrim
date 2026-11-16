"""Offline tests for POST /api/v1/preflight (synthetic data only; StockTrim is an httpx MockTransport that records every request)."""
import json
import logging
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.api.v1.preflight as route
import app.preflight.stocktrim_inventory as inv
from app.api.v1.preflight import get_stocktrim_client_kw
from app.config import Settings
from app.logging_config import setup_logging
from app.models import QBRecord, SyncLog
from app.preflight.report import preflight_client
from app.preflight.stocktrim_inventory import PRODUCT_PAGE
from app.stocktrim.api import StockTrimApi
from app.stocktrim.dto import ProductRequest

from .conftest import HEADERS, PASSWORD, TOKEN, USER, Env

CLEAN = [
    ("Vendor", "V1", {"ListID": "V1", "Name": "Acme"}),
    ("ItemInventory", "I1", {"ListID": "I1", "Name": "A1", "QuantityOnHand": "-2", "PrefVendorRef": {"ListID": "V1"}}),
    ("ItemInventory", "I2", {"ListID": "I2", "Name": "b  2"}),
    ("ItemInventoryAssembly", "AS1", {"ListID": "AS1", "Name": "ASM", "ItemInventoryAssemblyLine": ""}),
    ("Customer", "C1", {"ListID": "C1", "Name": "c one"}),
    ("Customer", "C2", {"ListID": "C2", "Name": "c two"}),
]
DIRTY = [
    ("Vendor", "V1", {"ListID": "V1", "Name": "SENTINEL-dup", "CompanyName": "SENTINEL-company"}),
    ("Vendor", "V2", {"ListID": "V2", "Name": "sentinel-DUP", "Email": "SENTINEL@x.y"}),
    ("ItemInventory", "I1", {"ListID": "I1", "Name": "ok1", "SalesDesc": "SENTINEL-DESC"}),
    ("ItemInventory", "I2", {"ListID": "I2", "Name": "SENTINEL-bad ", "SalesDesc": "SENTINEL-DESC"}),
    ("ItemInventory", "I3", {"ListID": "I3", "Name": "p3", "Max": "SENTINEL-nan"}),
    ("Customer", "C1", {"ListID": "C1", "Name": "SENTINEL-cust"}),
]


class Fake:
    """In-memory StockTrim. Records every request; any non-GET is answered 405 and listed in .writes (asserted empty)."""

    def __init__(self, products=(), customers=(), pos=(), suppliers=(), sales=(), mode="ok", supplier_status=None, bad_codes=(), sales_bad=False, pos_page0=()):
        self.products, self.customers, self.pos, self.suppliers, self.sales = list(products), list(customers), list(pos), list(suppliers), set(sales)
        self.mode, self.supplier_status, self.bad_codes, self.sales_bad, self.pos_page0 = mode, supplier_status, set(bad_codes), sales_bad, list(pos_page0)
        self.requests, self.writes = [], []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method != "GET":
            self.writes.append((req.method, req.url.path))
            return httpx.Response(405)
        if self.mode == "500":
            return httpx.Response(500)
        if self.mode == "401":
            return httpx.Response(401, json={"title": "bad signature"})
        q, path = dict(req.url.params), req.url.path
        page = int(q.get("pageNo") or q.get("page") or 1)
        if path == "/api/Products":
            return httpx.Response(200, json=[{"productId": p[0], "productCodeReadable": p[1], "name": p[2] if len(p) > 2 else None}
                                             for p in self.products[(page - 1) * PRODUCT_PAGE: page * PRODUCT_PAGE]])
        if path == "/api/Customers":
            return httpx.Response(200, json=[{"code": c[0], "name": c[1]} if isinstance(c, tuple) else {"code": c} for c in self.customers])
        if path == "/api/V2/PurchaseOrders":
            rows = [p for p in (self.pos_page0 if page == 0 else self.pos) if q.get("status") in (None, p)][(max(page, 1) - 1) * 50: max(page, 1) * 50]
            return httpx.Response(200, json=[{"status": s} for s in rows])
        if path == "/api/SuppliersBulk":
            if self.supplier_status:
                return httpx.Response(self.supplier_status)
            return httpx.Response(200, json=[{"supplierCode": s, "supplierName": "SENTINEL-sname"} for s in self.suppliers])
        if path == "/api/Suppliers":
            if self.supplier_status:
                return httpx.Response(self.supplier_status)
            if q["code"] in self.bad_codes:
                return httpx.Response(400, json={"errors": {"code": ["SENTINEL-bad"]}})
            return httpx.Response(200, json={"supplierCode": q["code"]}) if q["code"] in self.suppliers else httpx.Response(404, json={"title": "no"})
        if path == "/api/SalesOrders":
            if self.sales_bad:
                return httpx.Response(200, json={"unexpected": "SENTINEL-body"})
            return httpx.Response(200, json=[{"productId": q["productId"]}]) if q["productId"] in self.sales else httpx.Response(404)
        return httpx.Response(404)

    def paths(self, p: str) -> int:
        return sum(1 for r in self.requests if r.url.path == p)


def make(clean_db, tmp_path, rows, fake=None, creds=True) -> Env:
    engine = create_engine(clean_db)
    with Session(engine) as s:
        s.add_all(QBRecord(entity=e, qb_id=i, payload=p) for e, i, p in rows)
        s.commit()
    engine.dispose()
    extra = {"stocktrim_auth_id": "id-1", "stocktrim_auth_signature": "sig-1"} if creds else {}
    env = Env(Settings(_env_file=None, qbwc_username=USER, qbwc_password=PASSWORD, admin_token=TOKEN, qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(),
                       database_url=clean_db, fixtures_dir=tmp_path / "fx", log_dir=tmp_path / "logs", stocktrim_max_rps=50, **extra))
    if fake is not None:
        kw = {"transport": httpx.MockTransport(fake), "max_attempts": 1, "sleep": lambda s: None}
        env.app.dependency_overrides[get_stocktrim_client_kw] = lambda: kw
    return env


def run(env: Env, **body) -> dict:
    r = env.client.post("/api/v1/preflight", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def codes(items):
    return sorted(i["code"] for i in items)


def test_quickbooks_section_counts_samples_and_no_content(clean_db, tmp_path):
    env = make(clean_db, tmp_path, DIRTY)
    r = run(env, stocktrim=False, sample_limit=1)
    text = json.dumps(r)
    assert "SENTINEL" not in text and "sentinel" not in text.lower()
    e = r["quickbooks"]["entities"]
    assert e["suppliers"] == {"parsed": 2, "ok": 0, "quarantined": 2, "parse_errors": 0, "warnings": 0, "inactive": 0}
    assert (e["products"]["parsed"], e["products"]["ok"], e["products"]["quarantined"], e["products"]["parse_errors"]) == (2, 1, 2, 1)
    assert e["customers"]["ok"] == 1
    iss = r["quickbooks"]["issues"]
    assert iss["supplier.duplicate_key"]["count"] == 2 and len(iss["supplier.duplicate_key"]["samples"]) == 1  # sample_limit respected
    for g in iss.values():
        assert set(g) == {"severity", "count", "samples"}
        assert all(set(s) == {"ref", "code"} for s in g["samples"])
    assert iss["ItemInventory.parse_error"]["samples"] == [{"ref": "I3", "code": "parse_error"}]
    assert "depth_histogram" in r["quickbooks"]["bom"] and "negative_stock_count" in r["quickbooks"]
    assert r["stocktrim"] == {"status": "skipped"}
    assert not r["verdict"]["ok_to_push"] and {"qb_suppliers_quarantined", "qb_products_quarantined", "stocktrim_skipped"} <= set(codes(r["verdict"]["blocking"]))


def test_sample_limit_zero_and_bounds(clean_db, tmp_path):
    env = make(clean_db, tmp_path, DIRTY)
    assert all(g["samples"] == [] for g in run(env, stocktrim=False, sample_limit=0)["quickbooks"]["issues"].values())
    for bad in (-1, 501):
        assert env.client.post("/api/v1/preflight", json={"sample_limit": bad}, headers=HEADERS).status_code == 422
    assert env.client.post("/api/v1/preflight", json={"nope": 1}, headers=HEADERS).status_code == 422


def test_empty_account_is_ok_and_read_only(clean_db, tmp_path):
    fake = Fake()
    env = make(clean_db, tmp_path, CLEAN, fake)
    engine = create_engine(clean_db)
    with Session(engine) as s:
        before = s.scalar(select(func.count()).select_from(SyncLog))
    r = run(env)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(SyncLog)) == before  # nothing written to sync_log
    engine.dispose()
    assert r["verdict"]["ok_to_push"] is True and r["verdict"]["blocking"] == []
    assert {"internal_double_space", "negative_stock", "sales_orders_not_checked"} <= set(codes(r["verdict"]["warnings"]))
    st = r["stocktrim"]
    assert st["status"] == "ok" and st["products"]["count"] == 0 and st["suppliers"] == {"status": "ok", "count": 0, "overlap": 0, "samples": []}
    assert "suppliers_by_code" not in st and fake.paths("/api/Suppliers") == 0 and fake.paths("/api/SuppliersBulk") == 1
    assert st["boms"] == {"status": "unknown_cannot_list"} and st["sales_orders"] == {"status": "skipped"}
    assert fake.writes == [] and fake.paths("/api/SalesOrders") == 0
    assert r["plan"] == {"planned_calls": {"products": 3, "suppliers": 1, "customers": 2, "bom_edges": 0}, "total_calls": 6, "rps": 50.0, "eta_seconds": 1}
    assert set(r["snapshot"]["synced_at_max"]) == {"Customer", "Vendor", "ItemInventory", "ItemInventoryAssembly"} and len(r["fingerprint"]) == 64
    assert run(env)["fingerprint"] == r["fingerprint"]  # same snapshot + same StockTrim state -> same fingerprint
    assert not (tmp_path / "fx").exists() or not any((tmp_path / "fx").iterdir())


@pytest.mark.parametrize("kw,entity", [({"products": [("zz", "zz")]}, "products"), ({"suppliers": ["Other"]}, "suppliers"),
                                       ({"customers": ["x"]}, "customers"), ({"pos": ["Draft"]}, "purchase_orders"),
                                       ({"pos": [0]}, "purchase_orders"),  # integer status 0: no status filter finds it, only the unfiltered list
                                       ({"pos_page0": ["Draft"]}, "purchase_orders")])  # visible only with page=0
def test_non_empty_account_blocks(clean_db, tmp_path, kw, entity):
    fake = Fake(**kw)
    r = run(make(clean_db, tmp_path, CLEAN, fake))
    b = r["verdict"]["blocking"]
    assert r["verdict"]["ok_to_push"] is False and codes(b) == ["stocktrim_not_empty"] and b[0]["detail"] == {entity: 1}
    assert fake.writes == []
    if entity == "suppliers":
        assert r["stocktrim"]["suppliers"]["overlap"] == 0  # blocks on count alone, no key overlap needed


def test_sales_orders_opt_in_blocks(clean_db, tmp_path):
    fake = Fake(sales=["A1"])
    r = run(make(clean_db, tmp_path, CLEAN, fake), check_sales_orders=True)
    assert fake.paths("/api/SalesOrders") == 3 and r["stocktrim"]["sales_orders"]["found"] == 1
    assert r["stocktrim"]["sales_orders"]["samples"] == [{"ref": "I1", "code": "has_orders"}]  # ListID, not the SKU
    assert r["verdict"]["blocking"][0]["detail"] == {"sales_orders": 1} and "sales_orders_not_checked" not in codes(r["verdict"]["warnings"])


def test_overlap_readable_and_customer_collisions(clean_db, tmp_path):
    prods = [("a1 ", "A1-READ"), ("B  2", "B  2"), ("other", "other"), ("x", "dup"), ("y", "DUP ")]  # id overlap A1 (case + trailing space); both overlap b  2
    r = run(make(clean_db, tmp_path, CLEAN, Fake(products=prods, customers=["c1 ", "C1", "zzz", "dup", "DUP ", "dup2", ""])), check_suppliers=False)
    p, c = r["stocktrim"]["products"], r["stocktrim"]["customers"]
    assert p["count"] == 5 and p["readable_differs_from_product_id"] == 3 and p["duplicate_readable_groups"] == 1  # {dup, "DUP "}
    assert p["overlap_product_id"] == {"count": 2, "samples": [{"ref": "I1", "code": "overlap_product_id"}, {"ref": "I2", "code": "overlap_product_id"}]}
    assert p["overlap_readable"]["count"] == 1 and p["overlap_readable_only"] == {"count": 0, "samples": []}
    assert c["count"] == 7 and c["code_collisions"] == 2 and c["duplicate_code_groups"] == 2  # {c1, C1} and {dup, DUP }
    assert c["samples"] == [{"ref": "C1", "code": "code_collision"}] * 2
    assert r["stocktrim"]["suppliers"] == {"status": "skipped"} and "stocktrim_suppliers_not_checked" in codes(r["verdict"]["blocking"])
    nonempty = next(b for b in r["verdict"]["blocking"] if b["code"] == "stocktrim_not_empty")
    assert nonempty["detail"]["products_overlap_product_id"] == 2


def test_readable_only_overlap_blocks_as_conflict(clean_db, tmp_path):
    r = run(make(clean_db, tmp_path, CLEAN, Fake(products=[("someone-else", "A1")])))  # another product holds our key as its readable code
    p = r["stocktrim"]["products"]
    assert p["overlap_product_id"]["count"] == 0 and p["overlap_readable"]["count"] == 1 and p["overlap_readable_only"]["samples"] == [{"ref": "I1", "code": "overlap_readable_only"}]
    assert codes(r["verdict"]["blocking"]) == ["stocktrim_not_empty", "stocktrim_readable_conflict"]


def test_paging_until_empty_and_cap(clean_db, tmp_path, monkeypatch):
    fake = Fake(products=[(f"p{i}", f"p{i}") for i in range(120)], pos=["Draft"] * 60)
    env = make(clean_db, tmp_path, CLEAN, fake)
    st = run(env, check_suppliers=False)["stocktrim"]
    assert st["products"]["count"] == 120 and not st["products"]["capped"]
    prod_pages = [r.url.params.get("pageNo") for r in fake.requests if r.url.path == "/api/Products"]
    assert prod_pages == ["1", "2", "3", "4"]  # page 1 comes from the credential check (not fetched twice); 4 is the empty page
    assert st["purchase_orders"]["by_status"] == {"Draft": 60, "Approved": 0, "Sent": 0, "Received": 0, "any": 60}
    draft = [r.url.params["page"] for r in fake.requests if r.url.path == "/api/V2/PurchaseOrders" and r.url.params.get("status") == "Draft"]
    assert draft == ["1", "2", "0"]  # short page stops the walk, then the page=0 variant
    assert sum(1 for r in fake.requests if r.url.path == "/api/V2/PurchaseOrders" and "status" not in r.url.params) >= 3  # unfiltered walk + page 0
    monkeypatch.setattr(inv, "MAX_PAGES", 2)
    r = run(env, check_suppliers=False)
    assert r["stocktrim"]["products"]["count"] == 100 and r["stocktrim"]["products"]["capped"] is True
    assert not r["verdict"]["ok_to_push"]
    assert [b for b in r["verdict"]["blocking"] if b["code"] == "stocktrim_incomplete"][0]["detail"] == {"products": 1}


@pytest.mark.parametrize("mode", ["500", "401"])
def test_bad_credentials_stop_early(clean_db, tmp_path, mode):
    fake = Fake(mode=mode)
    r = run(make(clean_db, tmp_path, CLEAN, fake))
    assert r["stocktrim"]["status"] == "unreachable_or_bad_credentials" and len(fake.requests) == 1
    assert r["quickbooks"]["entities"]["products"]["ok"] == 3
    assert codes(r["verdict"]["blocking"]) == ["stocktrim_unreachable_or_bad_credentials"] and not r["verdict"]["ok_to_push"]


def test_credentials_not_configured_still_returns_quickbooks(clean_db, tmp_path):
    r = run(make(clean_db, tmp_path, CLEAN, creds=False))
    assert r["stocktrim"] == {"status": "not_configured"} and r["quickbooks"]["entities"]["customers"]["ok"] == 2
    assert codes(r["verdict"]["blocking"]) == ["stocktrim_not_configured"]


def test_supplier_bulk_overlap_and_by_code_cross_check(clean_db, tmp_path):
    rows = [("Vendor", f"V{i}", {"ListID": f"V{i}", "Name": f"S{i}"}) for i in range(5)]
    fake = Fake(suppliers=["S1", "S3", "elsewhere"])
    r = run(make(clean_db, tmp_path, rows + CLEAN[1:2], fake), check_suppliers_by_code=True)
    st = r["stocktrim"]
    assert st["suppliers"] == {"status": "ok", "count": 3, "overlap": 2, "samples": [{"ref": "V1", "code": "overlap"}, {"ref": "V3", "code": "overlap"}]}
    assert st["suppliers_by_code"] == {"status": "ok", "checked": 5, "found": 2, "errors": 0, "samples": [{"ref": "V1", "code": "found"}, {"ref": "V3", "code": "found"}]}
    assert fake.paths("/api/Suppliers") == 5


def test_supplier_probe_400_makes_the_run_incomplete(clean_db, tmp_path):
    rows = [("Vendor", f"V{i}", {"ListID": f"V{i}", "Name": f"S{i}"}) for i in range(3)] + CLEAN[1:2]
    r = run(make(clean_db, tmp_path, rows, Fake(bad_codes=["S1"])), check_suppliers_by_code=True)
    assert r["stocktrim"]["suppliers_by_code"]["errors"] == 1 and r["stocktrim"]["status"] == "ok"
    b = r["verdict"]["blocking"]
    assert r["verdict"]["ok_to_push"] is False and codes(b) == ["stocktrim_incomplete"] and b[0]["detail"] == {"suppliers_by_code": 1}


def test_sales_order_off_schema_body_makes_the_run_incomplete(clean_db, tmp_path):
    r = run(make(clean_db, tmp_path, CLEAN, Fake(sales_bad=True)), check_sales_orders=True)
    assert r["stocktrim"]["sales_orders"]["errors"] == 3 and "SENTINEL" not in json.dumps(r)
    b = r["verdict"]["blocking"]
    assert r["verdict"]["ok_to_push"] is False and codes(b) == ["stocktrim_incomplete"] and b[0]["detail"] == {"sales_orders": 3}


def test_unchecked_suppliers_block(clean_db, tmp_path):
    r = run(make(clean_db, tmp_path, CLEAN, Fake()), check_suppliers=False)
    assert r["verdict"]["ok_to_push"] is False and codes(r["verdict"]["blocking"]) == ["stocktrim_suppliers_not_checked"]


def test_empty_snapshot_blocks(clean_db, tmp_path):
    r = run(make(clean_db, tmp_path, [], Fake()))
    assert r["verdict"]["ok_to_push"] is False and codes(r["verdict"]["blocking"]) == ["qb_snapshot_empty"]


def test_stocktrim_down_mid_run_gives_partial_report(clean_db, tmp_path):
    rows = [("Vendor", f"V{i}", {"ListID": f"V{i}", "Name": f"S{i}"}) for i in range(10)]
    fake = Fake(products=[("x", "x")], supplier_status=500)
    r = run(make(clean_db, tmp_path, rows, fake))
    st = r["stocktrim"]
    assert st["status"] == "aborted" and st["suppliers"]["status"] == "aborted" and st["products"]["count"] == 1 and "sales_orders" not in st
    assert "stocktrim_incomplete" in codes(r["verdict"]["blocking"]) and not r["verdict"]["ok_to_push"]
    assert fake.writes == []


def test_second_concurrent_run_is_refused(clean_db, tmp_path):
    env = make(clean_db, tmp_path, CLEAN, Fake())
    assert route._RUNNING.acquire(blocking=False)  # a run is in progress
    try:
        r = env.client.post("/api/v1/preflight", json={}, headers=HEADERS)
        assert r.status_code == 409 and r.json()["detail"] == "preflight_running"
    finally:
        route._RUNNING.release()
    assert env.client.post("/api/v1/preflight", json={}, headers=HEADERS).status_code == 200  # and the lock is free again


def test_client_refuses_writes_even_when_called_directly(clean_db, tmp_path):
    fake = Fake()
    env = make(clean_db, tmp_path, CLEAN, fake)
    client = preflight_client(env.settings, {"transport": httpx.MockTransport(fake), "max_attempts": 1})
    api = StockTrimApi(client)
    with pytest.raises(RuntimeError, match="read-only"):
        api.post_product(ProductRequest(product_id="p", product_code_readable="p", stock_on_hand=1))
    with pytest.raises(RuntimeError, match="read-only"):
        api.delete_supplier("x")
    assert fake.requests == [] and api.get_products() == []  # reads still work
    client.close()


def test_no_client_content_in_response_or_logs(clean_db, tmp_path, caplog):
    """Sentinels sit in QuickBooks names/descriptions (vendor, product, BOM cycle members) AND in what StockTrim returns (found vendor, product,
    customer); httpx would log the Vendor Name inside the request URL unless setup_logging silences it."""
    setup_logging()
    rows = [
        ("Vendor", "V1", {"ListID": "V1", "Name": "SENTINEL-vendor", "CompanyName": "SENTINEL-co"}),
        ("ItemInventory", "I1", {"ListID": "I1", "Name": "SENTINEL-prod", "SalesDesc": "SENTINEL-desc", "PrefVendorRef": {"ListID": "V1"}}),
        ("ItemInventoryAssembly", "A1", {"ListID": "A1", "Name": "SENTINEL-asm1", "ItemInventoryAssemblyLine": {"ItemInventoryRef": {"ListID": "A2"}, "Quantity": "1"}}),
        ("ItemInventoryAssembly", "A2", {"ListID": "A2", "Name": "SENTINEL-asm2", "ItemInventoryAssemblyLine": {"ItemInventoryRef": {"ListID": "A1"}, "Quantity": "1"}}),
        ("Customer", "C1", {"ListID": "C1", "Name": "SENTINEL-cust"}),
    ]
    fake = Fake(products=[("SENTINEL-prod", "SENTINEL-prod", "SENTINEL-pname")], customers=[("C1", "SENTINEL-cname")], suppliers=["SENTINEL-vendor"],
                sales=["SENTINEL-prod"])
    env = make(clean_db, tmp_path, rows, fake)
    caplog.set_level(logging.DEBUG)  # root logger: every library
    r = run(env, check_suppliers_by_code=True, check_sales_orders=True, sample_limit=500)
    st = r["stocktrim"]
    assert st["suppliers_by_code"]["found"] == 1 and st["suppliers"]["overlap"] == 1 and st["sales_orders"]["found"] == 1 and st["customers"]["code_collisions"] == 1
    assert any("SENTINEL-vendor" in q.url.query.decode() for q in fake.requests)  # the name really was sent
    assert r["quickbooks"]["bom"]["quarantined_parents"] == 2 and "bom_cycle" in json.dumps(r)
    assert "sentinel" not in json.dumps(r).lower()
    assert caplog.records and not [q for q in caplog.records if "sentinel" in (q.getMessage() + str(q.exc_text or "")).lower()]
    assert not [q for q in caplog.records if q.name.split(".")[0] in ("httpx", "httpcore")]


def test_requires_admin_token(clean_db, tmp_path):
    env = make(clean_db, tmp_path, CLEAN)
    assert env.client.post("/api/v1/preflight", json={}).status_code == 401
    assert env.client.post("/api/v1/preflight", json={}, headers={"X-Admin-Token": "nope"}).status_code == 401
