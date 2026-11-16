"""Offline tests for app.domain (synthetic records only)."""
import time
import unicodedata
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domain.bom import fingerprint, plan_bom
from app.domain.from_qb import _why, load_snapshot, parse_customer, parse_product, parse_supplier
from app.domain.keys import resolve_customers, resolve_products, resolve_suppliers
from app.domain.models import BomEdge, Product, Quarantined, QuarantineResult, Supplier
from app.models import QBRecord


def prod(sku, lid=None, kind="inventory", **kw):
    return Product(sku=sku, list_id=lid or f"L-{sku}", kind=kind, name=sku, description=sku, **kw)


def codes(items):
    return sorted(i.code for i in items)


# ---------------------------------------------------------------- parsers
def test_parse_product_numbers_and_description():
    p = parse_product({"ListID": "1", "Name": "A&#0174; 1", "FullName": "G:A", "SalesDesc": "Mail&#0174; &#153;", "PurchaseDesc": "x",
                       "QuantityOnHand": "-3", "QuantityOnOrder": "", "ReorderPoint": "2.50", "BuildPoint": "9", "Max": "10",
                       "PurchaseCost": "1.10", "SalesPrice": "2", "IsActive": "false", "ManufacturerPartNumber": "",
                       "PrefVendorRef": {"ListID": "V1", "FullName": "Acme"}}, "inventory")
    assert p.sku == "A&#0174; 1"  # key untouched
    assert p.description == "Mail® ™"
    assert p.quantity_on_hand == Decimal("-3") and p.quantity_on_order is None and p.manufacturer_part_number is None
    assert p.reorder_point == Decimal("2.50") and p.cost == Decimal("1.10") and p.is_active is False
    assert (p.preferred_vendor_code, p.preferred_vendor_list_id) == (None, "V1")  # the code is set only by resolve_products


def test_parse_product_description_fallbacks_and_assembly_buildpoint():
    assert parse_product({"ListID": "1", "Name": "N", "PurchaseDesc": "pd"}, "inventory").description == "pd"
    assert parse_product({"ListID": "1", "Name": "N", "SalesDesc": ""}, "inventory").description == "N"
    a = parse_product({"ListID": "1", "Name": "N", "ReorderPoint": "1", "BuildPoint": "7", "PrefVendorRef": ""}, "assembly")
    assert a.reorder_point == Decimal("7") and a.preferred_vendor_code is None and a.is_active is True


def test_parse_product_bad_number_raises_with_field_not_value():
    with pytest.raises(ValueError) as e:
        parse_product({"ListID": "1", "Name": "N", "Max": "SECRETVAL"}, "inventory")
    assert _why(e.value) == "NumberError: Max" and "SECRETVAL" not in str(e.value)
    with pytest.raises(ValueError) as e2:  # pydantic path: only the loc is reported
        parse_product({"ListID": "1", "Name": 99999}, "inventory")
    assert _why(e2.value) == "ValidationError: sku" and "99999" not in _why(e2.value)


def test_parse_supplier_customer_shapes():
    s = parse_supplier({"ListID": "V", "Name": "Acme", "CompanyName": "", "Email": "a@b.c", "Contact": "Jo", "Phone": "555",
                        "VendorAddress": [{"Addr1": "x", "Addr2": "", "City": "C"}]})
    assert s.code == "Acme" and s.name == "Acme" and s.address.lines == ["x"] and s.address.city == "C"
    assert (s.email, s.contact, s.phone) == ("a@b.c", "Jo", "555")
    assert parse_supplier({"ListID": "V", "Name": "A", "VendorAddress": ["", {"Addr1": "y"}]}).address.lines == ["y"]
    assert parse_supplier({"ListID": "V", "Name": "A", "VendorAddress": [""]}).address is None
    s2 = parse_supplier({"ListID": "V", "Name": "Acme", "CompanyName": "Acme Inc", "VendorAddress": ""})
    assert s2.name == "Acme Inc" and s2.address is None
    c = parse_customer({"ListID": "C1", "Name": "n", "FullName": "Full", "BillAddress": {"Addr1": "a", "Country": "US"}, "IsActive": "true",
                       "Email": "c@d.e", "Phone": "123"})
    assert (c.code, c.name, c.address.country, c.email, c.phone) == ("C1", "Full", "US", "c@d.e", "123")


