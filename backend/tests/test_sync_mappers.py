import json
from decimal import Decimal

import pytest

from app.domain.bom import BomPlan, plan_bom
from app.domain.keys import resolve_customers, resolve_products, resolve_suppliers
from app.domain.models import Address, BomEdge, Customer, Product, QuarantineResult, Supplier
from app.stocktrim.dto import BomRequest
from app.sync.mappers import (
    bom_edge_key,
    build_push_plan,
    first_email,
    to_bom_request,
    to_customer_request,
    to_product_request,
    to_supplier_request,
)


def prod(sku="SKU-1", **kw):
    base = dict(sku=sku, list_id=f"L-{sku}", kind="inventory", description="A thing")
    return Product(**{**base, **kw})


def sup(code="Acme Co", **kw):
    return Supplier(**{**dict(code=code, list_id=f"V-{code}", name=code), **kw})


@pytest.mark.parametrize("raw,want", [
    ("a@x.com", "a@x.com"), ("a@x.com; b@y.org", "a@x.com"), ("a@x.com, b@y.org", "a@x.com"), ("  a@x.com  ;b@y.org", "a@x.com"),
    ("not-an-email; b@y.org", "b@y.org"), ("nope", None), ("a@b", None), ("", None), (None, None), (";,", None), ("a b@x.com", None),
    ("mailto:a@b.co", "a@b.co"), ("MAILTO:a@b.co", "a@b.co"), ("Pat <a@b.co>", "a@b.co"), ("a..b@x.com", None), ("a..b@x.com; ok@x.com", "ok@x.com"),
    ("a@x.com;", "a@x.com"), (";a@x.com,", "a@x.com"), ("  ;  ,  ", None), ("   ", None), ("ä@x.de", "ä@x.de"),  # internationalised: accepted
])
def test_first_email(raw, want):
    assert first_email(raw) == want


# ---- suppliers ----------------------------------------------------------------------------------

def test_supplier_fields_and_address_dropping():
    s = sup("Vend  Name ", name="Vend Co", email="x@y.com;z@y.com", contact="Contact Q", list_id="L-SUP-X",
            address=Address(lines=["VEND CO", "vend  name ", "1 Main St", "Unit 2", "Dock 3", "Back"], city="Town", state="TX", postal_code="75001", country="USA"))
    r = to_supplier_request(s).payload()
    assert r == {"supplierCode": "Vend  Name ", "supplierName": "Vend Co", "emailAddress": "x@y.com", "primaryContactName": "Contact Q", "externalId": "L-SUP-X",
                 "streetAddress": "1 Main St", "addressLine1": "Unit 2", "addressLine2": "Dock 3, Back", "state": "TX", "postCode": "75001", "country": "USA"}
    assert "defaultLeadTime" not in r and "city" not in r  # no lead time (A9); SupplierDto has no city


def test_supplier_minimal_sends_no_nulls():
    assert to_supplier_request(sup("S1")).payload() == {"supplierCode": "S1", "supplierName": "S1", "externalId": "V-S1"}


# ---- products -----------------------------------------------------------------------------------

def test_product_full_mapping_and_json_numbers():
    p = prod("SKU-T1", full_name="Grp:Sub:SKU-T1", description="Desc", cost=Decimal("1.25"), price=Decimal("9.99"), quantity_on_hand=Decimal("7.5"),
             quantity_on_order=Decimal("4"), reorder_point=Decimal("3"), max_stock=Decimal("10"), preferred_vendor_code="Acme",
             manufacturer_part_number="MPN1", is_active=False)
    req, adjusted = to_product_request(p)
    assert not adjusted
    raw = json.dumps(req.payload())
    body = json.loads(raw)  # the serialised payload
    assert body == {"productId": "SKU-T1", "productCodeReadable": "SKU-T1", "stockOnHand": 7.5, "name": "Desc", "category": "Grp", "cost": 1.25,
                    "price": 9.99, "supplierCode": "Acme", "suppliers": [{"supplierId": "Acme", "supplierSkuCode": "MPN1"}],
                    "minimumShelfLevel": 3.0, "maximumShelfLevel": 10.0, "discontinued": True}
    assert "stockOnOrder" not in raw
    for k in ("stockOnHand", "cost", "price", "minimumShelfLevel", "maximumShelfLevel"):
        assert type(body[k]) in (int, float)  # JSON numbers, not strings


def test_product_always_sends_stock_and_readable_even_without_data():
    body = to_product_request(prod("A B  "))[0].payload()  # exact name, double/trailing space kept
    assert body["productId"] == body["productCodeReadable"] == "A B  " and body["stockOnHand"] == 0
    assert "category" not in body and "supplierCode" not in body and "suppliers" not in body and "maximumShelfLevel" not in body
    assert body["discontinued"] is False


def test_product_max_only_when_positive_and_supplier_without_mpn():
    assert "maximumShelfLevel" not in to_product_request(prod(max_stock=Decimal(0)))[0].payload()
    body = to_product_request(prod(preferred_vendor_code="V", max_stock=Decimal(5)))[0].payload()
    assert body["maximumShelfLevel"] == 5 and body["suppliers"] == [{"supplierId": "V"}]
    assert "supplierCode" not in to_product_request(prod(preferred_vendor_list_id="x"))[0].payload()  # unresolved vendor: no link


@pytest.mark.parametrize("policy,sent,adjusted", [("as_is", -3.0, False), ("clamp", 0.0, True)])
def test_negative_stock_policy(policy, sent, adjusted):
    req, flag = to_product_request(prod(quantity_on_hand=Decimal("-3")), policy)
    assert (req.payload()["stockOnHand"], flag) == (sent, adjusted)


