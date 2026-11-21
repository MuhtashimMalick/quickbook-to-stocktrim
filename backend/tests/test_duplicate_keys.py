"""Duplicate product keys: a group of items that collide on the key is decided by its ACTIVE count (synthetic data only)."""
import random
from collections import Counter

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.domain.bom import plan_bom
from app.domain.keys import resolve_customers, resolve_products, resolve_suppliers
from app.domain.models import Customer, Product, Supplier
from app.models import PushExclusion, QBRecord
from app.reasons import CATALOG
from app.sync import engine

from .test_preflight import Fake, make, run
from .test_push import mk
from .test_push_orders import RECENT, Fake13, po, po_line, so_line
from .test_push_stage10 import dry


def prod(sku, lid, active=True, kind="inventory"):
    return Product(sku=sku, list_id=lid, kind=kind, description=sku, is_active=active)


def split(res):
    return {"ok": sorted(p.list_id for p in res.ok), "aside": sorted(p.list_id for p in res.inactive), "quar": sorted(q.record.list_id for q in res.quarantined)}


@pytest.mark.parametrize("recs,want", [
    ([("A-1", "1", True), ("A-1", "2", False)], {"ok": ["1"], "aside": ["2"], "quar": []}),
    ([("A-1", "1", False), ("A-1", "2", False)], {"ok": [], "aside": ["1", "2"], "quar": []}),
    ([("A-1", "1", True), ("A-1", "2", True)], {"ok": [], "aside": [], "quar": ["1", "2"]}),
    ([("A-1", "1", True), ("A-1", "2", False), ("A-1", "3", False)], {"ok": ["1"], "aside": ["2", "3"], "quar": []}),
    ([("A-1", "1", True), ("A-1", "2", True), ("A-1", "3", False)], {"ok": [], "aside": [], "quar": ["1", "2", "3"]}),
    ([("Straße", "1", True), ("STRASSE", "2", False), ("Ａ", "3", True), ("a", "4", False), ("A", "5", False)], {"ok": ["1", "3"], "aside": ["2", "4", "5"], "quar": []}),
    ([("B-2", "1", True), ("C-3", "2", False)], {"ok": ["1", "2"], "aside": [], "quar": []}),  # no collision: an inactive product is still a product
])
def test_collision_groups_follow_the_active_count(recs, want):
    for _ in range(5):  # order independent
        random.shuffle(recs)
        assert split(resolve_products([prod(k, lid, a) for k, lid, a in recs])) == want


def test_set_aside_twin_is_a_warning_and_cross_kind_twins_collide():
    r = resolve_products([prod("S-1", "1", True, "assembly"), prod("s-1", "2", False, "inventory")])
    assert [p.list_id for p in r.ok] == ["1"] and [p.list_id for p in r.inactive] == ["2"] and not r.quarantined
    assert [(w.code, w.severity, w.ref) for w in r.warnings] == [("duplicate_key", "warning", "2")]


def test_other_key_problems_and_shared_list_ids_are_not_resolved_by_activity():
    r = resolve_products([prod("X ", "1", True), prod("x", "2", False), prod("Y", "9", True), prod("Z", "9", False)])
    assert {q.record.sku for q in r.quarantined} == {"X ", "Y", "Z"} and [p.list_id for p in r.inactive] == ["2"] and not r.ok  # "X " collides (trailing space is ignored), its twin is set aside
    r = resolve_products([prod(" X", "1", True), prod(" x", "2", False)])
    assert [q.record.list_id for q in r.quarantined] == ["1", "2"]  # whitespace keys stay quarantined


def test_suppliers_and_customers_inactive_twin_never_quarantines_the_active_one():
    s = resolve_suppliers([Supplier(code="V-1", list_id="1", name="a"), Supplier(code="v-1", list_id="2", name="a", is_active=False)])
    assert [x.list_id for x in s.ok] == ["1"] and [x.list_id for x in s.inactive] == ["2"] and not s.quarantined
    s2 = resolve_suppliers([Supplier(code="V-1", list_id="1", name="a"), Supplier(code="v-1", list_id="2", name="a")])
    assert not s2.ok and len(s2.quarantined) == 2  # two ACTIVE vendors: a real conflict
    c = resolve_customers([Customer(code="1", list_id="1", name="n"), Customer(code="2", list_id="2", name="n", is_active=False)])
    assert [x.list_id for x in c.ok] == ["1"] and not c.quarantined


def test_bom_edge_to_a_set_aside_twin_is_excluded_with_the_existing_warning():
    res = resolve_products([prod("PARENT", "P", kind="assembly"), prod("C-1", "C1"), prod("c-1", "C2", False)])

    def line(lid):
        return {"ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": lid}, "Quantity": "2"}]}
    plan = plan_bom(res, [{"ListID": "P", **line("C2")}])
    assert [i.code for i in plan.issues] == ["bom_component_excluded"] and not plan.edges_by_parent
    assert not plan_bom(res, [{"ListID": "P", **line("C1")}]).issues  # the kept twin is a normal component


def test_catalog_has_the_new_finding_and_the_duplicate_key_text_covers_both_cases():
    assert CATALOG["duplicate_key_set_aside"]["next_step"] == "No action needed."
    assert "rename" in CATALOG["duplicate_key"]["next_step"].lower() and "inactive" in CATALOG["duplicate_key"]["explanation"]


def item(lid, name, active=True):
    return "ItemInventory", lid, {"ListID": lid, "Name": name, "IsActive": "true" if active else "false"}


