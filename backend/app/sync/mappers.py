"""Pure mappers: Stage 6 domain models -> Stage 5 StockTrim request DTOs (no I/O). Fields the DTO lacks are not sent."""
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.utils import parseaddr
from typing import NamedTuple, Optional

from email_validator import EmailNotValidError, validate_email

from ..domain.bom import BomPlan
from ..domain.from_qb import _dec, as_list, first_dict
from ..domain.keys import norm
from ..domain.models import Address, BomEdge, Customer, NegativeStockPolicy, Product, QuarantineResult, Supplier
from ..stocktrim.dto import (
    BomRequest,
    CustomerRequest,
    ProductRequest,
    ProductSupplier,
    PurchaseOrderLine,
    PurchaseOrderRequest,
    PurchaseOrderSupplier,
    SalesOrderRequest,
    SupplierRequest,
)

_MAILTO = re.compile(r"mailto:", re.IGNORECASE)
_WS = re.compile(r"\s+")


_CENT = Decimal("0.01")


def _r2(v: Optional[Decimal]) -> Optional[Decimal]:
    """StockTrim stores cost, price and stockOnHand with 2 decimals, ROUND_HALF_UP (measured live; negatives assumed to round away from zero, unproven).
    Rounding BEFORE building the request keeps plan fingerprints stable and read-backs exact."""
    return None if v is None else v.quantize(_CENT, ROUND_HALF_UP)


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


_CLEAR = ("email_address", "primary_contact_name", "street_address", "address_line1", "address_line2", "state", "post_code", "country")  # probed: "" clears them, null is ignored


def to_supplier_request(s: Supplier) -> SupplierRequest:
    """A supplier field QuickBooks has no value for is sent as "" (StockTrim clears it; omission or null would keep the old value, see docs/stocktrim-api-notes.md section 3a)."""
    addr = {"street_address": None, "address_line1": None, "address_line2": None, "state": None, "post_code": None, "country": None, **_address(s.address, s.code, s.name)}
    addr.pop("city", None)  # the swagger SupplierDto has no city
    f = {"email_address": first_email(s.email), "primary_contact_name": s.contact, **addr}
    return SupplierRequest(supplier_code=s.code, supplier_name=s.name, external_id=s.list_id, **{k: f[k] or "" for k in _CLEAR})


def to_product_request(p: Product, policy: NegativeStockPolicy = "as_is") -> tuple[ProductRequest, bool]:
    """(request, stock_adjusted): stock_adjusted is True when the `clamp` policy replaced a negative on-hand by 0. stockOnOrder is never sent.
    category = the top-level QuickBooks group of full_name ('Group:Item'); "" (clears it in StockTrim) for a top-level item or an empty group (':Item'); name = description with whitespace runs collapsed (default, change if the client wants another)."""
    qoh = p.quantity_on_hand or Decimal(0)
    adjusted = policy == "clamp" and qoh < 0
    category = p.full_name.split(":", 1)[0] if p.full_name and ":" in p.full_name else ""  # "" clears a category (probed; null is ignored)
    # supplierName is deliberately NOT sent: its behaviour is undocumented and it could rename the supplier created by the supplier push
    supplier = ([ProductSupplier(supplier_id=p.preferred_vendor_code, supplier_sku_code=p.manufacturer_part_number)]
                if p.preferred_vendor_code else None)
    req = ProductRequest(
        product_id=p.sku, product_code_readable=p.sku, stock_on_hand=0 if adjusted else _r2(qoh), name=_WS.sub(" ", p.description).strip(), category=category, cost=_r2(p.cost),
        price=_r2(p.price), supplier_code=p.preferred_vendor_code, suppliers=supplier, minimum_shelf_level=p.reorder_point,
        maximum_shelf_level=p.max_stock if p.max_stock and p.max_stock > 0 else None, discontinued=not p.is_active,
    )
    return req, adjusted


def to_customer_request(c: Customer) -> CustomerRequest:
    return CustomerRequest(code=c.code, name=c.name, email_address=first_email(c.email), phone=c.phone, **_address(c.address, c.name))


def to_bom_request(e: BomEdge) -> BomRequest:
    return BomRequest(product_code=e.parent_sku, component_code=e.component_sku, quantity=e.quantity)


