"""Push engine (Phase 2, stage 9): suppliers then products to StockTrim, resumable, one run at a time. See README "Push engine".
Leak rules: push_item / summary / progress carry keys (supplier code, SKU), QuickBooks ListIDs, counts and error codes ONLY (never names,
messages or response bodies); logs carry counts only. The plan lives in memory only; worker functions return nothing but a status."""
import hashlib
import json
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import case, func, insert, null, select, update
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import session_scope
from ..domain.bom import plan_bom
from ..domain.from_qb import load_snapshot
from ..domain.keys import resolve_customers, resolve_products, resolve_suppliers
from ..models import PushItem, PushRun, utcnow
from ..preflight.report import PreflightOptions, build_report
from ..stocktrim.api import StockTrimApi
from ..stocktrim.client import BulkItem, StockTrimClient
from ..stocktrim.dto import ProductRequest, ProductResponse
from ..stocktrim.errors import (
    StockTrimAborted,
    StockTrimAuthError,
    StockTrimBadResponse,
    StockTrimConfigError,
    StockTrimError,
    StockTrimRejected,
    StockTrimUnavailable,
)
from ..stocktrim.target import stocktrim_target
from .mappers import PushPlan, build_push_plan

log = logging.getLogger("app.push")
STEPS = ("suppliers", "products")  # push order; the other steps arrive in stage 10
ENTITY = {"suppliers": "supplier", "products": "product"}
CHUNK = 50
SAMPLE_LIMIT = 50
READBACK_SAMPLE = 25
# Closed set of push_item.error_code values. A not-attempted item is status `skipped` + `aborted` (never `pending`, so a resume retries it).
ERROR_CODES = frozenset({"rejected_400", "rejected_404", "rejected_409", "rejected_other", "auth", "unavailable", "unknown_outcome", "aborted",
                         "negative_stock_rejected", "supplier_link_dropped", "dependency_failed", "cancelled", "internal"})
_FATAL = (StockTrimUnavailable, StockTrimConfigError, StockTrimAuthError)


class PushError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


class _Cancelled(StockTrimAborted):
    """Raised inside a chunk when cancel was requested; the item stays `pending`. (An Aborted subclass: map_concurrent does not log it.)"""


class PlanChanged(Exception):
    """The plan built by the run thread differs from the plan_fingerprint the caller approved."""


_ACTIVE = threading.Lock()  # ponytail: one run per process; the DB partial unique index is the second line of defence
_CANCELS: dict[UUID, threading.Event] = {}
_thread: Optional[threading.Thread] = None


def join(timeout: Optional[float] = None) -> None:
    """Wait for the background run (tests, shutdown)."""
    if _thread:
        _thread.join(timeout)


def shutdown(timeout: float = 8.0) -> None:
    """App shutdown: ask every active run to cancel (it ends `cancelled`, resumable) and wait for the worker."""
    for ev in list(_CANCELS.values()):
        ev.set()
    join(timeout)


# ---------------------------------------------------------------- plan
@dataclass
class Built:
    plan: PushPlan
    list_ids: dict[str, str]  # product SKU -> QuickBooks ListID


def build_plan(settings: Settings) -> Built:
    with session_scope() as s:
        snap = load_snapshot(s, settings.product_key_field)
    sup = resolve_suppliers(snap.suppliers)
    prod = resolve_products(snap.products, sup)
    bom = plan_bom(prod, snap.assemblies, unusable_list_ids={i.ref for i in snap.issues})
    plan = build_push_plan(sup, prod, resolve_customers(snap.customers), bom, settings.negative_stock_policy)
    return Built(plan, {p.sku: p.list_id for p in prod.ok})


