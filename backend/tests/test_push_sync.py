"""Offline tests of the incremental sync (stage 11): synthetic data only, StockTrim is an in-memory httpx MockTransport, nothing really sleeps."""
import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.db import session_scope
from app.models import PushedItem, PushRun, QBRecord
from app.sync import engine

from .conftest import admin_engine, alembic_cfg, create_database
from .test_push import finish, go, items, mk, post, resume
from .test_push_stage10 import ALL, ROWS10, Fake10, dry, run_all, set_line, stored_edges

ROWS = [*ROWS10, ("Vendor", "V9", {"ListID": "V9", "Name": "SUPZ", "CompanyName": "SENTINEL-co-z"})]  # SUPZ: no product links to it


def set_field(list_id: str, **fields: Any) -> None:
    with session_scope() as s:
        rec = s.scalars(select(QBRecord).where(QBRecord.qb_id == list_id)).one()
        rec.payload = {**rec.payload, **fields}


def ledger(entity: str) -> dict[str, tuple]:
    with session_scope() as s:
        return {r.key: (r.ref, r.payload_hash, r.last_run_id) for r in s.scalars(select(PushedItem).where(PushedItem.entity == entity))}


def loaded(clean_db, tmp_path, **kw: Any):
    """Env after a complete initial load of all four steps."""
    fake = Fake10(**kw)
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    out = run_all(env)
    assert out["status"] == "done" and out["summary"]["mode"] == "initial"
    return env, fake


def sync(env, steps=ALL) -> dict:
    return finish(env, go(env, steps).json()["run_id"])


def n_writes(fake) -> int:
    return len(fake.writes)


# ---- migration -----------------------------------------------------------------------------------

def test_migration_0005_backfills_done_items_with_null_hash_and_downgrades(db_url):
    name = f"qb_scratch_{uuid.uuid4().hex[:8]}"
    create_database(db_url, name)
    url = create_engine(db_url).url.set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    real, dry_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        command.upgrade(alembic_cfg(url), "0004")
        with eng.begin() as c:
            for rid, target, opts in ((real, "T1", '{"dry_run": false}'), (dry_id, "T1", '{"dry_run": true}'), (other, "T2", '{"dry_run": false}')):
                c.execute(text("INSERT INTO push_run (id, target, started_at, status, options) VALUES (:i, :t, now(), 'done', CAST(:o AS jsonb))"), {"i": rid, "t": target, "o": opts})
            for rid, ent, key, st in ((real, "supplier", "S1", "done"), (real, "product", "P1", "done"), (real, "product", "P2", "failed"), (real, "customer", "C1", "done"),
                                      (real, "bom_edge", "E1", "done"), (dry_id, "product", "PD", "done"), (other, "product", "P1", "done")):
                c.execute(text("INSERT INTO push_item (run_id, entity, key, status, attempts, ref, updated_at) VALUES (:r, :e, :k, :s, 1, 'L-' || :k, now())"), {"r": rid, "e": ent, "k": key, "s": st})
        command.upgrade(alembic_cfg(url), "head")
        with eng.connect() as c:
            rows = set(c.execute(text("SELECT target, entity, key, ref, payload_hash, last_run_id FROM pushed_item")).all())
        assert rows == {("T1", "supplier", "S1", "L-S1", None, real), ("T1", "product", "P1", "L-P1", None, real), ("T1", "customer", "C1", "L-C1", None, real),
                        ("T2", "product", "P1", "L-P1", None, other)}  # done + non-dry-run + ledger entities only; BOM keeps bom_edge; NULL hash = re-sent once
        with pytest.raises(Exception), eng.begin() as c:  # the entity CHECK
            c.execute(text("INSERT INTO pushed_item (target, entity, key, pushed_at) VALUES ('T', 'bom_edge', 'k', now())"))
        command.downgrade(alembic_cfg(url), "0004")
        assert "pushed_item" not in inspect(eng).get_table_names() and "push_item" in inspect(eng).get_table_names()
        command.upgrade(alembic_cfg(url), "head")
    finally:
        eng.dispose()
        with admin_engine(db_url).connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


