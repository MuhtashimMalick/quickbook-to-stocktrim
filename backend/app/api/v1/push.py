from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...preflight.schema import Verdict
from ...sync import engine
from .preflight import get_stocktrim_client_kw

router = APIRouter(prefix="/push")


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PushRequest(M):
    steps: list[Literal["suppliers", "products"]] = Field(["suppliers", "products"], min_length=1)  # customers/BOM arrive in stage 10
    dry_run: bool = True
    preflight_fingerprint: Optional[str] = Field(None, max_length=128)


class PlanCounts(M):
    suppliers: int
    products: int
    bom_edges: int
    customers: int
    negative_stock: int
    stock_adjusted: int
    dropped_supplier_links: int


class PushStarted(M):
    run_id: UUID
    status: str
    dry_run: bool
    plan: Optional[PlanCounts] = None
    verdict: Optional[Verdict] = None
    plan_fingerprint: Optional[str] = None
    preflight_fingerprint: Optional[str] = None


class Sample(M):
    entity: str
    key: str  # supplier code or SKU: keys, never names
    error_code: str


class PushRunOut(M):
    run_id: UUID
    status: str
    dry_run: bool
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
        raise HTTPException(e.status, e.code) from None


@router.post("", response_model=PushStarted, response_model_exclude_none=True)
def push(request: Request, body: PushRequest, kw: dict[str, Any] = Depends(get_stocktrim_client_kw)) -> Any:
    """dry_run (default): plan + pre-flight, writes nothing to StockTrim. Real run: needs STOCKTRIM_WRITES_ENABLED, an ok pre-flight and its fingerprint."""
    return _call(engine.start, request.app.state.settings, body.steps, body.dry_run, body.preflight_fingerprint, kw)


@router.get("/runs/{run_id}", response_model=PushRunOut)
def run_progress(request: Request, run_id: UUID) -> Any:
    return _call(engine.progress, request.app.state.settings, run_id)


@router.post("/runs/{run_id}/cancel", response_model=PushRunOut)
def cancel(request: Request, run_id: UUID) -> Any:
    _call(engine.cancel, run_id)
    return _call(engine.progress, request.app.state.settings, run_id)


@router.post("/runs/{run_id}/resume", response_model=PushStarted, response_model_exclude_none=True)
def resume(request: Request, run_id: UUID, kw: dict[str, Any] = Depends(get_stocktrim_client_kw)) -> Any:
    return _call(engine.resume, request.app.state.settings, run_id, kw)
