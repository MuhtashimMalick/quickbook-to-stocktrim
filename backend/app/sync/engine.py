"""Push engine (Phase 2, stages 9-13): suppliers, products, BOM edges, customers, sales-order lines, then purchase orders to StockTrim, resumable, one run at a time; `initial` pushes everything, `sync` only what
changed since the `pushed_item` ledger (and re-queues what a StockTrim wipe removed). See README "Push engine".
Leak rules: push_item / summary / progress carry keys (supplier code, SKU, BOM edge key, customer ListID), QuickBooks ListIDs, counts and error codes ONLY (never names,
messages or response bodies); logs carry counts only. The plan lives in memory only; worker functions return nothing but a status."""
import hashlib
import json
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy import bindparam, case, delete, func, insert, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import session_scope
from ..domain.bom import BomPlan, plan_bom
from ..domain.from_qb import load_snapshot, stream_payloads
from ..domain.keys import norm, resolve_customers, resolve_products, resolve_suppliers
from ..models import BomEdge as BomEdgeRow
from ..models import PushedItem, PushItem, PushRun, utcnow
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
from .mappers import PushPlan, bom_edge_key, build_push_plan, purchase_orders, sales_order_lines

log = logging.getLogger("app.push")
STEPS = ("suppliers", "products", "bom", "customers", "sales_orders", "purchase_orders")  # push order
LEDGER_STEPS = ("suppliers", "products", "customers", "sales_orders", "purchase_orders")  # steps tracked in pushed_item (BOM keeps bom_edge)
ENTITY = {"suppliers": "supplier", "products": "product", "bom": "bom_edge", "customers": "customer", "sales_orders": "sales_order_line", "purchase_orders": "purchase_order"}
PLAN_ATTR = {"suppliers": "suppliers", "products": "products", "bom": "bom_edges", "customers": "customers", "sales_orders": "sales_orders", "purchase_orders": "purchase_orders"}  # step -> PushPlan field
CHUNK = 50
SAMPLE_LIMIT = 50
READBACK_SAMPLE = 25
# Closed set of push_item.error_code values. A not-attempted item is status `skipped` + `aborted` (never `pending`, so a resume retries it).
ERROR_CODES = frozenset({"rejected_400", "rejected_404", "rejected_409", "rejected_other", "auth", "unavailable", "unknown_outcome", "aborted",
                         "negative_stock_rejected", "supplier_link_dropped", "dependency_failed", "cancelled", "internal"})
_FATAL = (StockTrimUnavailable, StockTrimConfigError, StockTrimAuthError)


class PushError(Exception):
    def __init__(self, status: int, code: str, **extra: Any):
        self.status, self.code, self.extra = status, code, extra  # extra (step names, counts) ends up in the HTTP detail next to the code
        super().__init__(code)


class _Cancelled(StockTrimAborted):
    """Raised inside a chunk when cancel was requested; the item stays `pending`. (An Aborted subclass: map_concurrent does not log it.)"""


class _DependencyFailed(Exception):
    """A BOM edge whose parent or component product is not `done` for this target."""


class PlanChanged(Exception):
    """The plan built by the run thread differs from the plan_fingerprint the caller approved."""


_ACTIVE = threading.Lock()  # ponytail: one run per process; the DB partial unique index is the second line of defence
_CANCELS: dict[UUID, threading.Event] = {}
_thread: Optional[threading.Thread] = None
_SHUTTING_DOWN = threading.Event()  # set at app shutdown: no NEW real run may start (cleared at startup)


def join(timeout: Optional[float] = None) -> None:
    """Wait for the background run (tests, shutdown)."""
    if _thread:
        _thread.join(timeout)


def begin_shutdown() -> None:
    _SHUTTING_DOWN.set()


def shutdown(timeout: float = 8.0) -> None:
    """App shutdown: refuse new real runs, ask every active run to cancel (it ends `cancelled`, resumable) and wait for the worker."""
    _SHUTTING_DOWN.set()
    for ev in list(_CANCELS.values()):
        ev.set()
    join(timeout)


# ---------------------------------------------------------------- plan
@dataclass
class Built:
    plan: PushPlan
    list_ids: dict[str, str]  # product SKU -> QuickBooks ListID
    bom: BomPlan
    quantities: dict[str, Decimal]  # bom_edge_key -> planned quantity (exact Decimal, already rounded to StockTrim's 5 decimals)


def build_plan(settings: Settings, steps: "tuple[str, ...] | list[str]" = ()) -> Built:
    """`steps` selects the (big) order plans to build; the master-data plan is always built."""
    with session_scope() as s:
        snap = load_snapshot(s, settings.product_key_field)
        orders = {n: list(stream_payloads(s, e)) for n, e in (("sales_orders", "SalesOrder"), ("purchase_orders", "PurchaseOrder")) if n in steps}
    sup = resolve_suppliers(snap.suppliers)
    prod = resolve_products(snap.products, sup)
    bom = plan_bom(prod, snap.assemblies, unusable_list_ids={i.ref for i in snap.issues})
    cust = resolve_customers(snap.customers)
    plan = build_push_plan(sup, prod, cust, bom, settings.negative_stock_policy)
    sku_of = {p.list_id: p.sku for p in prod.ok}
    if "sales_orders" in orders:
        since = datetime.now(timezone.utc).date() - timedelta(days=round(365.25 * settings.sales_history_years))  # ponytail: ~N years, not calendar-exact
        plan.sales_orders, plan.order_stats["sales_orders"] = sales_order_lines(orders["sales_orders"], sku_of, {c.list_id for c in cust.ok}, since)
    if "purchase_orders" in orders:
        plan.purchase_orders, plan.order_stats["purchase_orders"] = purchase_orders(orders["purchase_orders"], sku_of, {s.list_id: s.code for s in sup.ok})
    return Built(plan, {p.sku: p.list_id for p in prod.ok}, bom,
                 {bom_edge_key(e.parent_sku, e.component_sku): e.quantity for es in bom.edges_by_parent.values() for e in es})


