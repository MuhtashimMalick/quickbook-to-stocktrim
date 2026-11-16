from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


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
