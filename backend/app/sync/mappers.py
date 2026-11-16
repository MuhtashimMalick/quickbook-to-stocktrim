"""Pure mappers: Stage 6 domain models -> Stage 5 StockTrim request DTOs (no I/O). Fields the DTO lacks are not sent."""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from email.utils import parseaddr
from typing import Optional

from email_validator import EmailNotValidError, validate_email

from ..domain.bom import BomPlan
from ..domain.models import Address, BomEdge, Customer, NegativeStockPolicy, Product, QuarantineResult, Supplier
from ..stocktrim.dto import BomRequest, CustomerRequest, ProductRequest, ProductSupplier, SupplierRequest

_MAILTO = re.compile(r"mailto:", re.IGNORECASE)
_WS = re.compile(r"\s+")


def bom_edge_key(parent: str, component: str) -> str:
    """The one key of a BOM edge (PushPlan, push_item.key; the engine must use this too): a JSON array, so SKUs containing '|' cannot collide."""
    return json.dumps([parent, component], ensure_ascii=False)


def _valid_email(part: str) -> Optional[str]:
    addr = parseaddr(_MAILTO.sub("", part.strip(), count=1))[1]
    try:
        return validate_email(addr, check_deliverability=False).normalized if addr else None
    except EmailNotValidError:
        return None


def first_email(raw: Optional[str]) -> Optional[str]:
    """First VALID address of a value that may hold several separated by ';' or ',' ('mailto:' and 'Name <addr>' accepted; checked with
    email-validator, no DNS). Internationalised addresses ('ä@x.de') are valid and returned NFC-normalised. None when there is none."""
    return next((e for part in re.split(r"[;,]", raw or "") if (e := _valid_email(part))), None)


def _address(a: Optional[Address], *names: str) -> dict[str, Optional[str]]:
    """Addr1..Addr5 lines (QuickBooks often puts the company name in Addr1: lines equal, case-insensitively, to a name are dropped)
    -> streetAddress, addressLine1, addressLine2 (the 3rd and later lines joined with ', '); city/state/postCode/country straight."""
    if a is None:
        return {}
    skip = {n.strip().casefold() for n in names}
    lines = [x for x in a.lines if x.strip().casefold() not in skip]
    return {"street_address": lines[0] if lines else None, "address_line1": lines[1] if len(lines) > 1 else None,
            "address_line2": ", ".join(lines[2:]) or None, "city": a.city, "state": a.state, "post_code": a.postal_code, "country": a.country}


def to_supplier_request(s: Supplier) -> SupplierRequest:
    addr = _address(s.address, s.code, s.name)
    addr.pop("city", None)  # the swagger SupplierDto has no city
    return SupplierRequest(supplier_code=s.code, supplier_name=s.name, email_address=first_email(s.email), primary_contact_name=s.contact,
                           external_id=s.list_id, **addr)


def to_product_request(p: Product, policy: NegativeStockPolicy = "as_is") -> tuple[ProductRequest, bool]:
    """(request, stock_adjusted): stock_adjusted is True when the `clamp` policy replaced a negative on-hand by 0. stockOnOrder is never sent.
    category = the top-level QuickBooks group of full_name ('Group:Item'); omitted for a top-level item or an empty group (':Item'); name = description with whitespace runs collapsed (default, change if the client wants another)."""
    qoh = p.quantity_on_hand or Decimal(0)
    adjusted = policy == "clamp" and qoh < 0
    category = (p.full_name.split(":", 1)[0] or None) if p.full_name and ":" in p.full_name else None
    # supplierName is deliberately NOT sent: its behaviour is undocumented and it could rename the supplier created by the supplier push
    supplier = ([ProductSupplier(supplier_id=p.preferred_vendor_code, supplier_sku_code=p.manufacturer_part_number)]
                if p.preferred_vendor_code else None)
    req = ProductRequest(
        product_id=p.sku, product_code_readable=p.sku, stock_on_hand=0 if adjusted else qoh, name=_WS.sub(" ", p.description).strip(), category=category, cost=p.cost,
        price=p.price, supplier_code=p.preferred_vendor_code, suppliers=supplier, minimum_shelf_level=p.reorder_point,
        maximum_shelf_level=p.max_stock if p.max_stock and p.max_stock > 0 else None, discontinued=not p.is_active,
    )
    return req, adjusted