def payload_hash(req: Any) -> str:
    """sha256 of the exact request payload that would be sent (the mappers already round to StockTrim's precision, so no phantom diffs)."""
    return hashlib.sha256(json.dumps(req.payload(), sort_keys=True).encode()).hexdigest()


def plan_fingerprint(plan: PushPlan, sel: "dict[str, list[str]] | list[str]", mode: str = "") -> str:
    """sha256 over the sorted selected keys per step and the payload hash of each ('' for a key that is not planned = a BOM DELETE).
    `sel` is a list of steps (every planned key: initial runs) or {step: keys} (sync runs: only the items that WILL be pushed). `mode` ('stamp') is part of the hash,
    so a sync fingerprint can never approve a stamp and vice-versa."""
    if not isinstance(sel, dict):
        sel = {st: [k for k, _ in getattr(plan, PLAN_ATTR[st])] for st in sel}
    h = hashlib.sha256(mode.encode())
    for step in STEPS:
        reqs = dict(getattr(plan, PLAN_ATTR[step])) if step in sel else {}
        for key in sorted(sel.get(step, ())):
            h.update(json.dumps([step, key, payload_hash(reqs[key]) if key in reqs else ""]).encode())
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
    if isinstance(e, _DependencyFailed):
        return "dependency_failed", {}
    if isinstance(e, StockTrimBadResponse):  # 2xx we cannot parse: the write probably happened
        return "unknown_outcome", {"status": e.status}
    return "internal", {"exc": type(e).__name__}


def _negative_stock_error(e: StockTrimRejected) -> bool:
    """A 400 whose error FIELDS name the on-hand stock (observed live: StockTrim stores negatives, so this is a safety net)."""
    return e.status == 400 and any("onhand" in f.lower() for f in _error_fields(e.body))


# ---------------------------------------------------------------- db helpers
def _mark_stmt(run_id: UUID, entity: str, keys: list[str], status: str, code: Optional[str] = None, detail: Optional[dict] = None) -> Any:
    return (update(PushItem).where(PushItem.run_id == run_id, PushItem.entity == entity, PushItem.key.in_(keys))
            .values(status=status, error_code=code, detail=detail or null(), attempts=PushItem.attempts + 1, updated_at=utcnow()))


def _mark(run_id: UUID, entity: str, keys: list[str], status: str, code: Optional[str] = None, detail: Optional[dict] = None) -> None:
    """One short transaction."""
    with session_scope() as s:
        s.execute(_mark_stmt(run_id, entity, keys, status, code, detail))


def _keys(run_id: UUID, entity: str, *, done: bool) -> list[str]:
    with session_scope() as s:
        cond = PushItem.status == "done" if done else PushItem.status != "done"
        return list(s.scalars(select(PushItem.key).where(PushItem.run_id == run_id, PushItem.entity == entity, cond).order_by(PushItem.id)))


def _beat(run_id: UUID) -> None:
    with session_scope() as s:
        s.execute(update(PushRun).where(PushRun.id == run_id).values(updated_at=utcnow()))


def bom_diff(target: str, built: Built, requeued: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(edge keys to POST in plan order = new or changed quantity, edge keys to DELETE = stored for this target but no longer planned).
    Stored edges that touch a re-queued product (it vanished from StockTrim) are treated as not stored: they are re-POSTed, never DELETEd.
    Pairs are compared by their normalised form (StockTrim keys are case-insensitive): a case-only rename is neither a POST nor a DELETE."""
    rq = {norm(x) for x in requeued}
    with session_scope() as s:
        raw = [(p, c, q) for p, c, q in s.execute(select(BomEdgeRow.parent_sku, BomEdgeRow.component_sku, BomEdgeRow.quantity).where(BomEdgeRow.target == target))
               if norm(p) not in rq and norm(c) not in rq]
    stored = {(norm(p), norm(c)): q for p, c, q in raw}
    post = [k for k, r in built.plan.bom_edges if stored.get((norm(r.product_code), norm(r.component_code))) != built.quantities[k]]
    planned = {(norm(r.product_code), norm(r.component_code)) for _, r in built.plan.bom_edges}
    return post, sorted(bom_edge_key(p, c) for p, c, _ in raw if (norm(p), norm(c)) not in planned)


def _unstamp(target: str, parent: str) -> Any:
    """Any write to one edge of a parent invalidates the parent's hash (siblings too): _finish_parents re-stamps it only when every planned edge matches."""
    return update(BomEdgeRow).where(BomEdgeRow.target == target, BomEdgeRow.parent_sku == parent).values(parent_hash="")


@dataclass
class Diff:
    """Sync-mode plan diff against pushed_item (suppliers, products, customers) and bom_edge. `sel` = {step: keys that WILL be pushed} (bom: POSTs then DELETEs)."""
    sel: dict[str, list[str]]
    stats: dict[str, dict[str, int]]  # step -> new/changed/unchanged/requeued/vanished
    vanished_refs: dict[str, list[str]]  # step -> QuickBooks ListIDs of ledger keys that are no longer planned (reported only)
    requeued: dict[str, list[str]]  # step -> ledger keys missing from StockTrim (pushed again as new)
    nulls: dict[str, list[str]]  # step -> present, planned ledger keys whose hash is NULL (what `stamp_only` adopts)
    ledger_rows: dict[str, int]  # step -> number of ledger rows (the mass_requeue threshold is relative to it)
    bom_post: list[str]
    bom_gone: list[str]
    so_calls: int = 0  # bulk calls the sales-order lines need

    @property
    def calls(self) -> int:
        n = {st: len(self.sel.get(st, ())) for st in STEPS}
        return math.ceil(n["suppliers"] / CHUNK) + n["products"] + n["bom"] + n["customers"] + self.so_calls + n["purchase_orders"]


def _pushed(target: str, entity: str) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """pushed_item rows of one entity: key -> (ref, payload_hash)."""
    with session_scope() as s:
        return {k: (r, h) for k, r, h in s.execute(select(PushedItem.key, PushedItem.ref, PushedItem.payload_hash).where(PushedItem.target == target, PushedItem.entity == entity))}


def so_chunks(keys: list[str], reqs: dict[str, Any]) -> list[list[str]]:
    """Bulk calls for sales-order lines: one customer per call (only the header's customerCode counts), at most CHUNK lines."""
    by: dict[str, list[str]] = {}
    for k in keys:
        by.setdefault(reqs[k].customer_code or "", []).append(k)
    return [ks[i:i + CHUNK] for ks in by.values() for i in range(0, len(ks), CHUNK)]


def _inexact(target: str) -> int:
    """Ledger products whose LAST push was not the planned payload (supplier link dropped / stock clamped): their hash is the hash of what was sent, so the next sync re-sends them."""
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(PushedItem).join(PushItem, (PushItem.run_id == PushedItem.last_run_id) & (PushItem.entity == PushedItem.entity) & (PushItem.key == PushedItem.key))
                        .where(PushedItem.target == target, PushedItem.entity == "product",
                               (PushItem.detail["supplier_link_dropped"].as_boolean().is_(True)) | (PushItem.detail["clamped"].as_boolean().is_(True)))) or 0


