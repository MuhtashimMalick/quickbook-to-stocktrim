"""Shared task machinery. Tasks only build qbXML and parse/store results."""
import logging
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, ClassVar, NamedTuple, Optional

from qbwc_kit import TaskContext, qbxml
from qbwc_kit.qbxml import QBXMLRequest, Response, ResponseSet

from .. import store
from ..config import Settings
from ..qbwc_service import RAW_RESPONSE_KEY

logger = logging.getLogger("qb_poc.tasks")

# Real QB says 3250 for "feature not enabled" (e.g. Advanced Inventory off); qbwc-kit's fake says 3100.
UNSUPPORTED_STATUS_CODES = {3100, 3250}
MAX_PAGES = 1000


class Outcome(NamedTuple):
    status: str  # done | error | unsupported
    message: Optional[str] = None
    result: Optional[dict[str, Any]] = None


Flow = Generator[QBXMLRequest, ResponseSet, Outcome]


def as_list(value: Any) -> list[Any]:
    """The parser yields a dict for one child and a list for repeats."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def negotiate_version(ctx: TaskContext, cap: str = "16.0") -> str:
    if ctx.major_version <= 0:
        return "13.0"  # QuickBooks did not tell us its version
    cap_t = tuple(int(p) for p in cap.split("."))
    return "%d.%d" % min((ctx.major_version, ctx.minor_version), cap_t)


class JobTask:
    job_type: ClassVar[str]
    is_write: ClassVar[bool] = False

    def __init__(self, job_id: int, params: Optional[dict[str, Any]], settings: Settings) -> None:
        self.job_id, self.params, self.settings = job_id, params or {}, settings
        self.name = f"{self.job_type}#{job_id}"
        self._saved: set[str] = set()

    def execute(self, ctx: TaskContext) -> Flow:
        raise NotImplementedError

    def run(self, ctx: TaskContext) -> Generator[QBXMLRequest, ResponseSet, None]:
        store.mark_running(self.job_id)
        try:
            outcome = yield from self.execute(ctx)
        except Exception as exc:  # never crash the session
            logger.exception("task %s failed", self.name)
            outcome = Outcome("error", f"{type(exc).__name__}: {exc}")
        store.set_job_status(self.job_id, outcome.status, outcome.message, outcome.result)
        store.log_event(self.job_id, "info" if outcome.status == "done" else "warning", None, None,
                        f"job {self.name} -> {outcome.status} {outcome.message or ''}".strip())

    def send(self, ctx: TaskContext, request: qbxml.builder.Request, fixture: str) -> Generator[QBXMLRequest, ResponseSet, Optional[Response]]:
        """Send one request; save first-page fixtures; return the response (None if QB sent nothing)."""
        version = negotiate_version(ctx, self.settings.max_qbxml_version)
        logger.info("[job %s] %s using qbXML %s (QuickBooks reports %s.%s)", self.job_id, request.name, version, ctx.major_version, ctx.minor_version)
        batch = QBXMLRequest([request], version=version)
        rs = yield batch
        raw = ctx.state.pop(RAW_RESPONSE_KEY, "")
        if self.settings.save_fixtures and fixture not in self._saved:
            self._saved.add(fixture)
            d = self.settings.fixtures_dir
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{fixture}_rq.xml").write_text(batch.render(), encoding="utf-8")
            (d / f"{fixture}_rs.xml").write_text(raw, encoding="utf-8")
        return rs.first() if len(rs) else None

    def failure(self, entity: str, rs: Response) -> Outcome:
        status = "unsupported" if rs.status_code in UNSUPPORTED_STATUS_CODES else "error"
        store.log_event(self.job_id, "warning", entity, rs.status_code, f"{status}: {rs.status_message}")
        return Outcome(status, f"[{rs.status_code}] {rs.status_message}")


@dataclass
class QuerySpec:
    entity: str
    extra: Optional[dict[str, Any]] = None
    paginate: bool = True


class QueryTask(JobTask):
    specs: ClassVar[list[QuerySpec]]

    def after_fetch(self, ctx: TaskContext, records: dict[str, list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
        return None

    def fetch(self, ctx: TaskContext, spec: QuerySpec) -> Generator[QBXMLRequest, ResponseSet, "tuple[Optional[Outcome], list[dict[str, Any]]]"]:
        paging = {"max_returned": self.settings.page_size, "iterator": "Start"} if spec.paginate else {}
        request = qbxml.query(spec.entity, extra=spec.extra, **paging)
        records: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            rs = yield from self.send(ctx, request, spec.entity)
            if rs is None:
                return Outcome("error", "empty response from QuickBooks"), records
            if rs.status_code == 0:
                store.upsert_records(spec.entity, rs.records, self.job_id)
                records += rs.records
            elif rs.status_code == 1:
                store.log_event(self.job_id, "info", spec.entity, 1, "nothing found")
            else:
                return self.failure(spec.entity, rs), records
            if not (spec.paginate and rs.has_more):
                return None, records
            request.iterator, request.iterator_id = "Continue", rs.iterator_id
        return Outcome("error", f"gave up after {MAX_PAGES} pages"), records

    def execute(self, ctx: TaskContext) -> Flow:
        fetched: dict[str, list[dict[str, Any]]] = {}
        for spec in self.specs:
            failure, fetched[spec.entity] = yield from self.fetch(ctx, spec)
            if failure:
                return failure
        result: dict[str, Any] = {"counts": {e: len(r) for e, r in fetched.items()}}
        result.update(self.after_fetch(ctx, fetched) or {})
        return Outcome("done", None, result)
