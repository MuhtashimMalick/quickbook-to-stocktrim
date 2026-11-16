"""qb_records snapshot persistence."""
from collections.abc import Sequence
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from ..db import session_scope
from ..jobs import log_event
from ..models import QBRecord, utcnow


def upsert_records(entity: str, records: Sequence[dict[str, Any]], job_id: Optional[int]) -> int:
    """Bulk upsert on (entity, qb_id); one short transaction per call (called once per page, so memory stays flat)."""
    keyed = {}
    for rec in records:
        qb_id = rec.get("TxnID") or rec.get("ListID")
        if qb_id:
            keyed[qb_id] = rec
        else:
            log_event(job_id, "warning", entity, None, "record without TxnID/ListID skipped")  # outside the DB transaction below
    if not keyed:
        return 0
    now = utcnow()
    stmt = insert(QBRecord).values([
        {"entity": entity, "qb_id": qb_id, "edit_sequence": rec.get("EditSequence"), "time_modified": rec.get("TimeModified"),
         "payload": rec, "job_id": job_id, "synced_at": now}
        for qb_id, rec in keyed.items()
    ])
    stmt = stmt.on_conflict_do_update(
        index_elements=["entity", "qb_id"],
        set_={c: stmt.excluded[c] for c in ("edit_sequence", "time_modified", "payload", "job_id", "synced_at")},
    )
    with session_scope() as s:
        s.execute(stmt)
    return len(keyed)


def prune(entity: str, job_id: int) -> int:
    """Delete rows of `entity` not (re)written by read job `job_id`, i.e. no longer returned by QuickBooks.

    Call only after a FULL, successful read of the entity (every page). Windowed reads (future rolling-window
    SO/PO reads) set `QuerySpec.full_snapshot=False`, which skips this call.

    Commits are per page plus this final prune, not one transaction: consumers must read an entity's snapshot
    only after its read job status is `done`.
    """
    with session_scope() as s:
        return s.execute(delete(QBRecord).where(QBRecord.entity == entity, QBRecord.job_id.is_distinct_from(job_id))).rowcount


def list_records(entities: Sequence[str], limit: int = 100, offset: int = 0) -> list[QBRecord]:
    with session_scope() as s:
        q = select(QBRecord).where(QBRecord.entity.in_(list(entities))).order_by(QBRecord.id).limit(limit).offset(offset)
        return list(s.scalars(q))


def first_record_id(entity: str) -> Optional[str]:
    rows = list_records([entity], limit=1)
    return rows[0].qb_id if rows else None
