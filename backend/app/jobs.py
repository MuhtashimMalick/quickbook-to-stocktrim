"""Job queue and sync log persistence."""
import logging
from collections.abc import Collection, Iterable
from typing import Any, Optional

from sqlalchemy import select

from .db import session_scope
from .models import Job, SyncLog, utcnow

logger = logging.getLogger(__name__)
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


def recover_interrupted(write_job_types: Collection[str]) -> int:
    """Startup: nothing can still be running. Reads become error; writes are re-queued (idempotent by RefNumber)."""
    with session_scope() as s:
        stuck = list(s.scalars(select(Job).where(Job.status == "running")))
        for job in stuck:
            if job.job_type in write_job_types:
                job.status, job.status_message, job.finished_at = "queued", "requeued after restart", None
            else:
                job.status, job.status_message, job.finished_at = "error", "interrupted by restart", utcnow()
    if stuck:
        log_event(None, "warning", None, None, f"recovered {len(stuck)} interrupted job(s) after restart")
    return len(stuck)


def queued_jobs() -> list[Job]:
    with session_scope() as s:
        return list(s.scalars(select(Job).where(Job.status == "queued").order_by(Job.id)))


def list_jobs(status: Optional[str] = None, limit: int = 50) -> list[Job]:
    with session_scope() as s:
        q = select(Job).order_by(Job.id.desc()).limit(limit)
        return list(s.scalars(q.where(Job.status == status) if status else q))


def log_event(job_id: Optional[int], level: str, entity: Optional[str], status_code: Optional[int], message: str) -> None:
    logger.log(getattr(logging, level.upper()), "[job %s] %s %s: %s", job_id, entity or "-", status_code if status_code is not None else "", message)
    with session_scope() as s:
        s.add(SyncLog(job_id=job_id, level=level, entity=entity, status_code=status_code, message=message))


def list_log(limit: int = 100, job_id: Optional[int] = None) -> list[SyncLog]:
    with session_scope() as s:
        q = select(SyncLog).order_by(SyncLog.id.desc()).limit(limit)
        return list(s.scalars(q.where(SyncLog.job_id == job_id) if job_id is not None else q))
