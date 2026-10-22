import logging
import secrets

from qbwc_kit import Session

from . import store
from .config import Settings
from .tasks import JOB_TYPES, WRITE_JOB_TYPES, JobTask

logger = logging.getLogger("qb_poc.auth")


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class DbAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(self, username: str, password: str) -> bool:
        user_ok = _eq(username, self.settings.qbwc_username)
        pass_ok = _eq(password, self.settings.qbwc_password.get_secret_value())
        return user_ok and pass_ok

    def tasks_for(self, username: str) -> list[JobTask]:
        """Queued jobs, write jobs first, then by id. (Single connector assumed.)"""
        tasks: list[JobTask] = []
        for job in sorted(store.queued_jobs(), key=lambda j: (j.job_type not in WRITE_JOB_TYPES, j.id)):
            cls = JOB_TYPES.get(job.job_type)
            if cls is None:
                store.set_job_status(job.id, "error", f"unknown job type {job.job_type!r}")
            elif cls.is_write and not self.settings.writes_enabled:
                store.set_job_status(job.id, "error", "writes disabled")
            else:
                tasks.append(cls(job.id, job.params, self.settings))
        return tasks


def end_session(session: Session) -> None:
    """L3 workaround: a QB hresult leaves the task suspended forever; fail whatever is still running."""
    ids = [t.job_id for t in session.tasks if isinstance(t, JobTask)]
    store.fail_running_jobs(ids, session.last_error() or "session ended before task completed")