SO = {"TxnID": "T1", "TxnDate": RECENT, "CustomerRef": {"ListID": "C1"}, "IsManuallyClosed": "false", "SalesOrderLineRet": [so_line("1", "O1", "3"), so_line("2", "N1", "1")]}
PO = po("PO1", "V1", [po_line("1", "O1", "3", "0")])
# AAA-1: active N1 + inactive twin O1 (case-only); BBB-2: no active item; ASM-1 uses the twin O1 and ZZZ-9
BASE = [item("N1", "AAA-1"), item("O1", "aaa-1", False), item("N2", "BBB-2", False), item("O2", "BBB-2", False), item("N3", "CCC-3"), item("N4", "ZZZ-9"),
        ("ItemInventoryAssembly", "AS", {"ListID": "AS", "Name": "ASM-1", "IsActive": "true", "ItemInventoryAssemblyLine": [
            {"ItemInventoryRef": {"ListID": "O1"}, "Quantity": "1"}, {"ItemInventoryRef": {"ListID": "N4"}, "Quantity": "1"}]}),
        ("Customer", "C1", {"ListID": "C1", "Name": "cust"}), ("Vendor", "V1", {"ListID": "V1", "Name": "vend"}),
        ("SalesOrder", "T1", SO), ("PurchaseOrder", "PO1", PO)]


def test_report_plan_and_order_lines_for_set_aside_twins(clean_db, tmp_path):
    env = make(clean_db, tmp_path, BASE, Fake())
    q = run(env)
    ents, v = q["quickbooks"]["entities"]["products"], q["verdict"]
    assert (ents["ok"], ents["quarantined"], ents["inactive"]) == (4, 0, 3)  # read 7, ready 4 (incl. the assembly), set aside 3 (RECORDS)
    assert v["ok_to_push"] is True and not v["blocking"]
    assert {"code": "duplicate_key_set_aside", "count": 3} in v["warnings"] and not any(w["code"] == "qb_products_quarantined" for w in v["warnings"])
    built = engine.build_plan(env.settings, ("sales_orders", "purchase_orders"))
    assert [k for k, _ in built.plan.products] == ["AAA-1", "ASM-1", "CCC-3", "ZZZ-9"]
    assert {(x.entity, x.key, x.code) for x in built.exclusions if x.code == "duplicate_key"} == {("product", "aaa-1", "duplicate_key"), ("product", "BBB-2", "duplicate_key")}
    assert built.bom.stats["excluded_lines"] == 1 and [i.code for i in built.bom.issues] == ["bom_component_excluded"]
    so = built.plan.order_stats["sales_orders"]
    assert [k for k, _ in built.plan.sales_orders] == ["T1:2"] and so["non_product"] == 1  # the line on the twin is skipped, NOT remapped to the active item
    assert built.plan.order_stats["purchase_orders"]["no_lines"] == 1


def test_push_exclusion_rows_are_written_with_the_duplicate_key_code(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake13(), rows=BASE)
    d = dry(env, tuple(engine.STEPS))
    with session_scope() as s:
        got = Counter(s.execute(select(PushExclusion.entity, PushExclusion.key, PushExclusion.code).where(PushExclusion.run_id == d["run_id"], PushExclusion.code == "duplicate_key")).tuples().all())
    assert got == Counter({("product", "aaa-1", "duplicate_key"): 1, ("product", "BBB-2", "duplicate_key"): 1})  # same-key twins collapse into one row


def test_two_active_twins_block_and_the_fingerprint_is_stable_and_order_independent(clean_db, tmp_path):
    env = make(clean_db, tmp_path, [item("A", "AAA-1"), item("B", "aaa-1"), item("C", "AAA-1", False), item("D", "OK-1")], Fake())
    r1 = run(env)
    assert r1["verdict"]["ok_to_push"] is False and {"code": "qb_products_quarantined", "count": 3} in r1["verdict"]["blocking"]
    assert r1["quickbooks"]["entities"]["products"]["inactive"] == 0 and r1["fingerprint"] == run(env)["fingerprint"]
    rows = [item("A", "AAA-1"), item("C", "AAA-1", False), item("D", "OK-1"), item("E", "EEE", False), item("F", "eee", False)]
    fps = set()
    for _ in range(3):
        random.shuffle(rows)
        eng = create_engine(clean_db)
        with Session(eng) as s:
            s.query(QBRecord).delete()
            s.add_all(QBRecord(entity=e, qb_id=i, payload=p) for e, i, p in rows)
            s.commit()
        eng.dispose()
        fps.add(run(env)["fingerprint"])
    assert len(fps) == 1


def test_sync_plan_when_twins_swap_or_a_third_active_twin_appears(clean_db, tmp_path):
    env = make(clean_db, tmp_path, [item("N", "AAA-1"), item("O", "AAA-1", False), item("K", "KEEP")], Fake())

    def keys():
        return engine.build_plan(env.settings).list_ids

    def edit(fn):
        eng = create_engine(clean_db)
        with Session(eng) as s:
            fn(s)
            s.commit()
        eng.dispose()

    def flip(lid, active):
        def f(s):
            r = s.scalars(select(QBRecord).where(QBRecord.qb_id == lid)).one()
            r.payload = {**r.payload, "IsActive": active}
        return f
    assert keys() == {"AAA-1": "N", "KEEP": "K"}
    edit(flip("N", "false"))  # all inactive: nothing is sent for the key any more
    assert keys() == {"KEEP": "K"}
    edit(flip("O", "true"))  # the obsolete twin is now the only active one: the key is back, with another ListID
    assert keys() == {"AAA-1": "O", "KEEP": "K"}
    edit(lambda s: s.add(QBRecord(entity="ItemInventory", qb_id="X", payload={"ListID": "X", "Name": "aaa-1", "IsActive": "true"})))  # a second active item: conflict, the key disappears
    assert keys() == {"KEEP": "K"}
