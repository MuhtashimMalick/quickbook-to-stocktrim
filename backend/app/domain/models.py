from decimal import Decimal
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

IssueCode = Literal[
    "duplicate_key", "whitespace_key", "control_chars", "empty_key", "key_too_long", "internal_double_space", "non_ascii_key",
    "duplicate_name", "unresolved_vendor", "nonpositive_quantity", "bom_self_reference", "bom_cycle", "bom_component_excluded",
    "bom_component_unresolved", "parse_error", "empty_list_id", "bom_cycle_ancestor", "inactive_supplier",
    "inactive_customer", "bom_quantity_precision",
]
ProductKeyField = Literal["Name", "FullName"]  # Product SKU source field
NegativeStockPolicy = Literal["as_is", "clamp"]  # negative QuickBooks stock: send as-is or send 0


class Issue(BaseModel):
    entity: str
    ref: str  # ListID (or the key when there is none); messages carry keys only, never other record content
    code: IssueCode
    severity: Literal["quarantine", "warning"]
    message: str


class Quarantined(BaseModel, Generic[T]):
    record: T
    issues: list[Issue]


class QuarantineResult(BaseModel, Generic[T]):
    ok: list[T] = Field(default_factory=list)
    quarantined: list[Quarantined[T]] = Field(default_factory=list)
    inactive: list[T] = Field(default_factory=list)  # valid but IsActive=false (suppliers and customers); never pushed
    warnings: list[Issue] = Field(default_factory=list)


class Address(BaseModel):
    lines: list[str] = Field(default_factory=list)
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class Product(BaseModel):
    sku: str  # the QuickBooks item key field (Name by default), kept exactly as stored
    list_id: str
    full_name: Optional[str] = None
    kind: Literal["inventory", "assembly"]
    description: str  # SalesDesc, else PurchaseDesc, else Name; also used as the StockTrim product name
    cost: Optional[Decimal] = None
    price: Optional[Decimal] = None
    quantity_on_hand: Optional[Decimal] = None  # raw, may be negative
    quantity_on_order: Optional[Decimal] = None
    reorder_point: Optional[Decimal] = None  # ReorderPoint (inventory) / BuildPoint (assembly)
    max_stock: Optional[Decimal] = None
    preferred_vendor_code: Optional[str] = None  # the pushable Vendor key; set ONLY by resolve_products
    preferred_vendor_list_id: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    is_active: bool = True


class Supplier(BaseModel):
    code: str  # Vendor Name
    list_id: str
    name: str
    email: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[Address] = None
    is_active: bool = True


class Customer(BaseModel):
    code: str  # ListID
    list_id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[Address] = None
    is_active: bool = True


class BomEdge(BaseModel):
    parent_sku: str
    component_sku: str
    quantity: Decimal = Field(gt=0)
