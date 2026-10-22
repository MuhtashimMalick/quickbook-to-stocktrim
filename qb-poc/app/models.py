from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class QBRecord(Base):
    __tablename__ = "qb_records"
    __table_args__ = (UniqueConstraint("entity", "qb_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(60), index=True)
    qb_id: Mapped[str] = mapped_column(String(64))  # TxnID or ListID
    edit_sequence: Mapped[Optional[str]] = mapped_column(String(32))
    time_modified: Mapped[Optional[str]] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"), index=True)
    level: Mapped[str] = mapped_column(String(10))
    entity: Mapped[Optional[str]] = mapped_column(String(60))
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