# ---------------------------------------------------------------- keys
def test_key_rules_quarantine():
    ps = [prod(k, lid=f"id{i}") for i, k in enumerate([" lead", "trail ", "\ttab", "mid\x99dle", "", "   ", "ok"])]
    r = resolve_products(ps)
    assert [p.sku for p in r.ok] == ["ok"] and len(r.quarantined) == 6
    by = {q.record.list_id: codes(q.issues) for q in r.quarantined}
    assert "whitespace_key" in by["id0"] and "whitespace_key" in by["id1"]
    assert "control_chars" in by["id2"] and "control_chars" in by["id3"]
    assert by["id4"] == ["empty_key"] and by["id5"] == ["empty_key"]


def test_collisions_case_and_trailing_space_quarantine_all():
    r = resolve_products([prod("X", "1"), prod("x", "2"), prod("X ", "3"), prod("Y", "4")])
    assert [p.sku for p in r.ok] == ["Y"]
    assert len(r.quarantined) == 3 and all("duplicate_key" in codes(q.issues) for q in r.quarantined)
    msg = next(i.message for q in r.quarantined for i in q.issues if i.code == "duplicate_key")
    assert "'X'" in msg and "'x'" in msg and "'X '" in msg


@pytest.mark.parametrize("a,b", [
    ("Straße", "STRASSE"), (unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")), ("Ａ", "A"), ("X", "x"), ("X", "X "),
])
def test_collision_normalisation(a, b):
    assert a != b
    r = resolve_products([prod(a, "1"), prod(b, "2"), prod("other", "3")])
    assert [p.sku for p in r.ok] == ["other"] and len(r.quarantined) == 2
    assert sorted(q.record.sku for q in r.quarantined) == sorted([a, b])  # keys untouched


def test_leading_space_is_not_a_collision_but_is_quarantined():
    r = resolve_products([prod(" X", "1"), prod("X", "2")])
    assert [p.sku for p in r.ok] == ["X"] and codes(r.quarantined[0].issues) == ["whitespace_key"]


def test_warnings_keep_key_exactly():
    r = resolve_products([prod("A  B", "1"), prod("Arrow™", "2"), prod("k" * 201, "3"), prod("fine", "4")])
    assert [p.sku for p in r.ok] == ["A  B", "Arrow™", "k" * 201, "fine"] and not r.quarantined
    assert codes(r.warnings) == ["internal_double_space", "key_too_long", "non_ascii_key"]


def test_cross_kind_uniqueness():
    r = resolve_products([prod("S1", "1", "inventory"), prod("s1", "2", "assembly")])
    assert not r.ok and len(r.quarantined) == 2


def test_empty_and_duplicate_list_ids_quarantined():
    r = resolve_products([prod("A", "1").model_copy(update={"list_id": ""}), prod("B", "X"), prod("C", "X", "assembly"), prod("D", "4")])
    assert [p.sku for p in r.ok] == ["D"] and len(r.quarantined) == 3
    by = {q.record.sku: codes(q.issues) for q in r.quarantined}
    assert by["A"] == ["empty_list_id"] and by["B"] == by["C"] == ["duplicate_key"]
    s = resolve_suppliers([Supplier(code="V", list_id="", name="V")])
    assert not s.ok and codes(s.quarantined[0].issues) == ["empty_list_id"]


def test_vendor_and_item_keys_are_separate_namespaces():
    sup = resolve_suppliers([parse_supplier({"ListID": "V1", "Name": "Same"})])
    r = resolve_products([prod("Same", "1", preferred_vendor_list_id="V1")], sup)
    assert len(sup.ok) == 1 and not r.quarantined and r.ok[0].preferred_vendor_code == "Same"


def test_inactive_supplier_not_pushable_and_vendor_link_dropped():
    sup = resolve_suppliers([parse_supplier({"ListID": "V1", "Name": "Acme"}), parse_supplier({"ListID": "V2", "Name": "Bolt", "IsActive": "false"})])
    assert [s.code for s in sup.ok] == ["Acme"] and [s.code for s in sup.inactive] == ["Bolt"] and not sup.quarantined
    assert codes(sup.warnings) == ["inactive_supplier"]
    r = resolve_products([prod("a", "1", preferred_vendor_list_id="V1"), prod("b", "2", preferred_vendor_list_id="V2")], sup)
    assert [p.preferred_vendor_code for p in r.ok] == ["Acme", None] and codes(r.warnings) == ["unresolved_vendor"]


def test_supplier_rules_and_vendor_resolution():
    sup = resolve_suppliers([parse_supplier({"ListID": "V1", "Name": "Acme"}), parse_supplier({"ListID": "V2", "Name": "acme"}),
                             parse_supplier({"ListID": "V3", "Name": "Bolt"})])
    assert [s.code for s in sup.ok] == ["Bolt"] and len(sup.quarantined) == 2
    ps = [prod("a", "1", preferred_vendor_list_id="V3"),
          prod("b", "2", preferred_vendor_list_id="V1"), prod("c", "3")]
    r = resolve_products(ps, sup)
    assert [p.preferred_vendor_code for p in r.ok] == ["Bolt", None, None]
    assert codes(r.warnings) == ["unresolved_vendor"]


def test_customer_rules():
    cs = [parse_customer({"ListID": "1", "Name": "Same"}), parse_customer({"ListID": "2", "Name": "same"}), parse_customer({"ListID": "", "Name": "z"})]
    r = resolve_customers(cs)
    assert [c.list_id for c in r.ok] == ["1", "2"] and len(r.quarantined) == 1
    assert codes(r.warnings) == ["duplicate_name"] and "Same" not in r.warnings[0].message


def test_inactive_customer_not_pushable():
    r = resolve_customers([parse_customer({"ListID": "1", "Name": "A"}), parse_customer({"ListID": "2", "Name": "B", "IsActive": "false"})])
    assert [c.list_id for c in r.ok] == ["1"] and [c.list_id for c in r.inactive] == ["2"] and not r.quarantined
    assert codes(r.warnings) == ["inactive_customer"] and "B" not in r.warnings[0].message


# ---------------------------------------------------------------- BOM
def line(lid, qty):
    return {"ItemInventoryRef": {"ListID": lid, "FullName": "n"}, "Quantity": qty}


def asm(sku, lines, kind="assembly"):
    return {"ListID": f"L-{sku}", "Name": sku, "ItemInventoryAssemblyLine": lines}


def run(skus_assemblies, raws=()):
    """skus_assemblies: {parent: [(component, qty)]}; raws: inventory skus."""
    ps = [prod(s, kind="assembly") for s in skus_assemblies] + [prod(s) for s in raws]
    res = QuarantineResult[Product](ok=ps, quarantined=[])
    asms = [asm(p, [line(f"L-{c}", q) for c, q in comps]) for p, comps in skus_assemblies.items()]
    return plan_bom(res, asms)


def test_sum_duplicates_and_shapes():
    ps = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("R"), prod("S")])
    a1 = {"ListID": "L-P", "ItemInventoryAssemblyLine": [line("L-R", "1"), "", line("L-R", "2.0"), line("L-S", "1"), None, {"x": 1}]}
    a2 = {"ListID": "L-P", "ItemInventoryAssemblyLine": line("L-S", "3")}  # single dict
    a3 = {"ListID": "L-P", "ItemInventoryAssemblyLine": ""}
    plan = plan_bom(ps, [a1])
    assert {e.component_sku: e.quantity for e in plan.edges_by_parent["P"]} == {"R": Decimal("3.0"), "S": Decimal("1")}
    assert plan.stats["summed_duplicates"] == 1 and plan.stats["excluded_lines"] == 1  # {"x":1} has no ref -> unresolved
    assert plan.stats["assemblies_with_summed_duplicates"] == 1 and plan.stats["assemblies_without_lines"] == 0
    assert plan_bom(ps, [a2]).stats["edges"] == 1
    p3 = plan_bom(ps, [a3])
    assert p3.stats["assemblies"] == 1 and p3.stats["assemblies_with_edges"] == 0


