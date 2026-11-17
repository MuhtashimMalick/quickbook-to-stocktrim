"""Offline tests of the push engine's BOM and customer steps (stage 10): synthetic data only, StockTrim is an in-memory httpx MockTransport."""
import json
import logging
import threading
from collections import Counter
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select

from app.api.v1.preflight import get_stocktrim_client_kw
from app.db import session_scope
from app.models import BomEdge, QBRecord
from app.preflight.stocktrim_inventory import _foreign
from app.stocktrim.target import stocktrim_target
from app.sync import engine
from app.sync.engine import plan_fingerprint
from app.sync.mappers import bom_edge_key

from .conftest import HEADERS, Env
from .test_push import ROWS, Fake, dry, finish, go, items, mk, post, resume

ASM = [
    ("ItemInventoryAssembly", "A1", {"ListID": "A1", "Name": "ASM1", "SalesDesc": "SENTINEL-asm1", "QuantityOnHand": "2", "ItemInventoryAssemblyLine": [
        {"ItemInventoryRef": {"ListID": "I1"}, "Quantity": "2"}, {"ItemInventoryRef": {"ListID": "I2"}, "Quantity": "1.5"},
        {"ItemInventoryRef": {"ListID": "A2"}, "Quantity": "3"}]}),
    ("ItemInventoryAssembly", "A2", {"ListID": "A2", "Name": "ASM2", "SalesDesc": "SENTINEL-asm2", "QuantityOnHand": "0", "ItemInventoryAssemblyLine": [
        {"ItemInventoryRef": {"ListID": "I4"}, "Quantity": "0.123456"}, {"ItemInventoryRef": {"ListID": "I5"}, "Quantity": "1"}]}),
]
CUST = [
    ("Customer", "C1", {"ListID": "C1", "FullName": "SENTINEL-cust-1", "Email": "SENTINEL-1@example.com", "Phone": "SENTINEL-555-0101",
                        "BillAddress": {"Addr1": "SENTINEL-street 1", "City": "Town", "PostalCode": "12345"}}),
    ("Customer", "C2", {"ListID": "C2", "FullName": "SENTINEL-cust-2"}),
    ("Customer", "C3", {"ListID": "C3", "FullName": "SENTINEL-inactive", "IsActive": "false"}),
]
ROWS10 = [*ROWS, *ASM, *CUST]
EDGES = {("ASM1", "P1"): "2", ("ASM1", "P2"): "1.5", ("ASM1", "ASM2"): "3", ("ASM2", "P4"): "0.12346", ("ASM2", "P5"): "1"}  # 0.123456 rounded to 5 places
ALL = ("suppliers", "products", "bom", "customers")


