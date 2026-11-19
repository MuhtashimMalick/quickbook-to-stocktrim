from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...preflight.schema import Verdict
from ...sync import engine
from .preflight import get_stocktrim_client_kw

router = APIRouter(prefix="/push")  # CLI only (starts/cancels/resumes pushes)
runs_router = APIRouter(prefix="/push")  # read-only progress, proxy-reachable


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PushRequest(M):
    steps: list[Literal["suppliers", "products", "bom", "customers", "sales_orders", "purchase_orders"]] = Field(["suppliers", "products"], min_length=1)  # run in this order whatever the list order
    dry_run: bool = True
    preflight_fingerprint: Optional[str] = Field(None, max_length=128)
    plan_fingerprint: Optional[str] = Field(None, max_length=128)  # required for a real run: the one the dry-run returned
    mode: Literal["auto", "initial", "sync"] = "auto"  # auto = initial while the target has no completed non-dry-run run, else sync (diff against the pushed_item ledger)
    acknowledge_foreign: Optional[str] = Field(None, max_length=128)  # sync mode: the verdict's foreign_hash, to proceed despite records StockTrim holds that we did not push
    stamp_only: bool = False  # adopt the current state as the baseline: writes payload hashes into the ledger, NO StockTrim call (TEST account, sync mode; see README)
    acknowledge_requeue: Optional[dict[Literal["suppliers", "products", "customers"], int]] = None  # sync mode: per step the re-queued count a human has seen (mass_requeue); the scheduler never sets it
    acknowledge_delete: Optional[dict[Literal["bom"], int]] = None  # sync mode: the BOM edge DELETE count a human has seen (mass_delete); the scheduler never sets it

    @model_validator(mode="after")
    def _real_run_needs_plan_fingerprint(self) -> "PushRequest":
        if not self.dry_run and not self.plan_fingerprint:
            raise ValueError("plan_fingerprint is required when dry_run is false")
        if self.stamp_only and "bom" in self.steps:
            raise ValueError("stamp_only covers suppliers, products and customers (BOM keeps its own ledger)")
        return self


class PlanCounts(M):
    suppliers: int
    products: int
    bom_edges: int
    customers: int
    negative_stock: int
    stock_adjusted: int
    dropped_supplier_links: int
    bom_to_post: Optional[int] = None  # only when the `bom` step is selected: diff against the stored edges
    bom_to_delete: Optional[int] = None
    sales_orders: Optional[dict[str, int]] = None  # only when selected: orders, lines (to send) and skipped lines by reason (non_product, group, zero_quantity, closed_uninvoiced)
    purchase_orders: Optional[dict[str, int]] = None  # only when selected: sent, received (POs to send) and skipped (vendor_not_pushed, no_lines, lines_non_product)


class PushStarted(M):
    run_id: UUID
    status: str
    dry_run: bool
    mode: Optional[str] = None  # initial | sync | stamp
    sync: Optional[dict[str, Any]] = None  # dry-run in sync mode: per step new/changed/unchanged/requeued/vanished (+ vanished_list_ids; bom: to_post/to_delete)
    planned_calls: Optional[int] = None
    eta_seconds: Optional[int] = None
    stamped: Optional[dict[str, int]] = None  # stamp_only: ledger rows adopted (would be, for a dry-run) per step
    plan: Optional[PlanCounts] = None
    verdict: Optional[Verdict] = None
    plan_fingerprint: Optional[str] = None
    preflight_fingerprint: Optional[str] = None


class Sample(M):
    entity: str
    ref: Optional[str]  # QuickBooks ListID: never a name (supplier keys can be personal names)
    error_code: str


class PushRunOut(M):
    run_id: UUID
    status: str
    dry_run: bool
    mode: Optional[str]
    requested_by: Optional[str]
    target: Optional[str]  # "<environment> @ <host>": no credentials
    steps: list[str]
    started_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime]
    counts: dict[str, dict[str, int]]
    error_codes: dict[str, dict[str, int]]
    eta_seconds: Optional[int]
    summary: Optional[dict[str, Any]]
    samples: list[Sample]


def _call(fn: Any, *a: Any) -> Any:
    try:
        return fn(*a)
    except engine.PushError as e:
        raise HTTPException(e.status, {"code": e.code, **e.extra} if e.extra else e.code) from None


@router.post("", response_model=PushStarted, response_model_exclude_none=True)
def push(request: Request, body: PushRequest, kw: dict[str, Any] = Depends(get_stocktrim_client_kw)) -> Any:
    """dry_run (default): plan + pre-flight, writes nothing to StockTrim. Real run: needs STOCKTRIM_WRITES_ENABLED, an ok pre-flight and BOTH fingerprints of the dry-run."""
    return _call(engine.start, request.app.state.settings, body.steps, body.dry_run, body.preflight_fingerprint, body.plan_fingerprint, kw, body.mode, body.acknowledge_foreign, body.stamp_only, body.acknowledge_requeue, body.acknowledge_delete, request.state.actor)


@runs_router.get("/runs/{run_id}", response_model=PushRunOut)
def run_progress(request: Request, run_id: UUID) -> Any:
    return _call(engine.progress, request.app.state.settings, run_id)


@router.post("/runs/{run_id}/cancel", response_model=PushRunOut)
def cancel(request: Request, run_id: UUID) -> Any:
    _call(engine.cancel, run_id, request.state.actor)
    return _call(engine.progress, request.app.state.settings, run_id)


@router.post("/runs/{run_id}/resume", response_model=PushStarted, response_model_exclude_none=True)
def resume(request: Request, run_id: UUID, kw: dict[str, Any] = Depends(get_stocktrim_client_kw)) -> Any:
    return _call(engine.resume, request.app.state.settings, run_id, kw, request.state.actor)