def plan_fingerprint(plan: PushPlan, steps: list[str]) -> str:
    """sha256 over the sorted planned keys of the selected steps and a hash of each request payload."""
    h = hashlib.sha256()
    for step in steps:
        for key, req in sorted(getattr(plan, step), key=lambda x: x[0]):
            h.update(json.dumps([step, key, hashlib.sha256(json.dumps(req.payload(), sort_keys=True).encode()).hexdigest()]).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- error handling
def _error_fields(body: str) -> list[str]:
    """FIELD names of a 400 `errors` object (e.g. StockTrimOnHand); never messages or values."""
    try:
        errs = json.loads(body).get("errors")
        return sorted(str(k) for k in errs)[:10] if isinstance(errs, dict) else []
    except (ValueError, AttributeError):
        return []


def classify(e: Exception) -> tuple[str, dict[str, Any]]:
    """(error_code, detail) with detail = status code + field names or exception type name only."""
    if isinstance(e, StockTrimAuthError):
        return "auth", {"status": e.status}
    if isinstance(e, StockTrimRejected):
        return {400: "rejected_400", 404: "rejected_404", 409: "rejected_409"}.get(e.status, "rejected_other"), {"status": e.status, "fields": _error_fields(e.body)}
    if isinstance(e, StockTrimUnavailable):
        return "unavailable", {}
    if isinstance(e, StockTrimBadResponse):  # 2xx we cannot parse: the write probably happened
        return "unknown_outcome", {"status": e.status}
    return "internal", {"exc": type(e).__name__}


def _negative_stock_error(e: StockTrimRejected) -> bool:
    """A 400 whose error FIELDS name the on-hand stock (observed live: StockTrim stores negatives, so this is a safety net)."""
    return e.status == 400 and any("onhand" in f.lower() for f in _error_fields(e.body))


# ---------------------------------------------------------------- db helpers
def _mark(run_id: UUID, entity: str, keys: list[str], status: str, code: Optional[str] = None, detail: Optional[dict] = None) -> None:
    """One short transaction."""
    with session_scope() as s:
        s.execute(update(PushItem).where(PushItem.run_id == run_id, PushItem.entity == entity, PushItem.key.in_(keys))
                  .values(status=status, error_code=code, detail=detail or null(), attempts=PushItem.attempts + 1, updated_at=utcnow()))


def _keys(run_id: UUID, entity: str, *, done: bool) -> list[str]:
    with session_scope() as s:
        cond = PushItem.status == "done" if done else PushItem.status != "done"
        return list(s.scalars(select(PushItem.key).where(PushItem.run_id == run_id, PushItem.entity == entity, cond).order_by(PushItem.id)))


def _beat(run_id: UUID) -> None:
    with session_scope() as s:
        s.execute(update(PushRun).where(PushRun.id == run_id).values(updated_at=utcnow()))


def _insert_items(run_id: UUID, built: Built, steps: list[str]) -> None:
    ref = {"suppliers": lambda k, req: req.external_id, "products": lambda k, req: built.list_ids[k]}  # QuickBooks ListIDs
    rows = [{"run_id": run_id, "entity": ENTITY[st], "key": k, "ref": ref[st](k, req)} for st in steps for k, req in getattr(built.plan, st)]
    with session_scope() as s:
        for i in range(0, len(rows), 1000):
            s.execute(insert(PushItem), rows[i:i + 1000])


def _has_pending(run_id: UUID) -> bool:
    with session_scope() as s:
        return s.scalar(select(PushItem.id).where(PushItem.run_id == run_id, PushItem.status == "pending").limit(1)) is not None


def _failures(run_id: UUID) -> int:
    """Items that ended `failed`, or were never attempted because the run aborted."""
    with session_scope() as s:
        return s.scalar(select(func.count()).where(PushItem.run_id == run_id, (PushItem.status == "failed") | ((PushItem.status == "skipped") & (PushItem.error_code == "aborted")))) or 0


def _done_suppliers(target: str) -> set[str]:
    """Supplier keys that are `done` in any run on this StockTrim target (this one included; dry-runs have no items)."""
    with session_scope() as s:
        return set(s.scalars(select(PushItem.key).join(PushRun, PushRun.id == PushItem.run_id).where(PushRun.target == target, PushItem.entity == "supplier", PushItem.status == "done")))


def _run_row(run_id: UUID) -> PushRun:
    with session_scope() as s:
        row = s.get(PushRun, run_id)
        if row is None:
            raise PushError(404, "run_not_found")
        s.expunge(row)
        return row


# ---------------------------------------------------------------- execution
class _Run:
    def __init__(self, run_id: UUID, built: Built, api: StockTrimApi, cancel: threading.Event, target: str):
        self.id, self.built, self.api, self.cancel, self.target = run_id, built, api, cancel, target
        self.reqs = {"supplier": dict(built.plan.suppliers), "product": dict(built.plan.products)}
        self.neg_ids = set(built.plan.negative_stock_list_ids)
        self.clamp = threading.Event()  # set after the first negative-stock rejection: the rest of the run is clamped
        self.fatal: Optional[str] = None
        self.extra: dict[str, Any] = {}

    def run(self, steps: list[str]) -> str:
        if _rejected_keys(self.id):
            self.clamp.set()  # a resumed run keeps the clamp decision
        for step in steps:
            if out := getattr(self, "_" + step)():
                return out
        if self.cancel.is_set() and _has_pending(self.id):  # e.g. cancelled during the last supplier chunk's per-supplier fallback
            return "cancelled"
        if "products" in steps:
            self._readback()
        return "done"

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise _Cancelled

    def _settle(self, entity: str, results: list[BulkItem]) -> None:
        """Record the failed items of a chunk (successful ones were marked done by their own worker)."""
        for r in results:
            if r.ok or isinstance(r.error, _Cancelled):
                continue
            if isinstance(r.error, StockTrimAborted):
                _mark(self.id, entity, [r.item], "skipped", "aborted")
                continue
            code, detail = classify(r.error)  # type: ignore[arg-type]
            _mark(self.id, entity, [r.item], "failed", code, detail)

    def _fatal(self, entity: str, e: Exception, items: Optional[list[BulkItem]] = None) -> str:
        """Fatal error (auth, unavailable, config): stop the run, never mark an unattempted item as failed or done."""
        self.fatal = classify(e)[0]
        self._settle(entity, (e.partial_results or []) if items is None else items)
        with session_scope() as s:
            s.execute(update(PushItem).where(PushItem.run_id == self.id, PushItem.status == "pending").values(status="skipped", error_code="aborted", updated_at=utcnow()))
        return "failed"

    # suppliers: chunks of 50 through the array endpoint; a chunk failure falls back to one POST per supplier to isolate the bad record
    def _suppliers(self) -> Optional[str]:
        keys = _keys(self.id, "supplier", done=False)
        for i in range(0, len(keys), CHUNK):
            if self.cancel.is_set():
                return "cancelled"
            chunk = keys[i:i + CHUNK]
            _beat(self.id)
            try:
                self.api.post_suppliers([self.reqs["supplier"][k] for k in chunk])
                _mark(self.id, "supplier", chunk, "done")
                continue
            except _FATAL as e:
                code, detail = classify(e)  # the array call failed as a whole: every key of it is unproven
                _mark(self.id, "supplier", chunk, "failed", code, detail)
                return self._fatal("supplier", e, [])
            except StockTrimError:
                pass  # ponytail: any non-fatal chunk failure (400, bad body) is isolated per supplier; the posts are idempotent upserts

            def one(k: str) -> None:
                self._check_cancel()
                self.api.post_suppliers([self.reqs["supplier"][k]])
                _mark(self.id, "supplier", [k], "done")

            try:
                self._settle("supplier", self.api.map_concurrent(one, chunk))
            except _FATAL as e:
                return self._fatal("supplier", e)
        return None

    # products: one POST per product (V1 upsert), concurrent inside a chunk
    def _products(self) -> Optional[str]:
        done_suppliers = _done_suppliers(self.target)
        with session_scope() as s:  # a resume re-pushes, WITH the link, the products that were pushed without it while their supplier is now done
            dropped = s.scalars(select(PushItem.key).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.status == "done",
                                                           PushItem.detail["supplier_link_dropped"].as_boolean().is_(True))).all()
            if relink := [k for k in dropped if self.reqs["product"][k].supplier_code in done_suppliers]:
                s.execute(update(PushItem).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.key.in_(relink))
                          .values(status="pending", error_code=None, detail=null(), updated_at=utcnow()))
        keys = _keys(self.id, "product", done=False)
        for i in range(0, len(keys), CHUNK):
            if self.cancel.is_set():
                return "cancelled"
            _beat(self.id)
            try:
                self._settle("product", self.api.map_concurrent(lambda k: self._one_product(k, done_suppliers), keys[i:i + CHUNK]))
            except _FATAL as e:
                return self._fatal("product", e)
        return None

    def _one_product(self, key: str, done_suppliers: set[str]) -> None:
        self._check_cancel()
        req, code, detail = self.reqs["product"][key], None, {}
        if req.supplier_code and req.supplier_code not in done_suppliers:  # StockTrim would auto-create an unknown supplier: never link to one that is not done
            req, code, detail = req.model_copy(update={"supplier_code": None, "suppliers": None}), "supplier_link_dropped", {"supplier_link_dropped": True}
        negative = self.built.list_ids[key] in self.neg_ids
        if negative and self.clamp.is_set() and req.stock_on_hand < 0:
            req, detail["clamped"] = req.model_copy(update={"stock_on_hand": 0}), True
        try:
            self.api.post_product(req)
        except StockTrimRejected as e:
            if not (negative and req.stock_on_hand < 0 and _negative_stock_error(e)):
                raise
            self.clamp.set()
            req, code = req.model_copy(update={"stock_on_hand": 0}), "negative_stock_rejected"
            detail.update(clamped=True, negative_stock_rejected={"status": e.status, "fields": _error_fields(e.body)})
            self.api.post_product(req)  # ONCE, clamped; a failure here is the item's failure
        if negative:
            detail["sent_stock"] = req.stock_on_hand
        _mark(self.id, "product", [key], "done", code, detail)

    # read-back: all negative-stock items + a deterministic sample of 25 ordinary products, by readable code
    def _readback(self) -> None:
        with session_scope() as s:
            done = {k: d or {} for k, d in s.execute(select(PushItem.key, PushItem.detail).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.status == "done"))}
        done_suppliers = _done_suppliers(self.target)
        neg = sorted(k for k in done if self.built.list_ids[k] in self.neg_ids)
        ordinary = sorted(k for k in done if k not in neg)
        todo = neg + ordinary[::max(len(ordinary) // READBACK_SAMPLE, 1)][:READBACK_SAMPLE]

        def check(k: str) -> list[str]:
            """Names of the fields whose stored value differs from what we sent ('missing' when the product cannot be read back)."""
            got = [p for p in self.api.get_products(code=k) if p.product_code_readable == k]
            if not got:
                return ["missing"]
            req = self.reqs["product"][k]
            sent = done[k].get("sent_stock", req.stock_on_hand)
            link = req.supplier_code if req.supplier_code in done_suppliers else None
            return _mismatches(req, got[0], sent, link)

        try:
            results = self.api.map_concurrent(check, todo)
        except StockTrimError as e:  # the read-back never fails the run
            self.extra["readback_error"] = type(e).__name__
            return
        fields: Counter[str] = Counter()
        neg_out: Counter[str] = Counter()
        for k, r in zip(todo, results):
            bad = r.result if r.ok else ["error"]
            fields.update(bad)
            if k in neg:
                sent = done[k].get("sent_stock", self.reqs["product"][k].stock_on_hand)
                neg_out["negative_stock_missing" if {"missing", "error"} & set(bad) else "negative_stock_silently_altered" if "stock_on_hand" in bad
                        else "negative_stock_stored_as_is" if sent < 0 else "negative_stock_clamped_ok"] += 1
        self.extra |= {"negative_stock_stored_as_is": 0, "negative_stock_silently_altered": 0, **neg_out,
                       "readback": {"negative_checked": len(neg), "sample_checked": len(todo) - len(neg), "mismatch_fields": dict(sorted(fields.items()))}}


def _rejected_keys(run_id: UUID) -> list[str]:
    with session_scope() as s:
        return list(s.scalars(select(PushItem.key).where(PushItem.run_id == run_id, PushItem.error_code == "negative_stock_rejected").order_by(PushItem.id)))


# Read-back compares EXACTLY: the mapper already rounded cost/price/stockOnHand to the 2 decimals StockTrim stores.
def _mismatches(req: ProductRequest, st: ProductResponse, sent_stock: float, link: Optional[str]) -> list[str]:
    bad = []
    if (st.name or "") != (req.name or ""):
        bad.append("name")
    if req.cost is not None and req.cost != st.cost:
        bad.append("cost")
    if req.price is not None and req.price != st.price:
        bad.append("price")
    if sent_stock != st.stock_on_hand:
        bad.append("stock_on_hand")
    if req.minimum_shelf_level is not None and req.minimum_shelf_level != st.minimum_shelf_level:
        bad.append("minimum_shelf_level")
    if req.maximum_shelf_level is not None and req.maximum_shelf_level != st.maximum_shelf_level:
        bad.append("maximum_shelf_level")
    if link and link != st.supplier_code and not any(x.supplier_id == link for x in st.suppliers or []):
        bad.append("supplier")
    return bad  # product_code_readable matched by construction (the lookup is by it)


# ---------------------------------------------------------------- lifecycle
def _summary(run_id: UUID, run: Optional[_Run], t0: float, prior: float, extra: dict[str, Any]) -> dict[str, Any]:
    rejected = _rejected_keys(run_id)
    ids = run.built.list_ids if run else {}
    out: dict[str, Any] = {"duration_seconds": round(prior + time.monotonic() - t0, 1), "items_failed": _failures(run_id), **extra}  # duration adds up over resumes
    if run:
        out |= run.extra | ({"fatal": run.fatal} if run.fatal else {})
        out["negative_stock_rejected"] = {"count": len(rejected), "list_ids": [ids[k] for k in rejected if k in ids], "clamp_switched": bool(rejected)}
    return out


def _thread_main(run_id: UUID, settings: Settings, kw: dict[str, Any], built: Optional[Built]) -> None:
    t0, prior, status, run, extra, client = time.monotonic(), 0.0, "failed", None, {}, None
    try:
        row = _run_row(run_id)
        opts, steps, prior = row.options, row.options["steps"], float((row.summary or {}).get("duration_seconds", 0))
        if built is None:  # fresh run: build the plan HERE (the request returned long ago), check it is the approved one, then lay out the items
            built = build_plan(settings)
            if plan_fingerprint(built.plan, steps) != opts.get("plan_fingerprint"):
                raise PlanChanged
            _insert_items(run_id, built, steps)
        client = StockTrimClient.from_settings(settings, **kw)
        run = _Run(run_id, built, StockTrimApi(client), _CANCELS[run_id], row.target)
        status = run.run(steps)
        if status == "done" and _failures(run_id):
            status = "done_with_failures"
    except Exception as e:  # noqa: BLE001 - the run is marked failed with the exception TYPE only
        log.error("push run %s crashed: %s", run_id, type(e).__name__)
        extra = {"error": "plan_fingerprint_mismatch" if isinstance(e, PlanChanged) else type(e).__name__}
    finally:
        if client:
            client.close()
        try:
            summary = _summary(run_id, run, t0, prior, extra)
            with session_scope() as s:
                s.execute(update(PushRun).where(PushRun.id == run_id).values(status=status, finished_at=utcnow(), updated_at=utcnow(), summary=summary))
            log.info("push run %s finished status=%s counts=%s", run_id, status, counts(run_id))
        finally:
            _CANCELS.pop(run_id, None)
            _ACTIVE.release()


def _launch(run_id: UUID, settings: Settings, kw: dict[str, Any], built: Optional[Built]) -> None:
    global _thread
    _CANCELS[run_id] = threading.Event()
    _thread = threading.Thread(target=_thread_main, args=(run_id, settings, kw, built), name=f"push-{run_id}", daemon=False)
    _thread.start()


def _set_running(run_id: UUID) -> None:
    try:
        with session_scope() as s:
            s.execute(update(PushRun).where(PushRun.id == run_id).values(status="running", finished_at=None, updated_at=utcnow()))
    except IntegrityError as e:
        raise PushError(409, "push_running") from e


def start(settings: Settings, steps: list[str], dry_run: bool, fingerprint: Optional[str], plan_fp: Optional[str], kw: dict[str, Any]) -> dict[str, Any]:
    steps = [s for s in STEPS if s in steps]
    opts = {"steps": steps, "dry_run": dry_run, "negative_stock_policy": settings.negative_stock_policy}
    if not dry_run:
        if not settings.stocktrim_writes_enabled:
            raise PushError(403, "stocktrim_writes_disabled")
        if not fingerprint:
            raise PushError(422, "preflight_fingerprint_required")
        if not plan_fp:
            raise PushError(422, "plan_fingerprint_required")
        if not _ACTIVE.acquire(blocking=False):
            raise PushError(409, "push_running")
    launched = False
    try:
        report = build_report(settings, PreflightOptions(), kw)
        target = stocktrim_target(settings)
        if not dry_run:
            if not report.verdict.ok_to_push:
                raise PushError(409, "preflight_not_ok")
            if report.fingerprint != fingerprint:
                raise PushError(409, "preflight_fingerprint_mismatch")
            run = PushRun(target=target, requested_by="admin-cli", options={**opts, "plan_fingerprint": plan_fp}, preflight_fingerprint=fingerprint, status="running")
            try:
                with session_scope() as s:
                    s.add(run)
            except IntegrityError as e:
                raise PushError(409, "push_running") from e
            _launch(run.id, settings, kw, None)
            launched = True  # the thread now owns the lock
            return {"run_id": run.id, "status": "running", "dry_run": False}
        built = build_plan(settings)
        pfp = plan_fingerprint(built.plan, steps)
        plan_counts = {**built.plan.counts, "dropped_supplier_links": sum(w.count for w in report.verdict.warnings if w.code == "unresolved_vendor")}
        run = PushRun(target=target, requested_by="admin-cli", options={**opts, "plan_fingerprint": pfp}, status="done", finished_at=utcnow(),
                      preflight_fingerprint=report.fingerprint, summary=plan_counts)
        with session_scope() as s:
            s.add(run)
        return {"run_id": run.id, "status": "done", "dry_run": True, "plan": plan_counts, "verdict": report.verdict, "plan_fingerprint": pfp,
                "preflight_fingerprint": report.fingerprint}
    finally:
        if not dry_run and not launched:
            _ACTIVE.release()


def resume(settings: Settings, run_id: UUID, kw: dict[str, Any]) -> dict[str, Any]:
    if not settings.stocktrim_writes_enabled:
        raise PushError(403, "stocktrim_writes_disabled")
    row = _run_row(run_id)
    if row.options.get("dry_run") or row.status not in ("interrupted", "cancelled", "failed", "done_with_failures") or not counts(run_id):
        raise PushError(409, "run_not_resumable")
    if row.target != stocktrim_target(settings):
        raise PushError(409, "target_mismatch")
    if not _ACTIVE.acquire(blocking=False):
        raise PushError(409, "push_running")
    try:
        built = build_plan(settings)
        if plan_fingerprint(built.plan, row.options["steps"]) != row.options.get("plan_fingerprint"):
            raise PushError(409, "plan_fingerprint_mismatch")
        _set_running(run_id)
    except BaseException:
        _ACTIVE.release()
        raise
    _launch(run_id, settings, kw, built)
    return {"run_id": run_id, "status": "running", "dry_run": False}


def cancel(run_id: UUID) -> None:
    _run_row(run_id)
    if (ev := _CANCELS.get(run_id)) is None:
        raise PushError(409, "run_not_active")
    ev.set()


def recover_push_runs() -> int:
    """Startup: one worker, so any `running` push_run is stale."""
    with session_scope() as s:
        n = s.execute(update(PushRun).where(PushRun.status == "running").values(status="interrupted", finished_at=utcnow())).rowcount
    if n:
        log.warning("marked %d stale push run(s) interrupted after restart", n)
    return n


def counts(run_id: UUID) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    with session_scope() as s:
        for ent, st, n in s.execute(select(PushItem.entity, PushItem.status, func.count()).where(PushItem.run_id == run_id).group_by(PushItem.entity, PushItem.status)):
            out.setdefault(ent, {})[st] = n
    return out


def progress(settings: Settings, run_id: UUID) -> dict[str, Any]:
    row = _run_row(run_id)
    c = counts(run_id)
    with session_scope() as s:
        codes: dict[str, dict[str, int]] = {}
        for ent, code, n in s.execute(select(PushItem.entity, PushItem.error_code, func.count()).where(PushItem.run_id == run_id, PushItem.error_code.is_not(None)).group_by(PushItem.entity, PushItem.error_code)):
            codes.setdefault(ent, {})[code] = n
        samples = [{"entity": e, "ref": r, "error_code": c_} for e, r, c_ in s.execute(  # failed first: warnings on done items must not push real failures out
            select(PushItem.entity, PushItem.ref, PushItem.error_code).where(PushItem.run_id == run_id, PushItem.error_code.is_not(None))
            .order_by(case((PushItem.status == "failed", 0), else_=1), PushItem.id).limit(SAMPLE_LIMIT))]
    pending = {e: v.get("pending", 0) for e, v in c.items()}
    calls = math.ceil(pending.get("supplier", 0) / CHUNK) + pending.get("product", 0)
    return {"run_id": run_id, "status": row.status, "dry_run": bool(row.options.get("dry_run")), "steps": row.options.get("steps", []), "started_at": row.started_at,
            "updated_at": row.updated_at, "finished_at": row.finished_at, "counts": c, "error_codes": codes, "eta_seconds": math.ceil(calls / settings.stocktrim_max_rps) if row.status == "running" else None,
            "summary": row.summary, "samples": samples}