class Fake10(Fake):
    """Fake + /api/boms (POST upsert by pair, GET, DELETE) + /api/Customers (PUT upsert, GET list); lists what was pushed, so sync mode sees 'our' records.
    on_bom(n, method, pair) / on_customer(n, body) -> Response|None run before the call is judged (n counts per kind)."""

    def __init__(self, *a: Any, on_bom: Optional[Callable] = None, on_customer: Optional[Callable] = None, **kw: Any):
        super().__init__(*a, **kw)
        self.edges: dict[tuple[str, str], float] = {}
        self.pos: list[str] = []  # purchase orders (only their presence matters)
        self.customers: dict[str, dict] = {}
        self.bom_calls: list[tuple[str, tuple[str, str]]] = []
        self.put_codes: Counter = Counter()
        self.on_bom, self.on_customer, self.nb, self.nc = on_bom, on_customer, 0, 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, dict(req.url.params)
        if path == "/api/boms":
            pair = (json.loads(req.content)["productId"], json.loads(req.content)["componentId"]) if req.method == "POST" else (q["productId"], q["componentId"])
            with self.lock:
                self.nb += 1
                n = self.nb
                self.log.append((req.method, path))
                if req.method != "GET":
                    self.writes.append((req.method, path))
            if self.on_bom and (resp := self.on_bom(n, req.method, pair)):
                return resp
            with self.lock:
                if req.method == "POST":
                    self.bom_calls.append(("POST", pair))
                    self.edges[pair] = json.loads(req.content)["quantity"]
                    return httpx.Response(200, json={"id": 1, "productId": pair[0], "componentId": pair[1], "quantity": self.edges[pair]})
                if req.method == "DELETE":
                    self.bom_calls.append(("DELETE", pair))
                    return httpx.Response(200) if self.edges.pop(pair, None) is not None else httpx.Response(404)
                return httpx.Response(200, json={"productId": "1", "componentId": "2", "quantity": self.edges[pair]}) if pair in self.edges else httpx.Response(404)
        if path == "/api/Customers":
            if req.method == "PUT":
                body = json.loads(req.content)
                with self.lock:
                    self.nc += 1
                    n = self.nc
                    self.log.append((req.method, path))
                    self.writes.append((req.method, path))
                if self.on_customer and (resp := self.on_customer(n, body)):
                    return resp
                with self.lock:
                    self.put_codes[body["code"]] += 1
                    self.customers[body["code"].casefold()] = body
                return httpx.Response(200, json=body)
            self.log.append((req.method, path))
            return httpx.Response(200, json=list(self.customers.values()))
        if path == "/api/V2/PurchaseOrders":
            self.log.append((req.method, path))
            return httpx.Response(200, json=[{"status": "Draft"} for _ in self.pos] if q.get("status") in (None, "Draft") and int(q.get("page") or 1) in (0, 1) else [])
        if req.method == "GET" and path == "/api/SuppliersBulk":
            self.log.append((req.method, path))
            return httpx.Response(200, json=[{"supplierCode": c} for c in self.suppliers])
        if req.method == "GET" and path == "/api/Products" and "code" not in q:
            self.log.append((req.method, path))
            rows = [{"productId": k, "productCodeReadable": k} for k in self.products] + [{"productId": f"X{i}", "productCodeReadable": f"X{i}"} for i in range(self.existing)]
            return httpx.Response(200, json=rows if int(q.get("pageNo") or 1) == 1 else [])
        return super().__call__(req)


def retries(env: Env, attempts: int) -> None:
    kw = env.app.dependency_overrides[get_stocktrim_client_kw]()
    env.app.dependency_overrides[get_stocktrim_client_kw] = lambda: {**kw, "max_attempts": attempts}


def run_all(env: Env, steps=ALL) -> dict:
    return finish(env, go(env, steps).json()["run_id"])


def set_line(list_id: str, comp_lid: str, qty: Optional[str]) -> None:
    """Change (or with qty=None remove) one BOM line of an assembly in the stored snapshot."""
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == list_id)).one()
        lines = [dict(ln, Quantity=qty) if ln["ItemInventoryRef"]["ListID"] == comp_lid else ln for ln in rec.payload["ItemInventoryAssemblyLine"]]
        rec.payload = {**rec.payload, "ItemInventoryAssemblyLine": [ln for ln in lines if ln["Quantity"] is not None]}


def stored_edges() -> dict[tuple[str, str], tuple[Decimal, str]]:
    with session_scope() as s:
        return {(r.parent_sku, r.component_sku): (r.quantity, r.parent_hash) for r in s.scalars(select(BomEdge))}