def compute_diff(target: str, built: Built, steps: list[str], exists: dict[str, Any]) -> Diff:
    """new = not in the ledger; requeued = in the ledger but missing from StockTrim (`exists` = the pre-flight key sets; an entity it does not cover is assumed present);
    changed = payload hash differs, is NULL or the key only differs in case (StockTrim keys are case-insensitive and a push rewrites the stored casing); unchanged = no item, no call.
    The ledger is indexed by the NORMALISED key. The BOM diff is bom_diff() (with the re-queued products' edges re-posted)."""
    d = Diff({}, {}, {}, {}, {}, {}, [], [])
    for st in LEDGER_STEPS:
        if st not in steps:
            continue
        led = {norm(k): (k, r, h) for k, (r, h) in _pushed(target, ENTITY[st]).items()}
        plan = getattr(built.plan, PLAN_ATTR[st])
        present = exists.get(st, {}).get("keys")
        out: dict[str, list[str]] = {"new": [], "changed": [], "unchanged": [], "requeued": []}
        d.nulls[st], d.ledger_rows[st] = [], len(led)
        for k, req in plan:
            old = led.get(norm(k))
            if old is None:
                out["new"].append(k)
            elif present is not None and norm(k) not in present:
                out["requeued"].append(k)
            elif old[2] != payload_hash(req) or old[0] != k:
                out["changed"].append(k)
                if old[2] is None and old[0] == k:
                    d.nulls[st].append(k)
            else:
                out["unchanged"].append(k)
        keys = {norm(k) for k, _ in plan}
        d.vanished_refs[st] = [r for n, (_, r, _) in sorted(led.items()) if n not in keys and r]
        d.requeued[st] = out["requeued"]
        d.stats[st] = {**{n: len(v) for n, v in out.items()}, "vanished": sum(1 for n in led if n not in keys), **({"inexact": _inexact(target)} if st == "products" else {})}
        push = {*out["new"], *out["changed"], *out["requeued"]}
        d.sel[st] = [k for k, _ in plan if k in push]  # plan order
    d.so_calls = len(so_chunks(d.sel.get("sales_orders", []), dict(built.plan.sales_orders)))
    if "bom" in steps:
        d.bom_post, d.bom_gone = bom_diff(target, built, frozenset(d.requeued.get("products", ())))
        d.sel["bom"] = [*d.bom_post, *d.bom_gone]
    return d