def _day(v: object) -> Optional[datetime]:
    """QuickBooks TxnDate/ExpectedDate ('2026-06-07') -> midnight UTC; None when absent or unreadable."""
    try:
        return datetime.combine(date.fromisoformat(str(v)[:10]), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        return None


def _true(v: object) -> bool:
    return str(v).strip().lower() == "true"


def _qty(p: dict, field_name: str) -> Decimal:
    return _r2(_dec(p.get(field_name), field_name) or Decimal(0))  # type: ignore[return-value]


class Excl(NamedTuple):
    """One record the plan leaves out (the run log's push_exclusion row): business key, QuickBooks ListID/TxnID, reason code."""
    entity: str
    key: str
    ref: str
    code: str


_KEY = {"supplier": lambda r: r.code, "product": lambda r: r.sku, "customer": lambda r: r.code}
_ENTITY = {"Vendor": "supplier", "Customer": "customer"}  # parse-issue entity -> push entity (every other one is an item)
_KEPT_OUT_WARNINGS = ("inactive_supplier", "inactive_customer", "unresolved_vendor")  # warning issues that still leave something out (the record / the supplier link)


def domain_exclusions(suppliers: QuarantineResult[Supplier], products: QuarantineResult[Product], customers: QuarantineResult[Customer], bom: BomPlan, parse_issues: Iterable) -> list[Excl]:
    """Quarantined / inactive records, supplier links and BOM lines the plan leaves out, from what the resolvers already computed. Records that failed to parse and BOM issues have no
    business key here: the ListID stands in."""
    out = [Excl(_ENTITY.get(i.entity, "product"), i.ref, i.ref, i.code) for i in parse_issues]
    for entity, res in (("supplier", suppliers), ("product", products), ("customer", customers)):
        key = _KEY[entity]
        out += [Excl(entity, key(q.record), i.ref, i.code) for q in res.quarantined for i in q.issues if i.severity == "quarantine"]
        by_lid = {r.list_id: key(r) for r in (*res.ok, *res.inactive)}
        out += [Excl(entity, by_lid.get(w.ref, w.ref), w.ref, w.code) for w in res.warnings if w.code in _KEPT_OUT_WARNINGS]
    return out + [Excl("bom_edge", i.ref, i.ref, i.code) for i in bom.issues]


def sales_order_lines(orders: Iterable[dict], sku_of: dict[str, str], customers: set[str], since: date, skipped: Optional[list[Excl]] = None) -> tuple[list[tuple[str, SalesOrderRequest]], dict[str, int]]:
    """One request per order LINE of the orders dated >= `since`; key = externalReferenceId = 'TxnID:TxnLineID'. Only lines of pushable products (`sku_of`: ListID -> SKU) are sent;
    quantity = ordered, but invoiced for a manually closed order. customerCode only when that customer is pushed (StockTrim rejects an unknown one).
    stats: orders (in window), lines (to send), skipped by reason: non_product, group (lines inside bundles), zero_quantity, closed_uninvoiced. `skipped` (out) receives one Excl per skipped line."""
    out: list[tuple[str, SalesOrderRequest]] = []
    st: Counter[str] = Counter()

    def skip(o: dict, ln: dict, code: str) -> None:
        st[code] += 1
        if skipped is not None:
            skipped.append(Excl("sales_order_line", f"{o.get('TxnID')}:{ln.get('TxnLineID')}", str(o.get("TxnID")), code))
    for o in orders:
        day = _day(o.get("TxnDate"))
        if day is None or day.date() < since:
            continue
        st["orders"] += 1
        closed = _true(o.get("IsManuallyClosed"))
        cust = first_dict(o.get("CustomerRef")).get("ListID")
        for g in (x for x in as_list(o.get("SalesOrderLineGroupRet")) if isinstance(x, dict)):
            for ln in as_list(g.get("SalesOrderLineRet")):
                skip(o, ln if isinstance(ln, dict) else {}, "group")
        for ln in (x for x in as_list(o.get("SalesOrderLineRet")) if isinstance(x, dict)):
            sku = sku_of.get(first_dict(ln.get("ItemRef")).get("ListID") or "")
            qty = _qty(ln, "Invoiced" if closed else "Quantity")
            if sku is None:
                skip(o, ln, "non_product")
            elif qty <= 0:
                skip(o, ln, "closed_uninvoiced" if closed else "zero_quantity")
            else:
                key = f"{o.get('TxnID')}:{ln.get('TxnLineID')}"
                price = _dec(ln.get("Rate"), "Rate")
                out.append((key, SalesOrderRequest(product_id=sku, order_date=day, quantity=qty, external_reference_id=key, unit_price=_r2(price),
                                                   customer_code=cust if cust in customers else None)))
    st["lines"] = len(out)
    return out, dict(st)


def _po_ref(po: dict, shared: set[str]) -> Optional[str]:
    """RefNumber, suffixed with the unique TxnNumber (else the first 8 characters of the TxnID) when another PO in the snapshot has the same one (compared normalised)."""
    ref = po.get("RefNumber") or None
    return f"{ref}-{po.get('TxnNumber') or str(po.get('TxnID'))[:8]}" if ref and norm(str(ref)) in shared else ref


def purchase_orders(pos: Iterable[dict], sku_of: dict[str, str], supplier_of: dict[str, str], skipped: Optional[list[Excl]] = None) -> tuple[list[tuple[str, PurchaseOrderRequest]], dict[str, int]]:
    """Every PO (no window): key = clientReferenceNumber = 'QB-' + TxnID. Open -> Sent with the REMAINING quantity per line (Quantity - ReceivedQuantity, lines <= 0 dropped);
    fully received or manually closed -> Received with the full ordered quantity (history). Lines are summed per product (StockTrim merges lines by productId).
    referenceNumber: RefNumber, or RefNumber-TxnNumber when the RefNumber is shared by several POs (counted over ALL POs). Skipped: vendor not a pushed supplier (`supplier_of`: ListID -> code; StockTrim would auto-create one), no pushable line. stockOnOrder is never involved.
    stats: sent, received (POs to send), vendor_not_pushed, no_lines (POs skipped), lines_non_product. `skipped` (out) receives one Excl per skipped PO / non-product line."""
    pos = list(pos)
    shared = {n for n, c in Counter(norm(str(p.get("RefNumber") or "")) for p in pos).items() if n and c > 1}  # StockTrim merges POs that share a referenceNumber
    out: list[tuple[str, PurchaseOrderRequest]] = []
    st: Counter[str] = Counter()

    def skip(po: dict, code: str, line: Optional[dict] = None) -> None:
        st[code] += 1
        if skipped is not None:
            skipped.append(Excl("purchase_order", f"QB-{po.get('TxnID')}" + (f":{line.get('TxnLineID')}" if line is not None else ""), str(po.get("TxnID")), code))

    for po in pos:
        vendor = supplier_of.get(first_dict(po.get("VendorRef")).get("ListID") or "")
        if vendor is None:
            skip(po, "vendor_not_pushed")
            continue
        done = _true(po.get("IsFullyReceived")) or _true(po.get("IsManuallyClosed"))
        eta = _day(po.get("ExpectedDate"))
        qtys: dict[str, Decimal] = {}
        prices: dict[str, Optional[Decimal]] = {}
        for ln in (x for x in as_list(po.get("PurchaseOrderLineRet")) if isinstance(x, dict)):
            sku = sku_of.get(first_dict(ln.get("ItemRef")).get("ListID") or "")
            qty = _qty(ln, "Quantity") - (0 if done else _qty(ln, "ReceivedQuantity"))
            if sku is None:
                skip(po, "lines_non_product", ln)
            elif qty > 0:
                qtys[sku] = qtys.get(sku, Decimal(0)) + qty
                prices.setdefault(sku, _r2(_dec(ln.get("Rate"), "Rate")))
        if not qtys:
            skip(po, "no_lines")
            continue
        st["received" if done else "sent"] += 1
        key = "QB-" + str(po.get("TxnID"))
        out.append((key, PurchaseOrderRequest(supplier=PurchaseOrderSupplier(supplier_code=vendor), purchase_order_line_items=[PurchaseOrderLine(product_id=s, quantity=q, eta_date=eta, unit_price=prices[s]) for s, q in qtys.items()], order_date=_day(po.get("TxnDate")),
                                              reference_number=_po_ref(po, shared), client_reference_number=key, status="Received" if done else "Sent")))
    return out, dict(st)


@dataclass
class PushPlan:
    """Ordered (key, request) pairs per entity, in push order. Keys: supplier code | SKU | bom_edge_key(parent, component) | customer ListID."""

    suppliers: list[tuple[str, SupplierRequest]] = field(default_factory=list)
    products: list[tuple[str, ProductRequest]] = field(default_factory=list)
    bom_edges: list[tuple[str, BomRequest]] = field(default_factory=list)  # children's BOMs before their parents' (BomPlan.order)
    customers: list[tuple[str, CustomerRequest]] = field(default_factory=list)
    sales_orders: list[tuple[str, SalesOrderRequest]] = field(default_factory=list)  # key = 'TxnID:TxnLineID'
    purchase_orders: list[tuple[str, PurchaseOrderRequest]] = field(default_factory=list)  # key = 'QB-' + TxnID
    order_stats: dict[str, dict[str, int]] = field(default_factory=dict)  # step -> sales_order_lines / purchase_orders stats (only for the steps that were built)
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
