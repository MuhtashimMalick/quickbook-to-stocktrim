"""Single home for persistence, used by tasks and the admin API."""
import logging
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from sqlalchemy import select

from .db import session_scope
from .models import Job, QBRecord, SyncLog, utcnow

logger = logging.getLogger("qb_poc.store")
TERMINAL = {"done", "error", "unsupported"}


def create_job(
    job_type: str,
    params: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    ref_number: Optional[str] = None,
) -> Job:
    with session_scope() as s:
        job = Job(job_type=job_type, params=params or {}, idempotency_key=idempotency_key, ref_number=ref_number)
        s.add(job)
        s.flush()
        return job


def get_job(job_id: int) -> Optional[Job]:
    with session_scope() as s:
        return s.get(Job, job_id)


def get_job_by_key(key: str) -> Optional[Job]:
    with session_scope() as s:
        return s.scalars(select(Job).where(Job.idempotency_key == key)).first()


def requeue_job(job_id: int) -> Job:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status, job.status_message, job.finished_at = "queued", None, None
        return job


def mark_running(job_id: int) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status, job.attempts = "running", job.attempts + 1


def set_job_status(job_id: int, status: str, message: Optional[str] = None, result: Optional[dict[str, Any]] = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status, job.status_message = status, message
        if result is not None:
            job.result = result
        if status in TERMINAL:
            job.finished_at = utcnow()


def fail_running_jobs(job_ids: Iterable[int], message: str) -> None:
    with session_scope() as s:
        for job in s.scalars(select(Job).where(Job.id.in_(list(job_ids)), Job.status == "running")):
            job.status, job.status_message, job.finished_at = "error", message, utcnow()


def queued_jobs() -> list[Job]:
    with session_scope() as s:
        return list(s.scalars(select(Job).where(Job.status == "queued").order_by(Job.id)))


def upsert_records(entity: str, records: Sequence[dict[str, Any]], job_id: Optional[int]) -> int:
    keyed = {}
    for rec in records:
        qb_id = rec.get("TxnID") or rec.get("ListID")
        if qb_id:
            keyed[qb_id] = rec
        else:
            log_event(job_id, "warning", entity, None, "record without TxnID/ListID skipped")
    with session_scope() as s:
        existing = {
            r.qb_id: r
            for r in s.scalars(select(QBRecord).where(QBRecord.entity == entity, QBRecord.qb_id.in_(list(keyed))))
        }
        for qb_id, rec in keyed.items():
            row = existing.get(qb_id) or QBRecord(entity=entity, qb_id=qb_id)
            row.edit_sequence, row.time_modified = rec.get("EditSequence"), rec.get("TimeModified")
            row.payload, row.job_id = rec, job_id
            s.add(row)
    return len(keyed)


def log_event(job_id: Optional[int], level: str, entity: Optional[str], status_code: Optional[int], message: str) -> None:
    logger.log(getattr(logging, level.upper()), "[job %s] %s %s: %s", job_id, entity or "-", status_code if status_code is not None else "", message)
    with session_scope() as s:
        s.add(SyncLog(job_id=job_id, level=level, entity=entity, status_code=status_code, message=message))


def list_jobs(status: Optional[str] = None, limit: int = 50) -> list[Job]:
    with session_scope() as s:
        q = select(Job).order_by(Job.id.desc()).limit(limit)
        return list(s.scalars(q.where(Job.status == status) if status else q))


def list_records(entities: Sequence[str], limit: int = 100, offset: int = 0) -> list[QBRecord]:
    with session_scope() as s:
        q = select(QBRecord).where(QBRecord.entity.in_(list(entities))).order_by(QBRecord.id).limit(limit).offset(offset)
        return list(s.scalars(q))


def first_record_id(entity: str) -> Optional[str]:
    rows = list_records([entity], limit=1)
    return rows[0].qb_id if rows else None


def list_log(limit: int = 100, job_id: Optional[int] = None) -> list[SyncLog]:
    with session_scope() as s:
        q = select(SyncLog).order_by(SyncLog.id.desc()).limit(limit)
        return list(s.scalars(q.where(SyncLog.job_id == job_id) if job_id is not None else q))