def test_unresolved_and_nonpositive_and_self_reference():
    ps = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("R")])
    plan = plan_bom(ps, [{"ListID": "L-P", "ItemInventoryAssemblyLine": [line("SERVICE", "5"), line("L-R", "0"), line("L-R", "-1"), line("L-R", "x"),
                                                                        line("L-P", "1"), line("L-R", "4")]}])
    assert codes(plan.issues) == ["bom_component_unresolved", "bom_self_reference", "nonpositive_quantity", "nonpositive_quantity", "nonpositive_quantity"]
    assert [(e.component_sku, e.quantity) for e in plan.edges_by_parent["P"]] == [("R", Decimal("4"))]
    assert plan.stats["excluded_lines"] == 1 and plan.stats["dropped_edges"] == 4  # self-reference is checked before quantity
    assert {i.code: i.severity for i in plan.issues}["bom_self_reference"] == "warning"


def test_garbage_quantity_forms():
    ps = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("A"), prod("B"), prod("C")])
    plan = plan_bom(ps, [{"ListID": "L-P", "ItemInventoryAssemblyLine": [line("L-A", None), line("L-B", "NaN"), line("L-C", "1E+3")]}])
    assert [(e.component_sku, e.quantity) for e in plan.edges_by_parent["P"]] == [("C", Decimal(1000))]
    assert codes(plan.issues) == ["nonpositive_quantity"] * 2 and plan.stats["dropped_edges"] == 2