def _insert_items(run_id: UUID, built: Built, steps: list[str], target: str, diff: Optional[Diff]) -> None:
    """One push_item per planned key (refs are QuickBooks ListIDs); sync mode only the keys of `diff.sel`; `bom` always only the DIFF against the stored edges (new/changed -> POST,
    vanished -> DELETE). Re-queued keys leave the ledger first (and their products' BOM edges leave bom_edge), in the SAME transaction: they are `new` from now on."""
    reqs = {st: dict(getattr(built.plan, PLAN_ATTR[st])) for st in LEDGER_STEPS}
    rows: list[dict[str, Any]] = []
    for st in steps:
        if st == "bom":
            keys = diff.sel["bom"] if diff else [k for part in bom_diff(target, built) for k in part]
        else:
            keys = diff.sel[st] if diff else [k for k, _ in getattr(built.plan, PLAN_ATTR[st])]
        ref = {"suppliers": lambda k: reqs["suppliers"][k].external_id, "products": lambda k: built.list_ids[k], "customers": lambda k: k,  # customer code == QuickBooks ListID
               "sales_orders": lambda k: k.split(":")[0], "purchase_orders": lambda k: k[3:],  # TxnID of the order
               "bom": lambda k: built.list_ids.get(json.loads(k)[0])}[st]
        rows += [{"run_id": run_id, "entity": ENTITY[st], "key": k, "ref": ref(k)} for k in keys]
    with session_scope() as s:
        if diff:
            for st, keys in diff.requeued.items():
                if keys:
                    s.execute(delete(PushedItem).where(PushedItem.target == target, PushedItem.entity == ENTITY[st], PushedItem.key.in_(keys)))
            if gone := [g.lower() for g in diff.requeued.get("products", ())]:  # ponytail: SQL lower() approximates norm(); an exotic (NFKC-only) case difference would leave a stale edge row
                touched = (func.lower(BomEdgeRow.parent_sku).in_(gone)) | (func.lower(BomEdgeRow.component_sku).in_(gone))
                parents = select(BomEdgeRow.parent_sku).where(BomEdgeRow.target == target, touched)
                s.execute(update(BomEdgeRow).where(BomEdgeRow.target == target, BomEdgeRow.parent_sku.in_(parents)).values(parent_hash=""))  # siblings of a lost edge are no longer complete
                s.execute(delete(BomEdgeRow).where(BomEdgeRow.target == target, touched))
        for i in range(0, len(rows), 1000):
            s.execute(insert(PushItem), rows[i:i + 1000])


def _has_pending(run_id: UUID) -> bool:
    with session_scope() as s:
        return s.scalar(select(PushItem.id).where(PushItem.run_id == run_id, PushItem.status == "pending").limit(1)) is not None


def _failures(run_id: UUID) -> int:
    """Items that ended `failed`, or were never attempted because the run aborted."""
    with session_scope() as s:
        return s.scalar(select(func.count()).where(PushItem.run_id == run_id, (PushItem.status == "failed") | ((PushItem.status == "skipped") & (PushItem.error_code == "aborted")))) or 0


def _done_keys(target: str, entity: str) -> set[str]:
    """Keys of `entity` (supplier, product, customer) that were pushed to this StockTrim target: the pushed_item ledger."""
    return set(_pushed(target, entity))


_REAL = (PushRun.status == "done", PushRun.options["dry_run"].as_boolean().is_(False), func.coalesce(PushRun.options["mode"].as_string(), "") != "stamp")  # a completed REAL run (a stamp is no baseline)


def _completed(target: str) -> bool:
    with session_scope() as s:
        return s.scalar(select(PushRun.id).where(PushRun.target == target, *_REAL).limit(1)) is not None