def test_full_run_of_all_four_steps(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    d = dry(env, ALL)
    assert d["plan"]["bom_edges"] == 5 and d["plan"]["customers"] == 2 and d["plan"]["bom_to_post"] == 5 and d["plan"]["bom_to_delete"] == 0
    assert fake.writes == []
    out = finish(env, go(env, ALL).json()["run_id"])
    assert out["status"] == "done" and out["counts"] == {"supplier": {"done": 3}, "product": {"done": 8}, "bom_edge": {"done": 5}, "customer": {"done": 2}}
    assert out["steps"] == list(ALL) and out["error_codes"] == {}
    kinds = [p for _, p in fake.log if p in ("/api/Suppliers", "/api/Products", "/api/boms", "/api/Customers") and _ != "GET"]
    assert kinds == sorted(kinds, key=("/api/Suppliers", "/api/Products", "/api/boms", "/api/Customers").index)  # suppliers -> products -> bom -> customers
    assert fake.edges == {k: float(v) for k, v in EDGES.items()}
    assert fake.edges[("ASM2", "P4")] == 0.12346  # rounded like StockTrim stores it, so a later sync sees no diff
    posts = [p for m, p in fake.bom_calls]
    assert max(posts.index(p) for p in posts if p[0] == "ASM2") < min(posts.index(p) for p in posts if p[0] == "ASM1")  # children's BOMs before their parents'
    rows = stored_edges()
    assert {k: v[0] for k, v in rows.items()} == {k: Decimal(v) for k, v in EDGES.items()} and all(h and len(h) == 64 for _, h in rows.values())  # every parent complete: hash stamped
    assert {i.key for i in items("bom_edge")} == {bom_edge_key(*k) for k in EDGES} and {i.ref for i in items("bom_edge")} == {"A1", "A2"}
    assert set(fake.put_codes) == {"C1", "C2"} and set(fake.put_codes.values()) == {1}  # only the active customers, code = ListID
    assert fake.customers["c1"] == {"code": "C1", "name": "SENTINEL-cust-1", "emailAddress": "SENTINEL-1@example.com", "phone": "SENTINEL-555-0101",
                                    "streetAddress": "SENTINEL-street 1", "city": "Town", "postCode": "12345"}
    assert fake.customers["c2"] == {"code": "C2", "name": "SENTINEL-cust-2"}


def test_unchanged_second_run_makes_no_calls_then_a_changed_and_a_removed_edge(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    fake.bom_calls.clear()
    d = dry(env, ("bom",))
    assert d["plan"]["bom_to_post"] == 0 and d["plan"]["bom_to_delete"] == 0 and d["verdict"]["ok_to_push"]  # sync mode: the account holds exactly our records
    n_before, rows_before = len(fake.writes), stored_edges()
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] == "done" and out["counts"] == {} and fake.bom_calls == [] and len(fake.writes) == n_before
    assert stored_edges() == rows_before and all(h for _, h in rows_before.values())  # an unchanged run keeps every parent_hash
    set_line("A2", "I5", "4")  # changed quantity: ONE POST
    assert finish(env, go(env, ("bom",)).json()["run_id"])["counts"] == {"bom_edge": {"done": 1}}
    assert fake.bom_calls == [("POST", ("ASM2", "P5"))] and stored_edges()[("ASM2", "P5")][0] == Decimal(4)
    fake.bom_calls.clear()
    set_line("A1", "I2", None)  # removed edge: DELETE with both readable codes, then the ledger row goes
    assert dry(env, ("bom",))["plan"]["bom_to_delete"] == 1
    assert finish(env, go(env, ("bom",)).json()["run_id"])["counts"] == {"bom_edge": {"done": 1}}
    assert fake.bom_calls == [("DELETE", ("ASM1", "P2"))] and ("ASM1", "P2") not in stored_edges() and ("ASM1", "P1") in stored_edges()
    assert ("ASM1", "P2") not in fake.edges


def test_delete_404_counts_as_success_after_a_transient_failure_and_when_plain(clean_db, tmp_path):
    state = Counter()

    def on_bom(n, method, pair):
        if method == "DELETE" and pair == ("ASM1", "P1"):  # lost response: the first try did delete, the retry sees 404
            state["a"] += 1
            if state["a"] == 1:
                fake.edges.pop(pair)
                return httpx.Response(503)
        if method == "DELETE" and pair == ("ASM1", "P2"):
            fake.edges.pop(pair)
            return httpx.Response(404)  # gone already, no transient failure involved
        return None

    fake = Fake10(on_bom=on_bom)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    retries(env, 3)
    assert run_all(env)["status"] == "done"
    set_line("A1", "I1", None)
    set_line("A1", "I2", None)
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] == "done" and out["counts"] == {"bom_edge": {"done": 2}} and state["a"] == 2
    assert set(stored_edges()) == {k for k in EDGES if k not in (("ASM1", "P1"), ("ASM1", "P2"))}


