"""Admin API. Every route requires X-Admin-Token. Paths follow the user's spec (no /api/v1)."""
import secrets
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from . import store
from .config import Settings
from .qwc import render_qwc
from .tasks import ENTITY_ALIASES, JOB_TYPES, READ_JOB_TYPES

_token_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def require_admin(request: Request, token: Optional[str] = Security(_token_header)) -> None:
    expected = request.app.state.settings.admin_token.get_secret_value()
    if not token or not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "invalid or missing X-Admin-Token")


router = APIRouter(dependencies=[Depends(require_admin)])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class JobOut(_Out):
    id: int
    job_type: str
    status: str
    status_message: Optional[str]
    idempotency_key: Optional[str]
    ref_number: Optional[str]
    params: dict[str, Any]
    result: Optional[dict[str, Any]]
    attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime]


class RecordOut(_Out):
    id: int
    entity: str
    qb_id: str
    edit_sequence: Optional[str]
    time_modified: Optional[str]
    payload: dict[str, Any]
    job_id: Optional[int]
    synced_at: datetime


class LogOut(_Out):
    id: int
    created_at: datetime
    job_id: Optional[int]
    level: str
    entity: Optional[str]
    status_code: Optional[int]
    message: str


class CreateTestPO(BaseModel):
    vendor_list_id: Optional[str] = None
    item_list_id: Optional[str] = None
    quantity: Decimal = Field(Decimal(1), gt=0)
    rate: Decimal = Field(Decimal(1), ge=0)
    idempotency_key: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/qwc")
def download_qwc(request: Request) -> Response:
    return Response(
        render_qwc(request.app.state.settings),
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="quickbook-poc.qwc"'},
    )


@router.post("/jobs/all-reads", response_model=list[JobOut])  # must precede /jobs/{job_type}
def queue_all_reads() -> Any:
    return [store.create_job(t) for t in READ_JOB_TYPES]


def _queue_test_po(settings: Settings, body: CreateTestPO) -> Any:
    if not settings.writes_enabled:
        raise HTTPException(403, "writes are disabled (set WRITES_ENABLED=true)")
    key = body.idempotency_key or uuid4().hex
    existing = store.get_job_by_key(key)
    if existing:
        if existing.status == "running":
            raise HTTPException(409, "job is running")
        # error/unsupported -> retry the same job (same key, ref, params); done/queued -> unchanged
        return store.requeue_job(existing.id) if existing.status in ("error", "unsupported") else existing
    vendor = body.vendor_list_id or store.first_record_id("Vendor")
    item = body.item_list_id or store.first_record_id("ItemInventory")
    if not vendor or not item:
        raise HTTPException(409, "no vendor/item given and none synced yet; run the vendors and items jobs first")
    params = {"vendor_list_id": vendor, "item_list_id": item, "quantity": str(body.quantity), "rate": str(body.rate)}
    ref = "ST-" + sha256(key.encode()).hexdigest()[:8].upper()  # 11 chars = QB RefNumber max
    return store.create_job("create_test_po", params, key, ref)


@router.post("/jobs/{job_type}", response_model=JobOut)
def queue_job(job_type: str, request: Request, body: Optional[CreateTestPO] = None) -> Any:
    if job_type not in JOB_TYPES:
        raise HTTPException(404, f"unknown job type; use one of {sorted(JOB_TYPES)}")
    if job_type == "create_test_po":
        return _queue_test_po(request.app.state.settings, body or CreateTestPO())
    return store.create_job(job_type)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(status: Optional[str] = None, limit: int = Query(50, ge=1, le=500)) -> Any:
    return store.list_jobs(status, limit)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int) -> Any:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.get("/records/{entity}", response_model=list[RecordOut])
def list_records(entity: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> Any:
    return store.list_records(ENTITY_ALIASES.get(entity, (entity,)), limit, offset)


@router.get("/log", response_model=list[LogOut])
def list_log(limit: int = Query(100, ge=1, le=1000), job_id: Optional[int] = None) -> Any:
    return store.list_log(limit, job_id)