def test_unusable_records_as_component_and_parent():
    res = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("R")])
    asms = [{"ListID": "L-P", "ItemInventoryAssemblyLine": [line("L-C", "1"), line("L-R", "1")]},
            {"ListID": "L-Q", "ItemInventoryAssemblyLine": [line("L-R", "1"), line("L-R", "2")]}]
    plan = plan_bom(res, asms, unusable_list_ids={"L-C", "L-Q"})
    assert codes(plan.issues) == ["bom_component_excluded"] * 2 and plan.stats["excluded_lines"] == 3
    assert plan.stats["assemblies_all_lines_excluded"] == 1 and list(plan.edges_by_parent) == ["P"]


def test_empty_list_id_never_matches_in_bom():
    res = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("Z").model_copy(update={"list_id": ""})])
    plan = plan_bom(res, [{"ListID": "L-P", "ItemInventoryAssemblyLine": [line("", "1")]}])
    assert codes(plan.issues) == ["bom_component_unresolved"] and plan.stats["excluded_lines"] == 1 and plan.stats["assemblies_all_lines_excluded"] == 1


def test_subassembly_without_bom_counted_and_not_a_level():
    plan = run({"P": [("S", "1")], "S": [("X", "1")]})  # S's only line is unresolved -> S has no pushed BOM
    assert plan.order == ["P"] and plan.stats["max_depth"] == 1 and plan.stats["depth_histogram"] == {1: 1}
    assert plan.stats["subassemblies_without_bom"] == 1 and plan.stats["assemblies_all_lines_excluded"] == 1


def test_quarantined_component_and_parent_excluded():
    bad = prod("BAD", "L-BAD")
    res = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("R")])
    res.quarantined.append(Quarantined(record=bad, issues=[]))
    plan = plan_bom(res, [{"ListID": "L-P", "ItemInventoryAssemblyLine": [line("L-BAD", "1"), line("L-R", "1")]},
                          {"ListID": "L-BAD", "ItemInventoryAssemblyLine": [line("L-R", "1")]}])
    assert list(plan.edges_by_parent) == ["P"] and codes(plan.issues) == ["bom_component_excluded"] * 2
    assert plan.stats["excluded_lines"] == 2