def test_clamp_leaves_non_negative_alone():
    req, flag = to_product_request(prod(quantity_on_hand=Decimal("2")), "clamp")
    assert req.payload()["stockOnHand"] == 2 and not flag


def test_assembly_reorder_point_is_the_domain_value():
    # parse_product puts BuildPoint into reorder_point for assemblies; the mapper just maps it
    assert to_product_request(prod(kind="assembly", reorder_point=Decimal("12")))[0].payload()["minimumShelfLevel"] == 12


# ---- customers / bom ----------------------------------------------------------------------------

def test_customer_mapping():
    c = Customer(code="C-9", list_id="C-9", name="Test Customer", email="bad; tc@x.com", phone="555",
                 address=Address(lines=["test customer", "9 Elm", "Apt 1"], city="C", state="S", postal_code="P", country="K"))
    assert to_customer_request(c).payload() == {"code": "C-9", "name": "Test Customer", "emailAddress": "tc@x.com", "phone": "555", "streetAddress": "9 Elm",
                                                "addressLine1": "Apt 1", "city": "C", "state": "S", "postCode": "P", "country": "K"}


def test_customer_without_address_or_email():
    assert to_customer_request(Customer(code="c", list_id="c", name="N")).payload() == {"code": "c", "name": "N"}


def test_bom_request_uses_skus_and_number_quantity():
    r = to_bom_request(BomEdge(parent_sku="P", component_sku="C", quantity=Decimal("0.25")))
    assert isinstance(r, BomRequest) and r.payload() == {"productId": "P", "componentId": "C", "quantity": 0.25}


# ---- plan ---------------------------------------------------------------------------------------

def _asm(lid, comps):
    return {"ListID": lid, "ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": c}, "Quantity": str(q)} for c, q in comps]}


def test_push_plan_order_counts_and_exclusions():
    suppliers = resolve_suppliers([sup("S1"), sup("S2", is_active=False)])
    products = resolve_products([
        prod("TOP", kind="assembly", quantity_on_hand=Decimal(-1), preferred_vendor_list_id="V-S1"),
        prod("MID", kind="assembly"), prod("RAW"), prod("BAD "),  # trailing space: quarantined
        prod("RAW2", quantity_on_hand=Decimal(-5)),
    ], suppliers)
    customers = resolve_customers([Customer(code="c1", list_id="c1", name="N"), Customer(code="c2", list_id="c2", name="M")])
    bom = plan_bom(products, [_asm("L-TOP", [("L-MID", 2), ("L-RAW", 1)]), _asm("L-MID", [("L-RAW2", 3)])])
    plan = build_push_plan(suppliers, products, customers, bom, "clamp")
    assert plan.counts == {"suppliers": 1, "products": 4, "bom_edges": 3, "customers": 2, "negative_stock": 2, "stock_adjusted": 2}
    assert [k for k, _ in plan.suppliers] == ["S1"] and [k for k, _ in plan.customers] == ["c1", "c2"]
    assert "BAD " not in [k for k, _ in plan.products]
    assert plan.negative_stock_list_ids == ["L-RAW2", "L-TOP"] == plan.stock_adjusted_list_ids  # in product-key order
    assert [k for k, _ in plan.bom_edges] == [bom_edge_key(*e) for e in (("MID", "RAW2"), ("TOP", "MID"), ("TOP", "RAW"))]  # MID (a component of TOP) is written before TOP
    assert build_push_plan(suppliers, products, customers, bom, "as_is").counts["stock_adjusted"] == 0


def test_bom_edge_key_cannot_collide():
    assert bom_edge_key("A|B", "C") != bom_edge_key("A", "B|C")
    assert bom_edge_key("A", "B") == '["A", "B"]' and bom_edge_key("ä", "B") == '["ä", "B"]'


def test_plan_is_sorted_by_key():
    suppliers = resolve_suppliers([sup("Zed"), sup("Abe")])
    products = resolve_products([prod("Z"), prod("A")], suppliers)
    customers = resolve_customers([Customer(code="c2", list_id="c2", name="N"), Customer(code="c1", list_id="c1", name="N2")])
    plan = build_push_plan(suppliers, products, customers, plan_bom(products, []))
    assert [k for k, _ in plan.suppliers] == ["Abe", "Zed"] and [k for k, _ in plan.products] == ["A", "Z"] and [k for k, _ in plan.customers] == ["c1", "c2"]


def test_plan_rejects_inconsistent_inputs():
    suppliers, products = resolve_suppliers([sup("S1")]), QuarantineResult[Product](ok=[prod("P")])
    ghost = BomPlan(edges_by_parent={"P": [BomEdge(parent_sku="P", component_sku="GHOST", quantity=Decimal(1))]}, order=["P"])
    with pytest.raises(ValueError, match="BOM edge"):
        build_push_plan(suppliers, products, resolve_customers([]), ghost)
    unlinked = QuarantineResult[Product](ok=[prod("P", preferred_vendor_code="NOPE")])
    with pytest.raises(ValueError, match="supplier"):
        build_push_plan(suppliers, unlinked, resolve_customers([]), BomPlan())


def test_category_empty_group_and_name_whitespace():
    body = to_product_request(prod("A", full_name=":A", description="  Two\n lines\t  and   gaps  "))[0].payload()
    assert "category" not in body and body["name"] == "Two lines and gaps"
    assert to_product_request(prod("A  B", description="x"))[0].payload()["productId"] == "A  B"  # keys are never collapsed