# ---- modes ---------------------------------------------------------------------------------------

def test_mode_auto_resolution_and_step_already_pushed_only_in_initial_mode(clean_db, tmp_path):
    fake = Fake10()
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    assert dry(env)["mode"] == "initial" and "sync" not in dry(env)
    assert post(env, steps=["products"], mode="sync").json()["detail"] == "no_completed_initial_run"  # a diff needs a baseline
    run_all(env)
    assert dry(env)["mode"] == "sync"
    r = post(env, steps=["products"], mode="initial")
    assert (r.status_code, r.json()["detail"]) == (409, "step_already_pushed")
    for mode in ("auto", "sync"):
        for steps in (["suppliers"], ["products"], ["suppliers", "products", "bom", "customers"]):
            assert post(env, steps=steps, mode=mode).status_code == 200
    assert post(env, steps=["products"], mode="bogus").status_code == 422


def test_unchanged_sync_makes_zero_calls_for_all_four_steps_and_stays_idempotent(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    before, log = n_writes(fake), len(fake.log)
    d = dry(env, ALL)
    assert d["mode"] == "sync" and d["planned_calls"] == 0 and d["eta_seconds"] == 0 and d["plan"]["bom_to_post"] == 0 and d["plan"]["bom_to_delete"] == 0
    assert d["sync"]["suppliers"]["unchanged"] == 4 and d["sync"]["products"]["unchanged"] == 8 and d["sync"]["customers"]["unchanged"] == 2
    assert all(v["new"] == v["changed"] == v["requeued"] == v["vanished"] == 0 for k, v in d["sync"].items() if k != "bom")
    for _ in range(2):  # a second one is idempotent too
        out = sync(env)
        assert out["status"] == "done" and out["counts"] == {} and n_writes(fake) == before
        assert out["summary"]["mode"] == "sync" and out["summary"]["sync"]["products"]["unchanged"] == 8
    assert [m for m, _ in fake.log[log:]] and all(m == "GET" for m, _ in fake.log[log:])  # only the pre-flight reads


def test_null_hash_rows_are_re_sent_once(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    with session_scope() as s:
        s.query(PushedItem).update({"payload_hash": None})  # what the migration backfill leaves
    d = dry(env, ("suppliers", "products", "customers"))
    assert (d["sync"]["suppliers"]["changed"], d["sync"]["products"]["changed"], d["sync"]["customers"]["changed"]) == (4, 8, 2)
    out = sync(env, ("suppliers", "products", "customers"))
    assert out["counts"] == {"supplier": {"done": 4}, "product": {"done": 8}, "customer": {"done": 2}}
    assert all(h and len(h) == 64 for _, h, _ in ledger("product").values())
    assert sync(env, ("suppliers", "products", "customers"))["counts"] == {}


def test_a_single_changed_product_supplier_and_customer_pushes_exactly_that_item(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    old = ledger("product")["P4"][1]
    set_field("I4", QuantityOnHand="42")
    set_field("V2", Email="new@example.com")
    set_field("C1", Phone="SENTINEL-new-phone")
    fake.posts.clear()
    fake.put_codes.clear()
    d = dry(env, ALL)
    assert (d["sync"]["products"]["changed"], d["sync"]["suppliers"]["changed"], d["sync"]["customers"]["changed"]) == (1, 1, 1)
    assert all(v["new"] == 0 for k, v in d["sync"].items() if k != "bom") and d["planned_calls"] == 3
    out = sync(env)
    assert out["counts"] == {"supplier": {"done": 1}, "product": {"done": 1}, "customer": {"done": 1}} and out["status"] == "done"
    assert fake.posts == {"P4": 1} and dict(fake.put_codes) == {"C1": 1} and fake.products["P4"]["stockOnHand"] == 42 and fake.suppliers["SUPB"]["emailAddress"] == "new@example.com"
    assert ledger("product")["P4"][1] != old and ledger("product")["P4"][2] == uuid.UUID(out["run_id"])
    assert {i.key for i in items() if str(i.run_id) == out["run_id"]} == {"SUPB", "P4", "C1"}
    set_field("I4", QuantityOnHand="1")  # back to the original value: exactly that one reverts
    assert sync(env)["counts"] == {"product": {"done": 1}}


def test_a_new_record_is_pushed_and_a_changed_bom_quantity_is_one_edge(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    with Session(create_engine(clean_db)) as s:
        s.add(QBRecord(entity="Customer", qb_id="C9", payload={"ListID": "C9", "FullName": "SENTINEL-c9"}))
        s.commit()
    set_line("A2", "I5", "4")
    d = dry(env, ALL)
    assert d["sync"]["customers"]["new"] == 1 and d["plan"]["bom_to_post"] == 1 and d["planned_calls"] == 2
    fake.bom_calls.clear()
    out = sync(env)
    assert out["counts"] == {"customer": {"done": 1}, "bom_edge": {"done": 1}} and fake.bom_calls == [("POST", ("ASM2", "P5"))]


# ---- atomicity -----------------------------------------------------------------------------------

def test_pushed_item_is_upserted_in_the_transaction_that_marks_done(clean_db, tmp_path, monkeypatch):
    env, fake = loaded(clean_db, tmp_path)
    old = ledger("product")["P1"]
    set_field("I1", QuantityOnHand="77")
    orig, boom = engine._mark_stmt, []

    def flaky(run_id, entity, keys, status, *a):  # fails INSIDE the transaction that also upserts the ledger row
        if entity == "product" and status == "done" and keys == ["P1"] and not boom:
            boom.append(1)
            raise RuntimeError("db went away")
        return orig(run_id, entity, keys, status, *a)

    monkeypatch.setattr(engine, "_mark_stmt", flaky)
    fake.posts.clear()
    out = sync(env, ("products",))
    assert fake.posts["P1"] == 1 and out["status"] == "done_with_failures"  # the POST happened, the DB write did not
    assert ledger("product")["P1"] == old  # ledger unchanged
    assert {i.key: (i.status, i.error_code) for i in items("product") if str(i.run_id) == out["run_id"]} == {"P1": ("failed", "internal")}
    resume(env, out["run_id"])
    assert finish(env, out["run_id"])["status"] == "done" and fake.posts["P1"] == 2  # the resume re-POSTs exactly that item
    assert ledger("product")["P1"][1] != old[1]


def test_dropped_supplier_link_leaves_a_null_hash_so_the_next_sync_repairs_it(clean_db, tmp_path):
    fake = Fake10(bad_suppliers={"SUPA"})
    env = mk(clean_db, tmp_path, fake, rows=ROWS)
    assert run_all(env, ("suppliers", "products"))["status"] == "done_with_failures"
    assert ledger("product")["P1"][1] is None and ledger("product")["P4"][1] is not None  # P1 linked to the failed supplier: sent without the link
    assert "SUPA" not in ledger("supplier")


# ---- vanished / requeue --------------------------------------------------------------------------

def test_vanished_keys_are_reported_not_deleted(clean_db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    env, fake = loaded(clean_db, tmp_path)
    with session_scope() as s:
        for lid in ("V9", "I6", "C2"):
            s.delete(s.scalars(select(QBRecord).where(QBRecord.qb_id == lid)).one())
    d = dry(env, ALL)
    assert [d["sync"][k]["vanished"] for k in ("suppliers", "products", "customers")] == [1, 1, 1]
    before, products, suppliers, customers = n_writes(fake), dict(fake.products), dict(fake.suppliers), dict(fake.customers)
    out = sync(env)
    assert out["counts"] == {} and n_writes(fake) == before and fake.products == products and fake.suppliers == suppliers and fake.customers == customers  # no deletion, no call
    sy = out["summary"]["sync"]
    assert sy["suppliers"]["vanished_list_ids"] == ["V9"] and sy["products"]["vanished_list_ids"] == ["I6"] and sy["customers"]["vanished_list_ids"] == ["C2"]
    assert {"SUPZ"} <= set(ledger("supplier")) and "P6" in ledger("product") and "C2" in ledger("customer")  # still in the ledger
    assert "SENTINEL" not in json.dumps([out, d]) + caplog.text


def test_requeue_after_a_wipe_repushes_the_products_customers_and_their_bom_edges(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    for k in ("P1", "P5"):  # P1 is a component of ASM1, P5 of ASM2: the wipe took the products (and, in StockTrim, their edges)
        del fake.products[k]
        for pair in [p for p in fake.edges if k in p]:
            del fake.edges[pair]
    del fake.customers["c2"]
    d = dry(env, ALL)
    assert (d["sync"]["products"]["requeued"], d["sync"]["customers"]["requeued"], d["sync"]["suppliers"]["requeued"]) == (2, 1, 0)
    assert d["sync"]["products"]["changed"] == 0 and d["plan"]["bom_to_post"] == 2 and d["plan"]["bom_to_delete"] == 0 and d["verdict"]["ok_to_push"]
    assert len(stored_edges()) == 5  # the dry-run touched nothing
    fake.posts.clear()
    fake.bom_calls.clear()
    fake.put_codes.clear()
    out = sync(env)
    assert out["status"] == "done" and out["counts"] == {"product": {"done": 2}, "bom_edge": {"done": 2}, "customer": {"done": 1}}
    assert dict(fake.posts) == {"P1": 1, "P5": 1} and sorted(fake.bom_calls) == [("POST", ("ASM1", "P1")), ("POST", ("ASM2", "P5"))] and dict(fake.put_codes) == {"C2": 1}
    assert out["summary"]["sync"]["products"]["requeued"] == 2
    assert len(stored_edges()) == 5 and all(h for _, h in stored_edges().values())  # edges and parent hashes restored
    assert sync(env)["counts"] == {}  # converged


def test_a_requeued_product_that_fails_stays_out_of_the_ledger_and_its_edges_wait(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    del fake.products["P1"]
    fake.on_product = lambda n, b: httpx.Response(400, json={"title": "x"}) if b["productId"] == "P1" else None
    out = sync(env, ("products", "bom"))
    assert out["status"] == "done_with_failures" and "P1" not in ledger("product")
    assert {i.error_code for i in items("bom_edge") if str(i.run_id) == out["run_id"]} == {"dependency_failed"}


# ---- gate: acknowledge_foreign -------------------------------------------------------------------

def test_acknowledge_foreign_flow(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    fake.existing = 2  # two foreign products
    d = dry(env, ("bom",))
    fh = d["verdict"]["foreign_hash"]
    assert not d["verdict"]["ok_to_push"] and fh and len(fh) == 64 and d["verdict"]["blocking"][0]["detail"] == {"products": 2}
    assert dry(env, ("bom",))["verdict"]["foreign_hash"] == fh  # stable
    r = post(env, steps=["bom"], dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
    assert (r.status_code, r.json()["detail"]) == (409, "preflight_not_ok")  # blocked without the acknowledgement
    assert post(env, steps=["bom"], acknowledge_foreign="0" * 64).json()["detail"] == "acknowledge_foreign_mismatch"  # wrong hash refused
    ok = post(env, steps=["bom"], acknowledge_foreign=fh).json()
    assert ok["verdict"]["ok_to_push"] and ok["verdict"]["foreign_hash"] == fh
    r = post(env, steps=["bom"], dry_run=False, preflight_fingerprint=ok["preflight_fingerprint"], plan_fingerprint=ok["plan_fingerprint"], acknowledge_foreign=fh)
    assert r.status_code == 200 and finish(env, r.json()["run_id"])["status"] == "done"
    fake.existing = 3  # the foreign set changed: the old hash no longer applies
    assert post(env, steps=["bom"], acknowledge_foreign=fh).json()["detail"] == "acknowledge_foreign_mismatch"
    fake.existing = 2
    fake.pos = ["PO"]  # purchase orders still block, acknowledged or not
    v = post(env, steps=["bom"], acknowledge_foreign=fh).json()["verdict"]
    assert not v["ok_to_push"] and v["blocking"][0]["detail"] == {"purchase_orders": 1}
    fake.pos = []
    fake.existing = 0
    assert "foreign_hash" not in dry(env, ("bom",))["verdict"]  # none foreign: omitted
    assert post(env, steps=["bom"], acknowledge_foreign=fh).json()["detail"] == "acknowledge_foreign_mismatch"  # nothing foreign: nothing to acknowledge


# ---- fingerprint, leakage, cancel/resume ---------------------------------------------------------

def test_sync_plan_fingerprint_covers_the_diff_set(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    fp = lambda: dry(env, ALL)["plan_fingerprint"]  # noqa: E731
    base = fp()
    assert fp() == base
    set_field("I4", QuantityOnHand="42")
    one = fp()
    assert one != base
    set_field("I4", QuantityOnHand="43")  # the same key, another hash
    assert fp() != one
    d = dry(env, ALL)
    set_field("I4", QuantityOnHand="44")  # the plan moved after the dry-run: the real run is refused by its thread
    r = post(env, steps=list(ALL), dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
    out = finish(env, r.json()["run_id"])
    assert out["status"] == "failed" and out["summary"]["error"] == "plan_fingerprint_mismatch" and fake.posts["P4"] == 1


def test_sync_summary_samples_and_logs_leak_nothing(clean_db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    env, fake = loaded(clean_db, tmp_path)
    set_field("I4", QuantityOnHand="42", SalesDesc="SENTINEL-new-desc")
    set_field("C1", Phone="SENTINEL-555-0199", Email="SENTINEL-9@example.com")
    set_field("V2", CompanyName="SENTINEL-co-changed")
    d = dry(env, ALL)
    out = sync(env)
    everything = json.dumps([d, out, [(i.key, i.ref, i.status, i.error_code, i.detail) for i in items()]]) + caplog.text
    assert "SENTINEL" not in everything and "example.com" not in everything and "555-" not in everything
    assert out["summary"]["sync"]["products"]["changed"] == 1 and {s["ref"] for s in out["samples"]} <= {"I1", "I2", "I3", "I4", "I5", "I6", "V1", "V2", "V3", "A1", "A2", "C1", "C2"}


def test_cancel_and_resume_in_sync_mode_send_each_changed_key_once(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CHUNK", 2)
    env, fake = loaded(clean_db, tmp_path)
    for lid in ("I1", "I2", "I4", "I5", "I6"):
        set_field(lid, QuantityOnHand="31")
    fake.posts.clear()
    fake.n = 0
    fake.on_product = lambda n, b: engine.cancel(next(iter(engine._CANCELS))) if n == 3 else None
    run_id = go(env, ("products",)).json()["run_id"]
    first = finish(env, run_id)
    assert first["status"] == "cancelled" and first["counts"]["product"].get("pending", 0) > 0 and first["summary"]["mode"] == "sync"
    assert 0 < len(ledger("product")) and sum(1 for k, v in fake.products.items() if v["stockOnHand"] == 31) < 5
    fake.on_product = None
    resume(env, run_id)
    out = finish(env, run_id)
    assert out["status"] == "done" and out["counts"] == {"product": {"done": 5}} and set(fake.posts.values()) == {1} and set(fake.posts) == {"P1", "P2", "P4", "P5", "P6"}
    assert out["summary"]["sync"]["products"]["changed"] == 5  # what the first start reported survives the resume


# ---- stamp_only ----------------------------------------------------------------------------------

def test_stamp_only_adopts_the_current_state_without_calling_stocktrim(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    with session_scope() as s:
        s.query(PushedItem).update({"payload_hash": None})  # the state right after migration 0005
    steps = ["suppliers", "products", "customers"]
    set_field("I4", QuantityOnHand="42")  # one record that really changed
    d = post(env, steps=steps, stamp_only=True).json()
    assert d["mode"] == "stamp" and d["dry_run"] and d["stamped"] == {"suppliers": 4, "products": 8, "customers": 2} and d["verdict"]["ok_to_push"]
    assert post(env, steps=steps, stamp_only=True, dry_run=False, preflight_fingerprint=d["preflight_fingerprint"]).status_code == 422  # plan fingerprint required
    assert post(env, steps=steps, stamp_only=True, dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint="0" * 64).json()["detail"] == "plan_fingerprint_mismatch"
    assert post(env, steps=steps, stamp_only=True, dry_run=False, preflight_fingerprint="0" * 64, plan_fingerprint=d["plan_fingerprint"]).json()["detail"] == "preflight_fingerprint_mismatch"
    env.app.state.settings.stocktrim_writes_enabled = False
    assert post(env, steps=steps, stamp_only=True, dry_run=False, preflight_fingerprint="x", plan_fingerprint="y").status_code == 403
    env.app.state.settings.stocktrim_writes_enabled = True
    before = n_writes(fake)
    r = post(env, steps=steps, stamp_only=True, dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
    assert r.status_code == 200 and r.json()["stamped"] == d["stamped"] and n_writes(fake) == before  # no StockTrim write
    assert all(h for _, h, _ in ledger("product").values())
    run = env.get(f"/api/v1/push/runs/{r.json()['run_id']}").json()
    assert run["status"] == "done" and run["summary"]["stamped"] == d["stamped"] and not engine._ACTIVE.locked()
    with session_scope() as s:
        assert s.scalars(select(PushRun).where(PushRun.id == uuid.UUID(r.json()["run_id"]))).one().options["mode"] == "stamp"
    # the baseline is the CURRENT plan, so the record changed in QuickBooks is also adopted (documented: stamp only what you know equals StockTrim)
    assert dry(env, ALL)["sync"]["products"]["changed"] == 0
    assert post(env, steps=steps, stamp_only=True).json()["stamped"] == {"suppliers": 0, "products": 0, "customers": 0}  # only NULL rows are stamped


def test_stamp_keeps_real_hashes_and_refuses_when_a_ledger_key_is_missing_or_not_sync_mode(clean_db, tmp_path):
    env, fake = loaded(clean_db, tmp_path)
    with session_scope() as s:
        s.query(PushedItem).filter(PushedItem.key != "P4").update({"payload_hash": None})
    set_field("I4", QuantityOnHand="42")  # P4 has a real hash that differs: NOT adopted, still pushed by the next sync
    d = post(env, steps=["products"], stamp_only=True).json()
    assert d["stamped"] == {"products": 7}
    post(env, steps=["products"], stamp_only=True, dry_run=False, preflight_fingerprint=d["preflight_fingerprint"], plan_fingerprint=d["plan_fingerprint"])
    assert dry(env, ("products",))["sync"]["products"]["changed"] == 1
    del fake.products["P1"]
    assert post(env, steps=["products"], stamp_only=True).json()["detail"] == "stamp_requires_all_present"
    assert post(env, steps=["bom"], stamp_only=True).status_code == 422
    with session_scope() as s:  # no baseline run for this target any more
        s.execute(text("TRUNCATE push_run RESTART IDENTITY CASCADE"))
    assert post(env, steps=["products"], stamp_only=True).json()["detail"] == "stamp_needs_sync_mode" and not engine._ACTIVE.locked()