@pytest.mark.parametrize("cycle", [["A", "B"], ["A", "B", "C"]])
def test_cycle_quarantines_all_members_and_ancestors(cycle):
    asms = {c: [(cycle[(i + 1) % len(cycle)], "1"), ("R", "1")] for i, c in enumerate(cycle)}
    asms["TOP"] = [(cycle[0], "1")]
    plan = run(asms, raws=["R"])
    assert plan.quarantined_parents == sorted([*cycle, "TOP"]) and not plan.edges_by_parent and plan.stats["cycle_ancestors"] == 1
    assert codes(plan.issues) == sorted(["bom_cycle"] * len(cycle) + ["bom_cycle_ancestor"])
    assert {i.ref for i in plan.issues if i.code == "bom_cycle"} == {f"L-{c}" for c in cycle}  # ListID refs
    anc = next(i for i in plan.issues if i.code == "bom_cycle_ancestor")
    assert anc.ref == "L-TOP" and all(repr(c) in anc.message for c in cycle)


def test_ancestor_chain_withheld_descendants_and_unrelated_kept():
    plan = run({"A": [("B", "1"), ("D", "1")], "B": [("A", "1")], "D": [("R", "1")], "T": [("A", "1")], "U": [("T", "1")], "V": [("D", "1")]}, raws=["R"])
    assert plan.quarantined_parents == ["A", "B", "T", "U"] and plan.stats["cycle_ancestors"] == 2
    assert plan.order == ["D", "V"] and [e.component_sku for e in plan.edges_by_parent["V"]] == ["D"]
    assert plan.stats["max_depth"] == 2 and plan.stats["depth_histogram"] == {1: 1, 2: 1}


def test_multiple_cycles_share_ancestors():
    plan = run({"A": [("B", "1")], "B": [("A", "1")], "C": [("D", "1")], "D": [("E", "1")], "E": [("C", "1")],
                "X": [("A", "1"), ("C", "1")], "Y": [("B", "1")], "Z": [("R", "1")]}, raws=["R"])
    assert plan.quarantined_parents == ["A", "B", "C", "D", "E", "X", "Y"] and plan.order == ["Z"] and plan.stats["cycle_ancestors"] == 2
    x = next(i for i in plan.issues if i.code == "bom_cycle_ancestor" and i.ref == "L-X")
    assert all(repr(m) in x.message for m in "ABCDE")


def test_cycle_with_tail_keeps_tail_below():
    plan = run({"A": [("B", "1")], "B": [("C", "1")], "C": [("D", "1")], "D": [("B", "1"), ("S", "1")], "S": [("R", "1")]}, raws=["R"])
    assert plan.quarantined_parents == ["A", "B", "C", "D"] and plan.order == ["S"] and plan.stats["cycle_ancestors"] == 1


def test_self_loop_alone_is_a_warning_and_with_cycle():
    plan = run({"A": [("A", "1"), ("B", "1")], "B": [("A", "1")], "S": [("S", "1"), ("R", "1")]}, raws=["R"])
    assert plan.quarantined_parents == ["A", "B"] and plan.order == ["S"] and plan.stats["dropped_edges"] == 2
    assert codes(plan.issues) == ["bom_cycle", "bom_cycle", "bom_self_reference", "bom_self_reference"]
    assert {i.severity for i in plan.issues if i.code == "bom_self_reference"} == {"warning"}


def test_shared_subassembly_and_bottom_up_order():
    plan = run({"T1": [("S", "2")], "T2": [("S", "1"), ("R", "1")], "S": [("M", "1")], "M": [("R", "1")]}, raws=["R"])
    assert plan.order.index("M") < plan.order.index("S") < plan.order.index("T1") and plan.order.index("S") < plan.order.index("T2")
    assert plan.stats["max_depth"] == 3 and plan.stats["edges"] == 5 and plan.stats["quarantined_parents"] == 0
    assert plan.stats["depth_histogram"] == {1: 1, 2: 1, 3: 2} and plan.stats["subassemblies_without_bom"] == 0


