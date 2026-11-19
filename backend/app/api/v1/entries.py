"""Push run list and the per-run "entries" view (failed / skipped items + excluded records), as JSON and as a CSV download. Keys, ListIDs, codes and a display
label looked up in the QuickBooks snapshot at READ time (never stored); never payloads, messages or response bodies."""
import csv
import io
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, Text, case, cast, exists, func, literal_column, null, select, union_all
from sqlalchemy.dialects.postgresql import JSONB

from ...db import session_scope
from ...models import PushExclusion, PushItem, PushRun, QBRecord
from ...reasons import describe
from ...sync import engine
from .ops import M
from .push import _call
from .reads import ReasonOut, before_clause

router = APIRouter(prefix="/push")
OUTCOMES = ("failed", "skipped", "excluded")
MAX_CSV_ROWS = 200_000
# QuickBooks entities a push key or ref can point at (ListIDs and TxnIDs are unique across them, so the lookup needs no per-row entity rule and can use the (entity, qb_id) index)
QB_ENTITIES = ("Vendor", "Customer", "ItemInventory", "ItemInventoryAssembly", "ItemNonInventory", "ItemService", "ItemOtherCharge", "SalesOrder", "PurchaseOrder")
_LABEL = func.coalesce(QBRecord.payload["FullName"].as_string(), QBRecord.payload["Name"].as_string(), QBRecord.payload["RefNumber"].as_string())


# ---- run list
class RunListItem(M):
    id: UUID
    mode: Optional[str]
    dry_run: bool
    status: str
    steps: list[str]
    counts: dict[str, int]  # items by status, all steps
    started_at: datetime
    finished_at: Optional[datetime]
    requested_by: Optional[str]
    target: Optional[str]  # "<environment> @ <host>": no credentials


@router.get("/runs", response_model=list[RunListItem])
def list_runs(limit: int = Query(50, ge=1, le=100), before: Optional[str] = Query(None, max_length=64), dry_run: Optional[bool] = None) -> Any:
    with session_scope() as s:
        q = select(PushRun).where(*before_clause(s, PushRun, before)).order_by(PushRun.started_at.desc(), PushRun.id.desc()).limit(limit)
        if dry_run is not None:
            q = q.where(PushRun.options["dry_run"].as_boolean().is_(dry_run))
        rows = s.scalars(q).all()
        by: dict[Any, dict[str, int]] = {}
        for rid, st, n in s.execute(select(PushItem.run_id, PushItem.status, func.count()).where(PushItem.run_id.in_([r.id for r in rows])).group_by(PushItem.run_id, PushItem.status)):
            by.setdefault(rid, {})[st] = n
    return [RunListItem(id=r.id, mode=r.options.get("mode"), dry_run=bool(r.options.get("dry_run")), status=r.status, steps=r.options.get("steps", []), counts=by.get(r.id, {}),
                        started_at=r.started_at, finished_at=r.finished_at, requested_by=r.requested_by, target=engine.target_label(r.target)) for r in rows]


# ---- entries
class EntryOut(M):
    source: Literal["item", "exclusion"]
    outcome: Literal["failed", "skipped", "excluded"]
    entity: str
    action: Optional[str]
    key: str
    ref: Optional[str]
    qb_label: Optional[str]  # display name from the current QuickBooks snapshot; null when the record is gone
    code: Optional[str]
    reason: ReasonOut
    http_status: Optional[int]
    fields: list[str]  # field names StockTrim rejected
    attempts: Optional[int]
    created_at: datetime
    updated_at: datetime
    run_id: UUID


class EntriesOut(M):
    items: list[EntryOut]
    total: int  # under the outcome filter
    counts: dict[str, int]  # per outcome, under the other filters
    entities: list[str]  # facet for the entity dropdown: distinct entities under every filter except `entity` itself, sorted


class Filters:
    def __init__(self, outcome: list[Literal["failed", "skipped", "excluded"]] = Query([]), entity: Optional[str] = Query(None, max_length=40),
                 code: Optional[str] = Query(None, max_length=60), q: Optional[str] = Query(None, max_length=100),
                 key: Optional[str] = Query(None, max_length=200)):
        self.outcome, self.entity, self.code, self.q, self.key = outcome, entity, code, q, key


def _union(run_id: UUID) -> Any:
    lit = literal_column
    items = select(lit("'item'").label("source"), case((PushItem.status == "failed", lit("'failed'")), else_=lit("'skipped'")).label("outcome"), PushItem.id, PushItem.entity, PushItem.action,
                   PushItem.key, PushItem.ref, PushItem.error_code.label("code"), PushItem.detail, PushItem.attempts, PushItem.created_at, PushItem.updated_at).where(
        PushItem.run_id == run_id, (PushItem.status == "failed") | ((PushItem.status == "skipped") & PushItem.error_code.in_(("aborted", "cancelled"))))
    excl = select(lit("'exclusion'"), lit("'excluded'"), PushExclusion.id, PushExclusion.entity, cast(null(), Text), PushExclusion.key, PushExclusion.ref, PushExclusion.code, cast(null(), JSONB),
                  cast(null(), Integer), PushExclusion.created_at, PushExclusion.created_at).where(PushExclusion.run_id == run_id)
    return union_all(items, excl).subquery("e")


