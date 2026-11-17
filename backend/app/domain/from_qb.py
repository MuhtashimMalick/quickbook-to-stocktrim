"""Stored qb_records payloads -> canonical models. Pure; numbers are Decimal, empty -> None, keys are never altered."""
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any, Literal, Optional

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import QBRecord
from .models import Address, Customer, Issue, Product, ProductKeyField, Supplier

_NUMERIC_REF = re.compile(r"&#\d+;")


def as_list(v: Any) -> list[Any]:
    """QuickBooks parsers yield a dict for one element, a list for repeats, '' or None for empty."""
    return v if isinstance(v, list) else [] if v in (None, "") else [v]


def _s(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v != "" else None


def first_dict(v: Any) -> dict[str, Any]:
    """First dict of a QuickBooks 'one-or-many' element ({} when absent)."""
    return next((x for x in as_list(v) if isinstance(x, dict)), {})


class NumberError(ValueError):
    """A numeric field failed to parse. Carries the FIELD name only, never the value."""

    def __init__(self, field_name: str) -> None:
        super().__init__(f"{field_name}: not a number")
        self.field_name = field_name


def _dec(v: Any, field_name: str) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        out = Decimal(str(v).strip())
    except InvalidOperation as exc:
        raise NumberError(field_name) from exc
    if not out.is_finite():
        raise NumberError(field_name)
    return out


def _why(exc: Exception) -> str:
    """Parse-error message: exception type + failing field name(s); never the offending value."""
    if isinstance(exc, ValidationError):
        return f"ValidationError: {', '.join('.'.join(map(str, e['loc'])) for e in exc.errors())}"
    return f"{type(exc).__name__}: {exc.field_name}" if isinstance(exc, NumberError) else type(exc).__name__


def _active(p: dict[str, Any]) -> bool:
    return str(p.get("IsActive", "true")).strip().lower() != "false"


def _unescape(s: str) -> str:
    """Literal numeric character references in DESCRIPTIONS only (never keys/names)."""
    return _NUMERIC_REF.sub(lambda m: unescape(m.group()), s)


def _address(v: Any) -> Optional[Address]:
    a = first_dict(v)
    lines = [x for i in range(1, 6) if (x := _s(a.get(f"Addr{i}")))]
    addr = Address(lines=lines, city=_s(a.get("City")), state=_s(a.get("State")), postal_code=_s(a.get("PostalCode")), country=_s(a.get("Country")))
    return addr if addr.model_dump(exclude_defaults=True) else None


def parse_product(p: dict[str, Any], kind: Literal["inventory", "assembly"], key_field: ProductKeyField = "Name") -> Product:
    desc = _unescape(_s(p.get("SalesDesc")) or _s(p.get("PurchaseDesc")) or _s(p.get("Name")) or "")
    vendor = first_dict(p.get("PrefVendorRef"))
    return Product(
        sku=p.get(key_field) or "", list_id=p.get("ListID") or "", full_name=_s(p.get("FullName")), kind=kind, description=desc,
        cost=_dec(p.get("PurchaseCost"), "PurchaseCost"), price=_dec(p.get("SalesPrice"), "SalesPrice"), quantity_on_hand=_dec(p.get("QuantityOnHand"), "QuantityOnHand"),
        quantity_on_order=_dec(p.get("QuantityOnOrder"), "QuantityOnOrder"), reorder_point=_dec(p.get(bp := "BuildPoint" if kind == "assembly" else "ReorderPoint"), bp),
        max_stock=_dec(p.get("Max"), "Max"), preferred_vendor_list_id=_s(vendor.get("ListID")),
        manufacturer_part_number=_s(p.get("ManufacturerPartNumber")), is_active=_active(p),
    )


def parse_supplier(p: dict[str, Any]) -> Supplier:
    return Supplier(
        code=p.get("Name") or "", list_id=p.get("ListID") or "", name=_s(p.get("CompanyName")) or p.get("Name") or "",
        email=_s(p.get("Email")), contact=_s(p.get("Contact")), phone=_s(p.get("Phone")), address=_address(p.get("VendorAddress")), is_active=_active(p),
    )


def parse_customer(p: dict[str, Any]) -> Customer:
    return Customer(
        code=p.get("ListID") or "", list_id=p.get("ListID") or "", name=_s(p.get("FullName")) or p.get("Name") or "",
        email=_s(p.get("Email")), phone=_s(p.get("Phone")), address=_address(p.get("BillAddress")), is_active=_active(p),
    )


@dataclass
class Snapshot:
    products: list[Product] = field(default_factory=list)  # inventory + assembly, unvalidated
    suppliers: list[Supplier] = field(default_factory=list)
    customers: list[Customer] = field(default_factory=list)
    assemblies: list[dict[str, Any]] = field(default_factory=list)  # raw payloads: the BOM planner reads their lines
    issues: list[Issue] = field(default_factory=list)  # parse errors


def _stream(session: Session, entity: str, page: int = 1000) -> Iterator[tuple[str, dict[str, Any]]]:
    stmt = select(QBRecord.qb_id, QBRecord.payload).where(QBRecord.entity == entity).order_by(QBRecord.id).execution_options(yield_per=page)
    yield from session.execute(stmt)


def stream_payloads(session: Session, entity: str) -> Iterator[dict[str, Any]]:
    """Raw stored payloads of one entity (SalesOrder, PurchaseOrder), streamed."""
    return (p for _, p in _stream(session, entity))


def load_snapshot(session: Session, key_field: ProductKeyField = "Name") -> Snapshot:
    """Reads only the four master-data entities, in streaming pages (never SalesOrder/PurchaseOrder)."""
    snap = Snapshot()
    plan: list[tuple[str, Callable[[dict[str, Any]], Any], list]] = [
        ("Customer", parse_customer, snap.customers),
        ("Vendor", parse_supplier, snap.suppliers),
        ("ItemInventory", lambda p: parse_product(p, "inventory", key_field), snap.products),
        ("ItemInventoryAssembly", lambda p: parse_product(p, "assembly", key_field), snap.products),
    ]
    for entity, parse, out in plan:
        for qb_id, payload in _stream(session, entity):
            if entity == "ItemInventoryAssembly":
                snap.assemblies.append(payload)
            try:
                out.append(parse(payload))
            except ValueError as exc:  # includes pydantic ValidationError
                snap.issues.append(Issue(entity=entity, ref=qb_id, code="parse_error", severity="quarantine", message=_why(exc)))
    return snap