def to_customer_request(c: Customer) -> CustomerRequest:
    return CustomerRequest(code=c.code, name=c.name, email_address=first_email(c.email), phone=c.phone, **_address(c.address, c.name))


def to_bom_request(e: BomEdge) -> BomRequest:
    return BomRequest(product_code=e.parent_sku, component_code=e.component_sku, quantity=e.quantity)


@dataclass
class PushPlan:
    """Ordered (key, request) pairs per entity, in push order. Keys: supplier code | SKU | bom_edge_key(parent, component) | customer ListID."""

    suppliers: list[tuple[str, SupplierRequest]] = field(default_factory=list)
    products: list[tuple[str, ProductRequest]] = field(default_factory=list)
    bom_edges: list[tuple[str, BomRequest]] = field(default_factory=list)  # children's BOMs before their parents' (BomPlan.order)
    customers: list[tuple[str, CustomerRequest]] = field(default_factory=list)
    negative_stock_list_ids: list[str] = field(default_factory=list)  # ListIDs with negative on-hand (whatever the policy)
    stock_adjusted_list_ids: list[str] = field(default_factory=list)  # ListIDs whose stock `clamp` set to 0

    @property
    def counts(self) -> dict[str, int]:
        return {"suppliers": len(self.suppliers), "products": len(self.products), "bom_edges": len(self.bom_edges), "customers": len(self.customers),
                "negative_stock": len(self.negative_stock_list_ids), "stock_adjusted": len(self.stock_adjusted_list_ids)}


def build_push_plan(suppliers: QuarantineResult[Supplier], products: QuarantineResult[Product], customers: QuarantineResult[Customer],
                    bom: BomPlan, policy: NegativeStockPolicy = "as_is") -> PushPlan:
    """Uses ONLY the `ok` lists (quarantined and inactive records are never pushed) and the BomPlan's pushable, bottom-up edges.
    Suppliers, products and customers are sorted by key (deterministic dry-runs). ValueError when the inputs disagree: a BOM edge end
    that is not a planned product, or a product whose preferred vendor is not a planned supplier."""
    sup_keys, prod_keys = {s.code for s in suppliers.ok}, {p.sku for p in products.ok}
    if bad := {sku for es in bom.edges_by_parent.values() for e in es for sku in (e.parent_sku, e.component_sku)} - prod_keys:
        raise ValueError(f"{len(bad)} BOM edge end(s) are not planned products")
    if bad := {p.sku for p in products.ok if p.preferred_vendor_code and p.preferred_vendor_code not in sup_keys}:
        raise ValueError(f"{len(bad)} product(s) link to a supplier that is not planned")
    plan = PushPlan(suppliers=sorted(((s.code, to_supplier_request(s)) for s in suppliers.ok), key=lambda x: x[0]))
    for p in sorted(products.ok, key=lambda p: p.sku):
        req, adjusted = to_product_request(p, policy)
        plan.products.append((p.sku, req))
        if (p.quantity_on_hand or 0) < 0:
            plan.negative_stock_list_ids.append(p.list_id)
        if adjusted:
            plan.stock_adjusted_list_ids.append(p.list_id)
    plan.bom_edges = [(bom_edge_key(e.parent_sku, e.component_sku), to_bom_request(e)) for parent in bom.order for e in bom.edges_by_parent[parent]]
    plan.customers = sorted(((c.code, to_customer_request(c)) for c in customers.ok), key=lambda x: x[0])
    return plan