def _ledger(target: str) -> Optional[dict[str, set[str]]]:
    """'Sync mode' gate: None (strict: the account must be empty) unless this target already has a completed non-dry-run run; then the
    (comparison-normalised) keys we pushed, so the pre-flight can tell our own records from foreign ones."""
    return {st: {norm(k) for k in _done_keys(target, ENTITY[st])} for st in LEDGER_STEPS} if _completed(target) else None


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
        self.reqs = {"supplier": dict(built.plan.suppliers), "product": dict(built.plan.products), "bom_edge": dict(built.plan.bom_edges), "customer": dict(built.plan.customers),
                     "sales_order_line": dict(built.plan.sales_orders), "purchase_order": dict(built.plan.purchase_orders)}
        self.neg_ids = set(built.plan.negative_stock_list_ids)
        self.clamp = threading.Event()  # set after the first negative-stock rejection: the rest of the run is clamped
        self.fatal: Optional[str] = None
        self.extra: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._led: dict[str, dict[str, str]] = {}  # entity -> normalised key -> ledger key (lazy, see _stale)

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

    def _stale(self, entity: str, keys: list[str]) -> list[str]:
        """Ledger keys that are the SAME record in another casing (a case-only rename): the push rewrote the stored casing, so the old row must go."""
        with self._lock:
            if entity not in self._led:
                self._led[entity] = {norm(k): k for k in _pushed(self.target, entity)}
            led = self._led[entity]
            old = [led[n] for k in keys if (n := norm(k)) in led and led[n] != k]
            led.update({norm(k): k for k in keys})
        return old

    def _done(self, entity: str, keys: list[str], code: Optional[str] = None, detail: Optional[dict] = None, sent: Any = None) -> None:
        """ONE transaction: the push_item is `done` and its pushed_item ledger row stands with it. The hash is that of what was actually SENT (`sent`, default the planned
        payload): a product pushed without its supplier link / clamped therefore never equals the plan and is re-sent by the next sync (NULL means only 'unknown' = backfill)."""
        ref = {"supplier": lambda k: self.reqs["supplier"][k].external_id, "product": lambda k: self.built.list_ids[k], "customer": lambda k: k,
               "sales_order_line": lambda k: k.split(":")[0], "purchase_order": lambda k: k[3:]}[entity]
        rows = [{"target": self.target, "entity": entity, "key": k, "ref": ref(k), "payload_hash": payload_hash(sent if sent is not None else self.reqs[entity][k]),
                 "last_run_id": self.id, "pushed_at": utcnow()} for k in keys]
        stale = self._stale(entity, keys)
        with session_scope() as s:
            if stale:
                s.execute(delete(PushedItem).where(PushedItem.target == self.target, PushedItem.entity == entity, PushedItem.key.in_(stale)))
            ins = pg_insert(PushedItem).values(rows)
            s.execute(ins.on_conflict_do_update(index_elements=["target", "entity", "key"], set_={c: ins.excluded[c] for c in ("ref", "payload_hash", "last_run_id", "pushed_at")}))
            s.execute(_mark_stmt(self.id, entity, keys, "done", code, detail))

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
                self._done("supplier", chunk)
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
                self._done("supplier", [k])

            try:
                self._settle("supplier", self.api.map_concurrent(one, chunk))
            except _FATAL as e:
                return self._fatal("supplier", e)
        return None

    # products: one POST per product (V1 upsert), concurrent inside a chunk
    def _products(self) -> Optional[str]:
        done_suppliers = _done_keys(self.target, "supplier")
        with session_scope() as s:  # a resume re-pushes, WITH the link, the products that were pushed without it while their supplier is now done
            dropped = s.scalars(select(PushItem.key).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.status == "done",
                                                           PushItem.detail["supplier_link_dropped"].as_boolean().is_(True))).all()
            if relink := [k for k in dropped if self.reqs["product"][k].supplier_code in done_suppliers]:
                s.execute(update(PushItem).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.key.in_(relink))
                          .values(status="pending", error_code=None, detail=null(), updated_at=utcnow()))
        return self._chunked("product", lambda k: self._one_product(k, done_suppliers))

    def _chunked(self, entity: str, fn: Callable[[str], None], after: Optional[Callable[[list[str]], None]] = None) -> Optional[str]:
        """Not-done keys of `entity` in chunks of CHUNK through map_concurrent (cancel takes effect between chunks, a fatal error stops the run)."""
        keys = _keys(self.id, entity, done=False)
        for i in range(0, len(keys), CHUNK):
            if self.cancel.is_set():
                return "cancelled"
            _beat(self.id)
            try:
                self._settle(entity, self.api.map_concurrent(fn, keys[i:i + CHUNK]))
            except _FATAL as e:
                return self._fatal(entity, e)
            if after:
                after(keys[i:i + CHUNK])
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
        self._done("product", [key], code, detail, sent=req)  # a dropped link / clamped stock is not the planned payload: its (different) hash makes the next sync re-send it

    # BOM edges (bottom-up, plan order): POST = upsert by pair; a vanished edge is DELETEd with both readable codes
    def _bom(self) -> Optional[str]:
        done = _done_keys(self.target, "product")
        return self._chunked("bom_edge", lambda k: self._one_edge(k, done), self._finish_parents)

    def _one_edge(self, key: str, done_products: set[str]) -> None:
        self._check_cancel()
        parent, comp = json.loads(key)
        if key not in self.reqs["bom_edge"]:  # no longer planned
            try:
                self.api.delete_bom(product_code=parent, component_code=comp)  # the client counts a 404 after a transient failure as success
            except StockTrimRejected as e:
                if e.status != 404:  # a plain 404 = already gone (or a product that is gone): the ledger row must go too
                    raise
                with self._lock:
                    self.extra["bom_delete_404"] = self.extra.get("bom_delete_404", 0) + 1
            with session_scope() as s:
                s.execute(_unstamp(self.target, parent))  # FIRST: locks the parent's rows in scan order, so concurrent edges of one parent queue instead of deadlocking
                s.execute(delete(BomEdgeRow).where(BomEdgeRow.target == self.target, BomEdgeRow.parent_sku == parent, BomEdgeRow.component_sku == comp))
                s.execute(_mark_stmt(self.id, "bom_edge", [key], "done"))
            return
        if parent not in done_products or comp not in done_products:
            raise _DependencyFailed
        self.api.post_bom(self.reqs["bom_edge"][key])
        qty = self.built.quantities[key]
        with session_scope() as s:  # ONE transaction: the ledger row and `done` stand or fall together
            s.execute(_unstamp(self.target, parent))  # first, see the delete branch
            s.execute(pg_insert(BomEdgeRow).values(target=self.target, parent_sku=parent, component_sku=comp, quantity=qty, parent_hash="")
                      .on_conflict_do_update(index_elements=["target", "parent_sku", "component_sku"], set_={"quantity": qty, "pushed_at": utcnow()}))
            s.execute(delete(BomEdgeRow).where(BomEdgeRow.target == self.target, func.lower(BomEdgeRow.parent_sku) == parent.lower(), func.lower(BomEdgeRow.component_sku) == comp.lower(),
                                               (BomEdgeRow.parent_sku != parent) | (BomEdgeRow.component_sku != comp)))  # the same edge in another casing (case-only rename)
            s.execute(_mark_stmt(self.id, "bom_edge", [key], "done"))

    def _finish_parents(self, chunk: list[str]) -> None:
        """parent_hash is stamped only once EVERY planned edge of the parent is stored with its planned quantity (and nothing else is)."""
        parents = {json.loads(k)[0] for k in chunk} & self.built.bom.edges_by_parent.keys()
        with session_scope() as s:
            stored: dict[str, dict[str, Decimal]] = {}
            for p, c, q in s.execute(select(BomEdgeRow.parent_sku, BomEdgeRow.component_sku, BomEdgeRow.quantity).where(BomEdgeRow.target == self.target, BomEdgeRow.parent_sku.in_(parents))):
                stored.setdefault(p, {})[c] = q
            for p in parents:
                if stored.get(p) == {e.component_sku: e.quantity for e in self.built.bom.edges_by_parent[p]}:
                    s.execute(update(BomEdgeRow).where(BomEdgeRow.target == self.target, BomEdgeRow.parent_sku == p).values(parent_hash=self.built.bom.fingerprint_by_parent[p]))

    # customers: PUT is a proven upsert by code (see docs/stocktrim-api-notes.md), so a failed or lost call is simply retried by the client
    def _customers(self) -> Optional[str]:
        return self._chunked("customer", self._one_customer)

    def _one_customer(self, key: str) -> None:
        self._check_cancel()
        self.api.put_customer(self.reqs["customer"][key])
        self._done("customer", [key])

    # sales-order lines: bulk POST per customer (probed 2026-11-17), upsert by externalReferenceId so a retry never duplicates; a rejected call (400) falls back to one POST per line
    def _sales_orders(self) -> Optional[str]:
        reqs = self.reqs["sales_order_line"]
        chunks = so_chunks(_keys(self.id, "sales_order_line", done=False), reqs)

        def one_chunk(ks: list[str]) -> None:
            self._check_cancel()
            try:
                self.api.post_sales_orders([reqs[k] for k in ks])
                self._done("sales_order_line", ks)
                return
            except StockTrimRejected:
                pass  # ponytail: any rejection of the whole call is isolated per line (e.g. a customer StockTrim does not know)
            for k in ks:
                self._check_cancel()
                try:
                    self.api.post_sales_order(reqs[k])
                    self._done("sales_order_line", [k])
                except StockTrimRejected as e:
                    _mark(self.id, "sales_order_line", [k], "failed", *classify(e))

        for i in range(0, len(chunks), CHUNK):  # a wave of CHUNK calls; cancel takes effect between waves
            if self.cancel.is_set():
                return "cancelled"
            _beat(self.id)
            try:
                results = self.api.map_concurrent(one_chunk, chunks[i:i + CHUNK])
            except _FATAL as e:
                return self._fatal("sales_order_line", e, [])
            for r in (r for r in results if not r.ok and not isinstance(r.error, _Cancelled)):
                aborted = isinstance(r.error, StockTrimAborted)
                _mark(self.id, "sales_order_line", r.item, "skipped" if aborted else "failed", *(("aborted", None) if aborted else classify(r.error)))  # type: ignore[arg-type]
        return None

    # purchase orders: POST upserts by clientReferenceNumber; a state change (Sent -> Received) is the same POST with the new status
    def _purchase_orders(self) -> Optional[str]:
        return self._chunked("purchase_order", self._one_po)

    def _one_po(self, key: str) -> None:
        self._check_cancel()
        self.api.post_purchase_order(self.reqs["purchase_order"][key])
        self._done("purchase_order", [key])

    # read-back: all negative-stock items + a deterministic sample of 25 ordinary products, by readable code
    def _readback(self) -> None:
        with session_scope() as s:
            done = {k: d or {} for k, d in s.execute(select(PushItem.key, PushItem.detail).where(PushItem.run_id == self.id, PushItem.entity == "product", PushItem.status == "done"))}
        done_suppliers = _done_keys(self.target, "supplier")
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


