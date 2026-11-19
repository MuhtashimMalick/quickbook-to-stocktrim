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
CYCLE_STATUSES = ("running", "done", "dry_run_only", "aborted", "failed", "interrupted")
CYCLE_TRIGGERS = ("schedule", "manual")
PUSH_ITEM_STATUSES = ("pending", "done", "failed", "skipped")
PUSH_ENTITIES = ("supplier", "product", "bom_edge", "customer", "sales_order_line", "purchase_order")
LEDGER_ENTITIES = ("supplier", "product", "customer", "sales_order_line", "purchase_order")  # pushed_item; BOM keeps bom_edge
PUSH_ACTIONS = ("upsert", "delete")
RUN_EVENT_LEVELS = ("info", "warning", "error")
# Why a cycle ended (sync_cycle.reason) ...
CYCLE_REASONS = ("read_timeout", "read_job_failed", "empty_entity", "stale_snapshot", "push_running", "foreign_records", "preflight_not_ok", "mass_vanish", "too_many_calls",
                 "writes_disabled", "production_not_allowed", "shutdown", "lock_lost", "push_done_with_failures", "push_failed", "push_interrupted", "push_cancelled", "restart", "stale", "crashed")
# ... and every PushError code (HTTP detail of the push routes; as a cycle reason when engine.start refuses). tests/test_reasons.py greps the engine source, so a new one cannot be forgotten.
PUSH_ERROR_CODES = ("run_not_found", "stamp_has_no_bom_step", "stamp_test_only", "shutting_down", "stocktrim_writes_disabled", "preflight_fingerprint_required", "plan_fingerprint_required",
                    "push_running", "no_completed_initial_run", "stamp_needs_sync_mode", "step_already_pushed", "acknowledge_foreign_mismatch", "foreign_in_plan_production", "preflight_not_ok",
                    "preflight_fingerprint_mismatch", "mass_delete", "requeue_requires_step", "mass_requeue", "stamp_snapshot_newer", "plan_fingerprint_mismatch", "stamp_requires_all_present",
                    "run_not_resumable", "target_mismatch", "run_not_active")
EVENT_CODES = ("cycle_started", "reads_queued", "waiting_for_web_connector", "reads_done", "snapshot_fresh", "dry_run_planned", "real_run_started", "cycle_finished",
               "run_started", "step_started", "step_finished", "run_finished", "preflight_requested", "run_cancel_requested", "run_resumed", "schedule_changed",
               *(f"check_failed:{r}" for r in dict.fromkeys((*CYCLE_REASONS, *PUSH_ERROR_CODES))))
# What push_exclusion.code can hold: the domain Issue codes that keep a record (or a BOM line / supplier link) out of the plan, plus the mappers' skip reasons.
EXCLUSION_ISSUE_CODES = ("empty_key", "whitespace_key", "control_chars", "duplicate_key", "empty_list_id", "parse_error", "inactive_supplier", "inactive_customer", "unresolved_vendor",
                         "bom_cycle", "bom_cycle_ancestor", "bom_component_excluded", "bom_component_unresolved", "bom_self_reference", "nonpositive_quantity", "bom_quantity_precision")
EXCLUSION_CODES = (*EXCLUSION_ISSUE_CODES, "non_product", "group", "zero_quantity", "closed_uninvoiced", "vendor_not_pushed", "no_lines", "lines_non_product")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})".replace(":", r"\:")  # SQLAlchemy reads a bare ':name' in a CHECK as a bind parameter


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
        CheckConstraint("attempts >= 0", name="ck_push_item_attempts_nonneg"), CheckConstraint(_in("action", PUSH_ACTIONS), name="ck_push_item_action"),
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
    action: Mapped[Optional[str]] = mapped_column(Text)  # one of PUSH_ACTIONS (NULL on rows from before stage B3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PushExclusion(Base):
    """A record the plan leaves out (or a skipped order line / dropped link), per run: business key + QuickBooks ListID/TxnID + reason code. Never names, messages or payloads."""

    __tablename__ = "push_exclusion"
    __table_args__ = (UniqueConstraint("run_id", "entity", "key", "code"), CheckConstraint(_in("code", EXCLUSION_CODES), name="ck_push_exclusion_code"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("push_run.id", ondelete="CASCADE"))
    entity: Mapped[str] = mapped_column(Text)  # one of PUSH_ENTITIES
    key: Mapped[str] = mapped_column(Text)
    ref: Mapped[Optional[str]] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)  # one of EXCLUSION_CODES
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=text("now()"))


class RunEvent(Base):
    """Phase log of a scheduler cycle and/or push run: code + counts (ints) only."""

    __tablename__ = "run_event"
    __table_args__ = (
        Index("ix_run_event_cycle", "cycle_id", "id"), Index("ix_run_event_run", "run_id", "id"),
        CheckConstraint(_in("level", RUN_EVENT_LEVELS), name="ck_run_event_level"), CheckConstraint(_in("code", EVENT_CODES), name="ck_run_event_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=text("now()"))
    cycle_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("sync_cycle.id", ondelete="CASCADE"))
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("push_run.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(8), default="info")
    code: Mapped[str] = mapped_column(Text)  # one of EVENT_CODES
    entity: Mapped[Optional[str]] = mapped_column(Text)
    counts: Mapped[Optional[dict[str, int]]] = mapped_column(JSONB(none_as_null=True))
    actor: Mapped[Optional[str]] = mapped_column(Text)  # username | scheduler | cli


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


class SyncCycle(Base):
    """One scheduler cycle (reads -> freshness -> dry-run -> real run). reason = short code, summary = counts: never record contents."""

    __tablename__ = "sync_cycle"
    __table_args__ = (
        CheckConstraint(_in("status", CYCLE_STATUSES), name="ck_sync_cycle_status"),
        CheckConstraint(_in("trigger", CYCLE_TRIGGERS), name="ck_sync_cycle_trigger"),
        Index("ux_sync_cycle_one_running", text("(1)"), unique=True, postgresql_where=text("status = 'running'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # one of CYCLE_STATUSES
    reason: Mapped[Optional[str]] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(16))  # one of CYCLE_TRIGGERS
    push_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("push_run.id", ondelete="SET NULL"))
    summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=text("now()"))


class AppState(Base):
    """Small key/value store: last_qbwc_contact, last_cycle."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=text("now()"))


class AppUser(Base):
    """Admin-UI login. username is stored lowercase (CHECK); password_hash is argon2id."""

    __tablename__ = "app_user"
    __table_args__ = (CheckConstraint("username ~ '^[a-z0-9._-]{3,40}$'", name="ck_app_user_username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    tokens_valid_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # access tokens issued before this are rejected (passwd/disable)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=text("now()"))


class RefreshToken(Base):
    """Only the sha256 of the cookie value is stored. Rotated on every use; presenting a revoked row revokes all of the user's tokens."""

    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # set only when a refresh rotated it (logout/passwd revoke without it)
    replaced_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("refresh_token.id", ondelete="SET NULL"))
    ip: Mapped[Optional[str]] = mapped_column(Text)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