def test_edge_with_a_product_that_is_not_done_gets_dependency_failed_then_resume_completes(clean_db, tmp_path):
    fake = Fake10(on_product=lambda n, b: httpx.Response(400, json={"title": "x"}) if b["productId"] == "P4" and not fake.fixed else None)
    fake.fixed = False
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    out = run_all(env)
    assert out["status"] == "done_with_failures" and out["counts"]["bom_edge"] == {"done": 4, "failed": 1} and out["error_codes"]["bom_edge"] == {"dependency_failed": 1}
    assert [i.key for i in items("bom_edge") if i.status == "failed"] == [bom_edge_key("ASM2", "P4")] and ("ASM2", "P4") not in fake.edges
    rows = stored_edges()
    assert rows[("ASM2", "P5")][1] == "" and rows[("ASM1", "P1")][1]  # ASM2 is incomplete: its parent_hash is NOT stamped; ASM1 is complete
    fake.fixed = True
    resume(env, out["run_id"])
    out = finish(env, out["run_id"])
    assert out["status"] == "done" and out["counts"]["bom_edge"] == {"done": 5}
    assert stored_edges()[("ASM2", "P5")][1] and stored_edges()[("ASM2", "P4")][1] == stored_edges()[("ASM2", "P5")][1]


def test_edge_row_and_done_are_one_transaction(clean_db, tmp_path, monkeypatch):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    orig, boom = engine._mark_stmt, []

    def flaky(run_id, entity, keys, status, *a):
        if entity == "bom_edge" and status == "done" and keys == [bom_edge_key("ASM1", "P1")] and not boom:
            boom.append(1)
            raise RuntimeError("db went away")
        return orig(run_id, entity, keys, status, *a)

    monkeypatch.setattr(engine, "_mark_stmt", flaky)
    out = run_all(env)
    assert out["status"] == "done_with_failures" and fake.bom_calls.count(("POST", ("ASM1", "P1"))) == 1  # POSTed, but the DB failed between the 200 and the commit
    assert ("ASM1", "P1") not in stored_edges() and ("ASM1", "P2") in stored_edges()  # no ledger row without `done`
    assert {i.key: i.error_code for i in items("bom_edge") if i.status == "failed"} == {bom_edge_key("ASM1", "P1"): "internal"}
    resume(env, out["run_id"])
    assert finish(env, out["run_id"])["status"] == "done" and fake.bom_calls.count(("POST", ("ASM1", "P1"))) == 2 and ("ASM1", "P1") in stored_edges()
    assert all(h for _, h in stored_edges().values())


def test_customers_resume_by_key_and_idempotent_retry(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 1)
    rows = [*ROWS, *[("Customer", f"K{i}", {"ListID": f"K{i}", "FullName": f"SENTINEL-k{i}"}) for i in range(1, 6)]]
    tried: set[str] = set()

    def on_customer(n, body):
        if n == 2:
            engine.cancel(next(iter(engine._CANCELS)))
        if body["code"] == "K5" and "K5" not in tried:  # one transient failure, then fine
            tried.add("K5")
            return httpx.Response(503)
        return None

    fake = Fake10(on_customer=on_customer)
    env = mk(clean_db, tmp_path, fake, rows=rows)
    retries(env, 3)
    out = finish(env, go(env, ("customers",)).json()["run_id"])
    assert out["status"] == "cancelled" and 0 < out["counts"]["customer"]["done"] < 5
    resume(env, out["run_id"])
    out = finish(env, out["run_id"])
    assert out["status"] == "done" and out["counts"] == {"customer": {"done": 5}}
    assert dict(fake.put_codes) == {f"K{i}": 1 for i in range(1, 6)}  # nothing done before the cancel was re-sent; the 503 on K5 was retried (idempotent), not failed
    assert sum(1 for m, p in fake.log if (m, p) == ("PUT", "/api/Customers")) == 6  # 5 + the retried K5