def _sync_summary(diff: Diff) -> dict[str, Any]:
    """New/changed/unchanged/requeued/vanished counts per step; vanished and re-queued records are reported by QuickBooks ListID only (never deleted: suppliers, customers, products)."""
    out: dict[str, Any] = {st: {**v, "vanished_list_ids": diff.vanished_refs[st][:SAMPLE_LIMIT]} for st, v in diff.stats.items()}
    if "bom" in diff.sel:
        out["bom"] = {"to_post": len(diff.bom_post), "to_delete": len(diff.bom_gone)}
    return out


def _thread_main(run_id: UUID, settings: Settings, kw: dict[str, Any], built: Optional[Built], exists: Optional[dict[str, Any]] = None) -> None:
    t0, prior, status, run, extra, client = time.monotonic(), 0.0, "failed", None, {}, None
    try:
        row = _run_row(run_id)
        opts, steps, prior = row.options, row.options["steps"], float((row.summary or {}).get("duration_seconds", 0))
        extra = {k: v for k, v in (row.summary or {}).items() if k in ("sync", "mode")}  # a resume keeps what the first start reported
        if built is None:  # fresh run: build the plan HERE (the request returned long ago), check it is the approved one, then lay out the items
            built = build_plan(settings, steps)
            diff = compute_diff(row.target, built, steps, exists or {}) if opts.get("mode") == "sync" else None
            if plan_fingerprint(built.plan, diff.sel if diff else steps) != opts.get("plan_fingerprint"):
                raise PlanChanged
            _insert_items(run_id, built, steps, row.target, diff)
            extra = {"mode": opts.get("mode", "initial"), **({"sync": _sync_summary(diff)} if diff else {})}
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


def _launch(run_id: UUID, settings: Settings, kw: dict[str, Any], built: Optional[Built], exists: Optional[dict[str, Any]] = None) -> None:
    global _thread
    _CANCELS[run_id] = threading.Event()
    _thread = threading.Thread(target=_thread_main, args=(run_id, settings, kw, built, exists), name=f"push-{run_id}", daemon=False)
    _thread.start()


def _set_running(run_id: UUID) -> None:
    try:
        with session_scope() as s:
            s.execute(update(PushRun).where(PushRun.id == run_id).values(status="running", finished_at=None, updated_at=utcnow()))
    except IntegrityError as e:
        raise PushError(409, "push_running") from e


MASS_REQUEUE_MIN, MASS_REQUEUE_PCT = 25, 0.05


def _mass_delete(diff: Diff, ack: Optional[dict[str, int]], target: str) -> None:
    """BOM edges to DELETE above max(25, 5% of the stored edges of this target) (an empty assemblies read looks like this) need `acknowledge_delete` = {"bom": that count}. The scheduler never passes it."""
    n = len(diff.bom_gone)
    if not n:
        return
    with session_scope() as s:
        stored = s.scalar(select(func.count()).select_from(BomEdgeRow).where(BomEdgeRow.target == target)) or 0
    if n > max(MASS_REQUEUE_MIN, MASS_REQUEUE_PCT * stored) and (ack or {}).get("bom") != n:
        raise PushError(409, "mass_delete", to_delete=n)


