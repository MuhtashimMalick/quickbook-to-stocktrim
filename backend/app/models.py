import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# One source for the allowed values: the CHECK constraints below and the later push engine use these tuples.
PUSH_RUN_STATUSES = ("running", "done", "done_with_failures", "failed", "interrupted", "cancelled")
PUSH_ITEM_STATUSES = ("pending", "done", "failed", "skipped")
PUSH_ENTITIES = ("supplier", "product", "bom_edge", "customer", "sales_order_line", "purchase_order")
LEDGER_ENTITIES = ("supplier", "product", "customer", "sales_order_line", "purchase_order")  # pushed_item; BOM keeps bom_edge


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    # queued | running | done | error | unsupported
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    status_message: Mapped[Optional[str]] = mapped_column(Text)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    ref_number: Mapped[Optional[str]] = mapped_column(String(11))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class QBRecord(Base):
    __tablename__ = "qb_records"
    __table_args__ = (UniqueConstraint("entity", "qb_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(String(60))
    qb_id: Mapped[str] = mapped_column(String(64))  # TxnID or ListID
    edit_sequence: Mapped[Optional[str]] = mapped_column(Text)
    time_modified: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    level: Mapped[str] = mapped_column(String(10))
    entity: Mapped[Optional[str]] = mapped_column(String(60))
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)


class PoLink(Base):
    """PO write-back ledger: StockTrim PO <-> QuickBooks PO. The only data that must never be lost."""

    __tablename__ = "po_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    stocktrim_po_id: Mapped[str] = mapped_column(String(64), unique=True)
    stocktrim_reference: Mapped[Optional[str]] = mapped_column(String(64))
    qb_ref_number: Mapped[str] = mapped_column(String(11), unique=True)
    qb_txn_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    qb_edit_sequence: Mapped[Optional[str]] = mapped_column(String(32))
    # pending | created | acked | needs_review | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reason: Mapped[Optional[str]] = mapped_column(Text)
    payload_hash: Mapped[Optional[str]] = mapped_column(String(64))
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BomEdge(Base):
    """BOM edge last pushed to StockTrim (it cannot list edges, so this is what the BOM diff compares against)."""

    __tablename__ = "bom_edge"
    __table_args__ = (UniqueConstraint("target", "parent_sku", "component_sku"), CheckConstraint("quantity > 0", name="ck_bom_edge_quantity_positive"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    target: Mapped[str] = mapped_column(Text)  # stocktrim_target(settings): the StockTrim account this edge was pushed to
    parent_sku: Mapped[str] = mapped_column(Text)
    component_sku: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    parent_hash: Mapped[str] = mapped_column(Text)  # BomPlan fingerprint of the parent's edge set at push time
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushRun(Base):
    __tablename__ = "push_run"
    __table_args__ = (
        CheckConstraint(_in("status", PUSH_RUN_STATUSES), name="ck_push_run_status"),
        Index("ux_push_run_one_running", text("(1)"), unique=True, postgresql_where=text("status = 'running'")),  # at most one running run
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    target: Mapped[str] = mapped_column(Text)  # stocktrim_target(settings)
    requested_by: Mapped[Optional[str]] = mapped_column(Text)  # audit: who started the run
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=text("now()"))  # heartbeat: stale 'running' = interrupted
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # one of PUSH_RUN_STATUSES
    status: Mapped[str] = mapped_column(String(24), default="running")
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # steps, dry_run, policies
    preflight_fingerprint: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)  # counts only


class PushItem(Base):
    """Per-item progress of a push run (resumability). Keys only: never names or response bodies."""

    __tablename__ = "push_item"
    __table_args__ = (
        UniqueConstraint("run_id", "entity", "key"), Index("ix_push_item_run_entity_status", "run_id", "entity", "status"),
        CheckConstraint(_in("status", PUSH_ITEM_STATUSES), name="ck_push_item_status"),
        CheckConstraint(_in("entity", PUSH_ENTITIES), name="ck_push_item_entity"),
        CheckConstraint("attempts >= 0", name="ck_push_item_attempts_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("push_run.id", ondelete="CASCADE"))
    entity: Mapped[str] = mapped_column(Text)  # one of PUSH_ENTITIES
    key: Mapped[str] = mapped_column(Text)  # supplier code | SKU | bom_edge_key(parent, component) | customer ListID | TxnID:TxnLineID | QB-TxnID
    status: Mapped[str] = mapped_column(String(16), default="pending")  # one of PUSH_ITEM_STATUSES
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(Text)  # short code (rejected_400, unavailable, ...), never a body
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True))  # None = SQL NULL, not JSON null
    ref: Mapped[Optional[str]] = mapped_column(Text)  # QuickBooks ListID: what progress samples show (keys can be personal names)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PushedItem(Base):
    """Incremental-sync ledger: what we last pushed to a StockTrim target per record. payload_hash = sha256 of the exact request payload (NULL = unknown, re-sent once)."""

    __tablename__ = "pushed_item"
    __table_args__ = (CheckConstraint(_in("entity", LEDGER_ENTITIES), name="ck_pushed_item_entity"),)

    target: Mapped[str] = mapped_column(Text, primary_key=True)
    entity: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # supplier code | SKU | customer ListID
    ref: Mapped[Optional[str]] = mapped_column(Text)  # QuickBooks ListID
    payload_hash: Mapped[Optional[str]] = mapped_column(Text)
    last_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("push_run.id", ondelete="SET NULL"))
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