def test_fingerprint_stable():
    e = lambda c, q: BomEdge(parent_sku="P", component_sku=c, quantity=Decimal(q))  # noqa: E731
    assert fingerprint([e("A", "2"), e("B", "1")]) == fingerprint([e("B", "1.0"), e("A", "2.0")]) == fingerprint([e("A", "2.00"), e("B", "1")])
    assert fingerprint([e("A", "2")]) != fingerprint([e("A", "3")])
    assert fingerprint([e("A", "20")]) != fingerprint([e("A", "2")])


def test_large_graph_fast_and_deep_chain_no_recursion():
    start = time.perf_counter()
    plan = run({f"A{i}": [(f"A{i + 1}", "1"), ("R", "1")] for i in range(2000)} | {"A2000": [("R", "1")]}, raws=["R"])
    assert time.perf_counter() - start < 2 and plan.stats["max_depth"] == 2001
    deep = run({f"D{i}": [(f"D{i + 1}", "1")] for i in range(3000)} | {"D3000": [("R", "1")]}, raws=["R"])
    assert deep.stats["max_depth"] == 3001 and deep.order[0] == "D3000" and not deep.quarantined_parents
    cyc = run({f"C{i}": [(f"C{(i + 1) % 3000}", "1")] for i in range(3000)})  # one 3000-node cycle: iterative Tarjan
    assert len(cyc.quarantined_parents) == 3000


# ---------------------------------------------------------------- loader
def test_load_snapshot_reads_only_master_entities(clean_db):
    engine = create_engine(clean_db)
    rows = [("Customer", "C1", {"ListID": "C1", "Name": "c"}), ("Vendor", "V1", {"ListID": "V1", "Name": "v"}),
            ("ItemInventory", "I1", {"ListID": "I1", "Name": "i", "QuantityOnHand": "-2"}),
            ("ItemInventoryAssembly", "A1", {"ListID": "A1", "Name": "a", "ItemInventoryAssemblyLine": ""}),
            ("ItemInventory", "I2", {"ListID": "I2", "Name": "bad", "Max": "zz"}),
            ("SalesOrder", "S1", {"TxnID": "S1"})]
    with Session(engine) as s:
        s.add_all(QBRecord(entity=e, qb_id=i, payload=p) for e, i, p in rows)
        s.commit()
        snap = load_snapshot(s)
    engine.dispose()
    assert [c.list_id for c in snap.customers] == ["C1"] and [x.code for x in snap.suppliers] == ["v"]
    assert sorted(p.sku for p in snap.products) == ["a", "i"] and [p.quantity_on_hand for p in snap.products if p.sku == "i"] == [Decimal("-2")]
    assert len(snap.assemblies) == 1 and codes(snap.issues) == ["parse_error"] and snap.issues[0].ref == "I2"
    assert snap.issues[0].message == "NumberError: Max" and "zz" not in snap.issues[0].message


def test_quantity_precision_over_six_places_dropped():
    ps = QuarantineResult[Product](ok=[prod("P", kind="assembly"), prod("A"), prod("B"), prod("C")])
    lines = [line("L-A", "0.0000001"), line("L-B", "0.1234560"), line("L-C", "0.0000004"), line("L-C", "0.0000004")]  # B has 7 digits but equals 6; C sums to 8e-7
    plan = plan_bom(ps, [{"ListID": "L-P", "ItemInventoryAssemblyLine": lines}])
    assert [(e.component_sku, e.quantity) for e in plan.edges_by_parent["P"]] == [("B", Decimal("0.1234560"))]
    assert codes(plan.issues) == ["bom_quantity_precision"] * 2 and plan.stats["dropped_edges"] == 2
    assert all(i.severity == "warning" for i in plan.issues)