def _requires_step(target: str, steps: list[str], exists: dict[str, Any]) -> None:
    """A wipe that took ledger entities of a step that is NOT in this run cannot be repaired by it (a bom-only run with a wiped product, a products-only run with a wiped supplier):
    refuse and name the steps to add."""
    gone = [st for st in LEDGER_STEPS if st not in steps and (present := exists.get(st, {}).get("keys")) is not None and any(norm(k) not in present for k in _pushed(target, ENTITY[st]))]
    if gone:
        raise PushError(409, "requeue_requires_step", steps=gone)


def _mass_requeue(diff: Diff, ack: Optional[dict[str, int]]) -> None:
    """An empty/failed StockTrim listing looks like a wipe: a step that would re-queue more than max(25, 5% of its ledger rows) needs `acknowledge_requeue` = that count per step.
    ponytail: the (future) scheduler must never pass acknowledge_requeue; a human confirms a mass re-send."""
    big = {st: v["requeued"] for st, v in diff.stats.items() if v["requeued"] > max(MASS_REQUEUE_MIN, MASS_REQUEUE_PCT * diff.ledger_rows[st])}
    if big and any((ack or {}).get(st) != n for st, n in big.items()):
        raise PushError(409, "mass_requeue", requeued=big)


def _stamp(built: Built, target: str, steps: list[str], dry_run: bool, report: Any, diff: Diff, plan_fp: Optional[str]) -> dict[str, Any]:
    """`stamp_only`: adopt the current state as the baseline. Writes the plan's payload hashes into the NULL-hash ledger rows (no StockTrim call), only when no ledger key is
    missing from StockTrim, only the NULL rows (a genuinely changed record keeps its hash, so it is still pushed; an inexact row has the hash of what was sent, never NULL) and only
    when the snapshot is not newer than the last completed real run. Same gates as a real run; the plan fingerprint carries the mode, so a sync fingerprint cannot approve it."""
    newest = max(report.snapshot.synced_at_max.values(), default=None)
    with session_scope() as s:
        last = s.scalar(select(func.max(PushRun.finished_at)).where(PushRun.target == target, *_REAL))
    if newest and last and newest > last:
        raise PushError(409, "stamp_snapshot_newer")  # QuickBooks data changed after the push: stamping would hide those differences
    if any(diff.requeued[st] for st in steps):
        raise PushError(409, "stamp_requires_all_present")
    sel = {st: diff.nulls[st] for st in steps}
    pfp = plan_fingerprint(built.plan, sel, "stamp")
    stamped = {st: len(v) for st, v in sel.items()}
    run = PushRun(target=target, requested_by="admin-cli", options={"steps": steps, "dry_run": dry_run, "mode": "stamp", "plan_fingerprint": pfp}, status="done",
                  finished_at=utcnow(), preflight_fingerprint=report.fingerprint, summary={"stamped": stamped})
    if dry_run:
        with session_scope() as s:
            s.add(run)
    else:
        if pfp != plan_fp:
            raise PushError(409, "plan_fingerprint_mismatch")
        hashes = {st: {k: payload_hash(r) for k, r in getattr(built.plan, PLAN_ATTR[st])} for st in steps}
        with session_scope() as s:  # one transaction
            s.add(run)
            s.flush()
            for st in steps:
                if sel[st]:
                    s.connection().execute(update(PushedItem).where(PushedItem.target == target, PushedItem.entity == ENTITY[st], PushedItem.key == bindparam("k"), PushedItem.payload_hash.is_(None))
                              .values(payload_hash=bindparam("h"), last_run_id=run.id), [{"k": k, "h": hashes[st][k]} for k in sel[st]])
    return {"run_id": run.id, "status": "done", "dry_run": dry_run, "mode": "stamp", "stamped": stamped, "verdict": report.verdict, "plan_fingerprint": pfp,
            "preflight_fingerprint": report.fingerprint}


