"""Response model of POST /preflight. extra="forbid" everywhere: a field that is not declared here cannot reach the JSON, so record
content (names, descriptions) can only leak through a declared field. `Sample.ref` is a QuickBooks ListID (never a Name); StockTrim
product samples carry the product key (SKU): the endpoint is admin-only."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sample(M):
    ref: str
    code: str


class Group(M):
    severity: str
    count: int
    samples: list[Sample]


class EntityCounts(M):
    parsed: int
    ok: int
    quarantined: int
    parse_errors: int
    warnings: int
    inactive: Optional[int] = None


class Entities(M):
    products: EntityCounts
    suppliers: EntityCounts
    customers: EntityCounts


class Bom(M):
    assemblies: int
    assemblies_with_edges: int
    edges: int
    summed_duplicates: int
    assemblies_with_summed_duplicates: int
    excluded_lines: int
    dropped_edges: int
    quarantined_parents: int
    cycle_ancestors: int
    max_depth: int
    depth_histogram: dict[int, int]
    assemblies_without_lines: int
    assemblies_all_lines_excluded: int
    subassemblies_without_bom: int


class QuickBooks(M):
    entities: Entities
    issues: dict[str, Group]
    bom: Bom
    negative_stock_count: int


class Overlap(M):
    count: int
    samples: list[Sample]


class Step(M):
    """status ok | skipped | aborted; the other fields are set only by the step that owns them."""
    status: str = "ok"
    error: Optional[str] = None
    checked: Optional[int] = None


class Products(Step):
    count: Optional[int] = None
    capped: Optional[bool] = None
    overlap_product_id: Optional[Overlap] = None
    overlap_readable: Optional[Overlap] = None
    overlap_readable_only: Optional[Overlap] = None
    duplicate_readable_groups: Optional[int] = None
    readable_differs_from_product_id: Optional[int] = None


class Customers(Step):
    count: Optional[int] = None
    code_collisions: Optional[int] = None
    samples: Optional[list[Sample]] = None
    duplicate_code_groups: Optional[int] = None


class PurchaseOrders(Step):
    count: Optional[int] = None
    capped: Optional[bool] = None
    by_status: Optional[dict[str, int]] = None


class Suppliers(Step):
    count: Optional[int] = None
    overlap: Optional[int] = None
    samples: Optional[list[Sample]] = None


class Probe(Step):
    found: Optional[int] = None
    errors: Optional[int] = None
    samples: Optional[list[Sample]] = None


class StockTrim(M):
    status: str
    error: Optional[str] = None
    products: Optional[Products] = None
    customers: Optional[Customers] = None
    purchase_orders: Optional[PurchaseOrders] = None
    suppliers: Optional[Suppliers] = None
    suppliers_by_code: Optional[Probe] = None
    sales_orders: Optional[Probe] = None
    boms: Optional[Step] = None


class Finding(M):
    code: str
    count: int
    detail: Optional[dict[str, int]] = None


class Verdict(M):
    ok_to_push: bool
    blocking: list[Finding]
    warnings: list[Finding]


class Snapshot(M):
    synced_at_max: dict[str, datetime]


class Plan(M):
    """Upper bound for a later push (one call per record) at `rps`; the pre-flight's own reads are not included."""
    planned_calls: dict[str, int]
    total_calls: int
    rps: float
    eta_seconds: int


class PreflightReport(M):
    quickbooks: QuickBooks
    stocktrim: StockTrim
    verdict: Verdict
    snapshot: Snapshot
    plan: Plan
    fingerprint: str