def test_fatal_error_in_the_bom_step_aborts_the_later_steps(clean_db, tmp_path):
    fake = Fake10(on_bom=lambda n, m, p: httpx.Response(401) if n == 2 else None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    out = run_all(env)
    assert out["status"] == "failed" and out["summary"]["fatal"] == "auth"
    assert out["counts"]["customer"] == {"skipped": 2} and out["error_codes"]["customer"] == {"aborted": 2} and fake.put_codes == Counter()
    assert out["counts"]["product"] == {"done": 8}


def test_cancel_and_resume_across_the_bom_and_customer_steps(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    fake = Fake10(on_bom=lambda n, m, p: engine.cancel(next(iter(engine._CANCELS))) if n == 3 else None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    out = run_all(env)
    assert out["status"] == "cancelled" and out["counts"]["customer"] == {"pending": 2} and 0 < out["counts"]["bom_edge"]["done"] < 5
    resume(env, out["run_id"])
    out = finish(env, out["run_id"])
    assert out["status"] == "done" and out["counts"] == {"supplier": {"done": 3}, "product": {"done": 8}, "bom_edge": {"done": 5}, "customer": {"done": 2}}
    assert Counter(p for _, p in fake.bom_calls) == Counter(dict.fromkeys(EDGES, 1)) and set(fake.put_codes.values()) == {1}  # each edge and customer sent exactly once


def test_products_only_and_bom_only_runs_on_an_existing_target(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env, ("suppliers", "products"))["status"] == "done"
    assert run_all(env, ("bom",))["counts"] == {"bom_edge": {"done": 5}}  # the products are `done` for the target through an earlier run
    assert run_all(env, ("customers",))["counts"] == {"customer": {"done": 2}}
    n = len(fake.log)
    for body in ({}, {"dry_run": False, "preflight_fingerprint": "x", "plan_fingerprint": "y"}):  # the dry-run and the real run: same 409, BEFORE any StockTrim read
        r = post(env, steps=["products"], mode="initial", **body)
        assert (r.status_code, r.json()["detail"]) == (409, "step_already_pushed") and not engine._ACTIVE.locked()  # master data re-push = the incremental sync (mode auto/sync)
    assert len(fake.log) == n


def test_sync_mode_gate_foreign_records_still_block(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert not engine._ledger("x")  # no completed run on a target: strict mode
    assert run_all(env)["status"] == "done"
    assert dry(env, ("bom",))["verdict"]["ok_to_push"]  # exactly our records
    fake.customers["zz-probe-cust"] = {"code": "zz-probe-cust"}  # a ZZ-PROBE record is ours
    assert dry(env, ("bom",))["verdict"]["ok_to_push"]
    fake.customers["foreign"] = {"code": "FOREIGN"}
    v = dry(env, ("bom",))["verdict"]
    assert not v["ok_to_push"] and v["blocking"] == [{"code": "stocktrim_not_empty", "count": 1, "detail": {"customers": 1}}]
    del fake.customers["foreign"]
    fake.existing = 1  # a foreign product
    assert not dry(env, ("bom",))["verdict"]["ok_to_push"]
    fake.existing = 0
    fake.suppliers["FOREIGN"] = {}
    assert not dry(env, ("bom",))["verdict"]["ok_to_push"]
    del fake.suppliers["FOREIGN"]
    assert dry(env, ("bom",))["verdict"]["ok_to_push"]


def test_strict_mode_unchanged_for_a_first_run_on_a_used_account(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake10(existing=1), rows=ROWS10)
    assert not dry(env, ("bom",))["verdict"]["ok_to_push"]  # no completed run of ours: any record blocks


def test_plan_fingerprint_covers_the_new_steps(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake10(), rows=ROWS10)
    base = {s: dry(env, (s,))["plan_fingerprint"] for s in ("products", "bom", "customers")}
    set_line("A2", "I5", "9")
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == "C1")).one()
        rec.payload = {**rec.payload, "Phone": "SENTINEL-other"}
    new = {s: dry(env, (s,))["plan_fingerprint"] for s in base}
    assert new["products"] == base["products"] and new["bom"] != base["bom"] and new["customers"] != base["customers"]
    assert plan_fingerprint(engine.build_plan(env.app.state.settings).plan, ["bom", "customers"])


def test_new_steps_leak_nothing_and_codes_stay_a_closed_set(clean_db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = Fake10(on_product=lambda n, b: httpx.Response(400, json={"title": "x"}) if b["productId"] == "P4" else None,
                  on_customer=lambda n, b: httpx.Response(400, json={"errors": {"Name": ["SENTINEL-bad-name"]}}) if b["code"] == "C2" else None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    d = dry(env, ALL)
    run_id = go(env, ALL).json()["run_id"]
    out = finish(env, run_id)
    everything = json.dumps([out, d, [(i.key, i.ref, i.status, i.error_code, i.detail) for i in items()]]) + caplog.text
    assert "SENTINEL" not in everything and "example.com" not in everything and "555-" not in everything
    codes = {i.error_code for i in items() if i.error_code}
    assert {"dependency_failed", "rejected_400"} <= codes <= engine.ERROR_CODES
    assert {s["ref"] for s in out["samples"]} <= {"V1", "V2", "V3", "I1", "I2", "I3", "I4", "I5", "I6", "A1", "A2", "C1", "C2"}
    assert out["status"] == "done_with_failures" and out["counts"]["customer"] == {"done": 1, "failed": 1}


def test_endpoint_accepts_the_new_steps_only(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake10(), rows=ROWS10)
    assert post(env, steps=["bom", "customers"]).status_code == 200 and post(env, steps=["sales_orders"]).status_code == 422
    assert env.client.get("/api/v1/push/runs/00000000-0000-0000-0000-000000000000", headers=HEADERS).status_code == 404


def test_progress_eta_counts_bom_and_customer_calls(clean_db, tmp_path):
    release = threading.Event()
    fake = Fake10(on_customer=lambda n, b: release.wait(10) and None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    run_id = go(env, ("bom", "customers")).json()["run_id"]
    try:
        out = env.get(f"/api/v1/push/runs/{run_id}").json()
        assert out["status"] == "running" and out["eta_seconds"] is not None
    finally:
        release.set()
    assert finish(env, run_id)["status"] == "done_with_failures"  # BOM edges: products are not pushed in this account yet -> dependency_failed
    assert {i.error_code for i in items("bom_edge")} == {"dependency_failed"}


def refused(env: Env, steps) -> dict:
    """The verdict of a dry-run (must still answer 200)."""
    return dry(env, steps)["verdict"]


def test_purchase_orders_block_the_initial_load_but_only_warn_in_sync_mode(clean_db, tmp_path):
    fake = Fake10()
    fake.pos = ["PO"]
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    v = refused(env, ("bom",))  # initial mode: the account must be empty
    assert not v["ok_to_push"] and v["blocking"][0]["code"] == "stocktrim_not_empty" and v["blocking"][0]["detail"] == {"purchase_orders": 1}
    fake.pos = []
    assert run_all(env)["status"] == "done" and refused(env, ("bom",))["ok_to_push"]
    fake.pos = ["PO"]  # the client's first PO must not stop every sync
    v = refused(env, ("bom",))
    assert v["ok_to_push"] and v["blocking"] == [] and {"code": "purchase_orders_present", "count": 1} in v["warnings"]


def test_sync_gate_done_with_failures_and_dry_runs_do_not_establish_it(clean_db, tmp_path):
    fake = Fake10(on_product=lambda n, b: httpx.Response(400, json={"title": "x"}) if b["productId"] == "P4" else None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    target = stocktrim_target(env.app.state.settings)
    refused(env, ("products",))
    refused(env, ("products",))
    assert engine._ledger(target) is None  # dry-runs never establish sync mode, however many
    assert run_all(env, ("suppliers", "products"))["status"] == "done_with_failures"
    assert engine._ledger(target) is None  # neither does a run with failures
    v = refused(env, ("bom",))
    assert not v["ok_to_push"] and v["blocking"][0]["code"] == "stocktrim_not_empty"  # strict: the account holds our own half-done load


def test_sync_gate_blank_customer_code_is_foreign(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    for i, bad in enumerate(({"name": "no code"}, {"code": ""}, {"code": "   "}), 1):
        fake.customers[f"blank{i}"] = bad
        v = refused(env, ("bom",))
        assert not v["ok_to_push"] and v["blocking"] == [{"code": "stocktrim_not_empty", "count": i, "detail": {"customers": i}}]


def test_probe_exemption_applies_to_a_test_target_only(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    env.app.state.settings.stocktrim_environment = "production"
    assert run_all(env)["status"] == "done" and refused(env, ("bom",))["ok_to_push"]
    fake.customers["zz-probe-cust"] = {"code": "zz-probe-cust"}  # exempt on TEST only: on a production target it is a foreign record
    v = refused(env, ("bom",))
    assert not v["ok_to_push"] and v["blocking"][0]["detail"] == {"customers": 1}
    assert _foreign("customers", ["zz-probe-cust"], set(), True) == {"foreign": 0} and _foreign("products", ["zz-probe-cust"], set(), True) == {"foreign": 1}  # only the listed entity


def test_bom_rounding_below_the_fifth_decimal_is_no_diff_and_a_zero_rounding_edge_is_deleted(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    set_line("A2", "I4", "0.123459")  # 0.12346 either way
    d = dry(env, ("bom",))["plan"]
    assert d["bom_to_post"] == 0 and d["bom_to_delete"] == 0
    set_line("A2", "I4", "0.000001")  # rounds to 0: the edge is dropped from the plan, so the stored one is deleted
    d = dry(env, ("bom",))["plan"]
    assert d["bom_to_post"] == 0 and d["bom_to_delete"] == 1
    fake.bom_calls.clear()
    assert finish(env, go(env, ("bom",)).json()["run_id"])["counts"] == {"bom_edge": {"done": 1}}
    assert fake.bom_calls == [("DELETE", ("ASM2", "P4"))] and ("ASM2", "P4") not in stored_edges()


def test_a_start_while_a_run_is_active_is_a_running_conflict_for_bom_and_customers(clean_db, tmp_path):
    release = threading.Event()
    fake = Fake10(on_customer=lambda n, b: release.wait(10) and None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    d = dry(env, ("bom", "customers"))
    body = {"dry_run": False, "preflight_fingerprint": d["preflight_fingerprint"], "plan_fingerprint": d["plan_fingerprint"]}
    run_id = post(env, steps=["customers"], **body).json()["run_id"]
    try:
        for steps in (["bom"], ["customers"], ["bom", "customers"]):
            r = post(env, steps=steps, **body)
            assert (r.status_code, r.json()["detail"]) == (409, "push_running")
    finally:
        release.set()
    finish(env, run_id)


def test_delete_500_fails_the_edge_and_keeps_its_ledger_row(clean_db, tmp_path):
    fake = Fake10(on_bom=lambda n, m, p: httpx.Response(500) if m == "DELETE" and p == ("ASM1", "P2") else None)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    set_line("A1", "I2", None)
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] in ("failed", "done_with_failures") and out["counts"] == {"bom_edge": {"failed": 1}}
    assert ("ASM1", "P2") in stored_edges() and ("ASM1", "P2") in fake.edges  # nothing was deleted anywhere: retried by the next run


def test_a_sibling_failure_leaves_the_parent_hash_empty_and_a_clean_resume_restamps_it(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    assert all(stored_edges()[k][1] for k in (("ASM2", "P4"), ("ASM2", "P5")))
    set_line("A2", "I4", "0.5")
    set_line("A2", "I5", "4")  # both edges of ASM2 change; the P4 POST is rejected, the sibling's goes through
    fake.on_bom = lambda n, m, p: httpx.Response(400, json={"title": "x"}) if m == "POST" and p == ("ASM2", "P4") else None
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] == "done_with_failures" and out["counts"]["bom_edge"] == {"done": 1, "failed": 1}
    assert stored_edges()[("ASM2", "P5")] == (Decimal(4), "") and stored_edges()[("ASM2", "P4")][1] == ""  # old hash invalidated, not re-stamped
    assert all(stored_edges()[k][1] for k in (("ASM1", "P1"), ("ASM1", "P2")))  # other parents untouched
    fake.on_bom = None
    resume(env, out["run_id"])
    assert finish(env, out["run_id"])["status"] == "done" and stored_edges()[("ASM2", "P4")][1] == stored_edges()[("ASM2", "P5")][1] != ""


def test_plain_delete_404_is_counted_in_the_summary(clean_db, tmp_path):
    def on_bom(n, method, pair):
        if method == "DELETE" and pair == ("ASM1", "P2"):
            fake.edges.pop(pair)
            return httpx.Response(404)
        return None

    fake = Fake10(on_bom=on_bom)
    env = mk(clean_db, tmp_path, fake, rows=ROWS10)
    assert run_all(env)["status"] == "done"
    set_line("A1", "I2", None)
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] == "done" and out["summary"]["bom_delete_404"] == 1