def start(settings: Settings, steps: list[str], dry_run: bool, fingerprint: Optional[str], plan_fp: Optional[str], kw: dict[str, Any], mode: str = "auto",
          acknowledge_foreign: Optional[str] = None, stamp_only: bool = False, acknowledge_requeue: Optional[dict[str, int]] = None,
          acknowledge_delete: Optional[dict[str, int]] = None, requested_by: str = "admin-cli") -> dict[str, Any]:
    steps = [s for s in STEPS if s in steps]
    if stamp_only and "bom" in steps:
        raise PushError(422, "stamp_has_no_bom_step")
    if stamp_only and settings.stocktrim_environment != "test":
        raise PushError(409, "stamp_test_only")  # a stamp trusts the operator that StockTrim holds what was pushed: only ever on the TEST account
    if not dry_run:
        if _SHUTTING_DOWN.is_set():
            raise PushError(503, "shutting_down")
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
        target = stocktrim_target(settings)
        completed = _completed(target)
        mode = ("sync" if completed else "initial") if mode == "auto" else mode
        if mode == "sync" and not completed:
            raise PushError(409, "no_completed_initial_run")  # a diff needs a baseline
        if stamp_only and mode != "sync":
            raise PushError(409, "stamp_needs_sync_mode")
        opts = {"steps": steps, "dry_run": dry_run, "mode": mode, "negative_stock_policy": settings.negative_stock_policy}
        ledger = _ledger(target)  # not None = the account may hold what an earlier run of ours pushed
        if mode == "initial" and ledger and any(ledger[st] for st in steps if st in LEDGER_STEPS):  # before the minutes-long StockTrim reads; dry-runs too (same 409)
            raise PushError(409, "step_already_pushed")  # re-pushing master data on a used target is the incremental sync (mode sync/auto); only BOM edges are diffed in initial mode
        exists: dict[str, Any] = {}
        report = build_report(settings, PreflightOptions(), kw, ledger, exists, acknowledge_foreign)
        if acknowledge_foreign and acknowledge_foreign != report.verdict.foreign_hash:
            raise PushError(409, "acknowledge_foreign_mismatch")
        if acknowledge_foreign and settings.stocktrim_environment == "production" and any(w.code == "foreign_in_plan" for w in report.verdict.warnings):
            raise PushError(409, "foreign_in_plan_production")  # the push would overwrite (adopt) foreign records: never acknowledgeable in production
        if not dry_run:
            if not report.verdict.ok_to_push:
                raise PushError(409, "preflight_not_ok")
            if report.fingerprint != fingerprint:
                raise PushError(409, "preflight_fingerprint_mismatch")
        built, diff = None, None
        if mode == "sync" and not stamp_only:  # the sync guards need the diff, so the plan is built here (dry-run and real run alike)
            _requires_step(target, steps, exists)
            built = build_plan(settings, steps)
            diff = compute_diff(target, built, steps, exists)
            _mass_requeue(diff, acknowledge_requeue)
            _mass_delete(diff, acknowledge_delete, target)
        if stamp_only:
            built = build_plan(settings, steps)
            return _stamp(built, target, steps, dry_run, report, compute_diff(target, built, steps, exists), plan_fp)
        if not dry_run:
            run = PushRun(target=target, requested_by=requested_by, options={**opts, "plan_fingerprint": plan_fp}, preflight_fingerprint=fingerprint, status="running")
            try:
                with session_scope() as s:
                    s.add(run)
            except IntegrityError as e:
                raise PushError(409, "push_running") from e
            _launch(run.id, settings, kw, None, exists)
            launched = True  # the thread now owns the lock
            return {"run_id": run.id, "status": "running", "dry_run": False, "mode": mode}
        built = built or build_plan(settings, steps)
        pfp = plan_fingerprint(built.plan, diff.sel if diff else steps)
        plan_counts = {**built.plan.counts, "dropped_supplier_links": sum(w.count for w in report.verdict.warnings if w.code == "unresolved_vendor")}
        extra: dict[str, Any] = {"mode": mode}
        plan_counts |= built.plan.order_stats
        if "bom" in steps:
            post, gone = (diff.bom_post, diff.bom_gone) if diff else bom_diff(target, built)
            plan_counts |= {"bom_to_post": len(post), "bom_to_delete": len(gone)}
        if diff:
            extra |= {"sync": _sync_summary(diff)}
        run = PushRun(target=target, requested_by=requested_by, options={**opts, "plan_fingerprint": pfp}, status="done", finished_at=utcnow(),
                      preflight_fingerprint=report.fingerprint, summary={**plan_counts, **extra})
        with session_scope() as s:
            s.add(run)
        return {"run_id": run.id, "status": "done", "dry_run": True, "mode": mode, "plan": plan_counts, "verdict": report.verdict, "plan_fingerprint": pfp,
                "preflight_fingerprint": report.fingerprint,
                **({"sync": extra["sync"], "planned_calls": diff.calls, "eta_seconds": math.ceil(diff.calls / settings.stocktrim_max_rps)} if diff else {})}
    finally:
        if not dry_run and not launched:
            _ACTIVE.release()


def resume(settings: Settings, run_id: UUID, kw: dict[str, Any]) -> dict[str, Any]:
    if _SHUTTING_DOWN.is_set():
        raise PushError(503, "shutting_down")
    if not settings.stocktrim_writes_enabled:
        raise PushError(403, "stocktrim_writes_disabled")
    row = _run_row(run_id)
    if row.options.get("dry_run") or row.options.get("mode") == "stamp" or row.status not in ("interrupted", "cancelled", "failed", "done_with_failures") or not counts(run_id):
        raise PushError(409, "run_not_resumable")
    if row.target != stocktrim_target(settings):
        raise PushError(409, "target_mismatch")
    if not _ACTIVE.acquire(blocking=False):
        raise PushError(409, "push_running")
    try:
        built = build_plan(settings, row.options["steps"])
        sel: Any = row.options["steps"]
        if row.options.get("mode") == "sync":  # a sync run was approved for exactly the keys of its items
            sel = {}
            with session_scope() as s:
                for ent, k in s.execute(select(PushItem.entity, PushItem.key).where(PushItem.run_id == run_id)):
                    sel.setdefault(next(st for st in STEPS if ENTITY[st] == ent), []).append(k)
        if plan_fingerprint(built.plan, sel) != row.options.get("plan_fingerprint"):
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
    _SHUTTING_DOWN.clear()
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
    calls = math.ceil((pending.get("supplier", 0) + pending.get("sales_order_line", 0)) / CHUNK) + sum(pending.get(e, 0) for e in ("product", "bom_edge", "customer", "purchase_order"))
    return {"run_id": run_id, "status": row.status, "dry_run": bool(row.options.get("dry_run")), "steps": row.options.get("steps", []), "started_at": row.started_at,
            "updated_at": row.updated_at, "finished_at": row.finished_at, "counts": c, "error_codes": codes, "eta_seconds": math.ceil(calls / settings.stocktrim_max_rps) if row.status == "running" else None,
            "summary": row.summary, "samples": samples}