def _where(u: Any, f: Filters, outcome: bool, entity: bool = True) -> list[Any]:
    w = []
    if outcome and f.outcome:
        w.append(u.c.outcome.in_(f.outcome))
    if entity and f.entity:
        w.append(u.c.entity == f.entity)
    if f.code:
        w.append(u.c.code == f.code)
    if f.key:  # an exact key lookup wins over the substring search
        w.append(u.c.key == f.key)
    elif f.q:  # wildcards in the search text are escaped; the label is only searched when key and ref do not match
        w.append(u.c.key.icontains(f.q, autoescape=True) | u.c.ref.icontains(f.q, autoescape=True)
                 | exists().where(QBRecord.entity.in_(QB_ENTITIES), QBRecord.qb_id == u.c.ref, _LABEL.icontains(f.q, autoescape=True)))
    return w


def _page_query(u: Any, f: Filters) -> Any:
    return select(u).where(*_where(u, f, True)).order_by(u.c.updated_at.desc(), u.c.source, u.c.id.desc())  # source+id: a stable tiebreak across the two tables


def _labels(s: Any, refs: Any) -> dict[str, Optional[str]]:
    """ONE query for a whole page."""
    refs = {r for r in refs if r}
    return dict(s.execute(select(QBRecord.qb_id, _LABEL).where(QBRecord.entity.in_(QB_ENTITIES), QBRecord.qb_id.in_(refs))).all()) if refs else {}


def _counts(s: Any, u: Any, f: Filters) -> tuple[dict[str, int], int]:
    c = dict.fromkeys(OUTCOMES, 0) | dict(s.execute(select(u.c.outcome, func.count()).where(*_where(u, f, False)).group_by(u.c.outcome)).all())
    return c, sum(c[o] for o in (f.outcome or OUTCOMES))


def _entry(r: Any, run_id: UUID, label: Optional[str]) -> EntryOut:
    d = r.detail or {}
    return EntryOut(source=r.source, outcome=r.outcome, entity=r.entity, action=r.action, key=r.key, ref=r.ref, qb_label=label, code=r.code, reason=describe(r.code or ""),  # type: ignore[arg-type]
                    http_status=d.get("status"), fields=[str(x) for x in d.get("fields") or []], attempts=r.attempts, created_at=r.created_at, updated_at=r.updated_at, run_id=run_id)


@router.get("/runs/{run_id}/entries", response_model=EntriesOut)
def entries(run_id: UUID, f: Filters = Depends(), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> Any:
    _call(engine._run_row, run_id)
    u = _union(run_id)
    with session_scope() as s:
        counts, total = _counts(s, u, f)
        rows = s.execute(_page_query(u, f).limit(limit).offset(offset)).all()
        labels = _labels(s, (r.ref for r in rows))
        entities = list(s.scalars(select(u.c.entity).where(*_where(u, f, True, entity=False)).distinct().order_by(u.c.entity)))
    return EntriesOut(items=[_entry(r, run_id, labels.get(r.ref)) for r in rows], total=total, counts=counts, entities=entities)


# ---- CSV
HEADER = ("outcome", "entity", "action", "key", "ref", "qb_label", "code", "reason", "http_status", "fields", "attempts", "updated_at")


def _cell(v: Any) -> Any:
    """Spreadsheet formula injection: a text cell that starts with = + - @ tab or CR gets a leading quote."""
    if v is None:
        return ""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _csv_stream(run_id: UUID, f: Filters, cap: int) -> Any:
    buf = io.StringIO(newline="")
    w = csv.writer(buf)

    def flush() -> bytes:
        out = buf.getvalue().encode()
        buf.seek(0)
        buf.truncate()
        return out

    w.writerow(HEADER)
    yield b"\xef\xbb\xbf" + flush()  # UTF-8 BOM: Excel
    u = _union(run_id)
    with session_scope() as s:
        for part in s.execute(_page_query(u, f).limit(cap).execution_options(yield_per=1000)).partitions():
            labels = _labels(s, (r.ref for r in part))
            for r in part:
                e = _entry(r, run_id, labels.get(r.ref))
                w.writerow([_cell(c) for c in (e.outcome, e.entity, e.action, e.key, e.ref, e.qb_label, e.code, e.reason.title, e.http_status, ";".join(e.fields), e.attempts, e.updated_at.isoformat())])
            yield flush()


@router.get("/runs/{run_id}/entries.csv")
def entries_csv(run_id: UUID, f: Filters = Depends()) -> StreamingResponse:
    _call(engine._run_row, run_id)
    with session_scope() as s:
        _, total = _counts(s, _union(run_id), f)
    return StreamingResponse(_csv_stream(run_id, f, MAX_CSV_ROWS), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="entries-{str(run_id)[:8]}.csv"', "X-Truncated": str(total > MAX_CSV_ROWS).lower(), "Cache-Control": "no-store"})
