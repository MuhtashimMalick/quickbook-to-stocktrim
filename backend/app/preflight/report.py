"""Pre-flight dry run: what QuickBooks would push and what is wrong with it, what StockTrim already holds, and a verdict.
Read-only on both sides (the StockTrim client refuses any non-GET). Nothing here is persisted; INFO logs carry counts only. Issue
messages and record fields are never copied into the report (Vendor Names can be personal names): samples are `{ref, code}` with ref =
QuickBooks ListID, and the response is the typed `PreflightReport` (extra fields cannot reach the JSON).
ponytail: the 'one run at a time' lock lives in the route; the process-wide rate limiter per StockTrim base URL lives in app/stocktrim/limiter.py (every client shares it)."""
import hashlib
import json
import logging
import math
from collections import defaultdict
from contextlib import closing
from typing import Any, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import session_scope
from ..domain.bom import plan_bom
from ..domain.from_qb import load_snapshot
from ..domain.keys import norm, resolve_customers, resolve_products, resolve_suppliers
from ..domain.models import Issue
from ..models import QBRecord
from ..stocktrim.api import StockTrimApi
from ..stocktrim.client import StockTrimClient, read_only_guard
from ..stocktrim.errors import StockTrimConfigError, StockTrimError
from .schema import PreflightReport
from .stocktrim_inventory import Ledger, Pairs, inventory

log = logging.getLogger("app.preflight")
ENTITIES = ("Customer", "Vendor", "ItemInventory", "ItemInventoryAssembly")


class PreflightOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stocktrim: bool = True
    check_suppliers: bool = True  # GET /api/SuppliersBulk (one call); false leaves the suppliers unchecked, which blocks
    check_suppliers_by_code: bool = False  # optional cross-check: one GET ?code= per supplier (~382 calls)
    check_sales_orders: bool = False  # slow: one GET per product (~2,346 calls, ~8 minutes at 5 rps)
    sample_limit: int = Field(50, ge=0, le=500)


class Keys(NamedTuple):
    """(key, ListID) pairs of the pushable records: StockTrim is probed by key, the report only ever shows the ListID."""
    products: Pairs
    suppliers: Pairs
    customers: Pairs


def qb_report(session: Session, key_field: Any, limit: int) -> tuple[dict[str, Any], Keys, dict[str, Any]]:
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})  # one consistent snapshot for every read below
    synced = dict(session.execute(select(QBRecord.entity, func.max(QBRecord.synced_at)).where(QBRecord.entity.in_(ENTITIES)).group_by(QBRecord.entity)).tuples().all())
    snap = load_snapshot(session, key_field)
    sup = resolve_suppliers(snap.suppliers)
    prod = resolve_products(snap.products, sup)
    cust = resolve_customers(snap.customers)
    bom = plan_bom(prod, snap.assemblies, unusable_list_ids={i.ref for i in snap.issues})
    parse_fail: dict[str, int] = defaultdict(int)  # parse failures never reach the resolvers
    for i in snap.issues:
        parse_fail["products" if i.entity.startswith("ItemInventory") else "suppliers" if i.entity == "Vendor" else "customers"] += 1
    entities = {}
    for name, parsed, res in (("products", snap.products, prod), ("suppliers", snap.suppliers, sup), ("customers", snap.customers, cust)):
        entities[name] = {"parsed": len(parsed), "ok": len(res.ok), "quarantined": len(res.quarantined) + parse_fail[name], "parse_errors": parse_fail[name],
                          "warnings": len(res.warnings), **({"inactive": len(res.inactive)} if name == "suppliers" else {})}
    every: list[Issue] = [*snap.issues, *bom.issues, *(i for r in (prod, sup, cust) for q in r.quarantined for i in q.issues), *(w for r in (prod, sup, cust) for w in r.warnings)]
    groups: dict[str, dict[str, Any]] = {}
    for i in every:
        g = groups.setdefault(f"{i.entity}.{i.code}", {"severity": i.severity, "count": 0, "samples": []})
        g["count"] += 1
        if len(g["samples"]) < limit:
            g["samples"].append({"ref": i.ref, "code": i.code})
    qb = {"entities": entities, "issues": dict(sorted(groups.items())), "bom": bom.stats,
          "negative_stock_count": sum(1 for p in prod.ok if p.quantity_on_hand is not None and p.quantity_on_hand < 0)}
    log.info("preflight: QuickBooks entities=%s bom=%s negative_stock=%d", entities, bom.stats, qb["negative_stock_count"])
    keys = Keys([(p.sku, p.list_id) for p in prod.ok], [(s.code, s.list_id) for s in sup.ok], [(c.code, c.list_id) for c in cust.ok])
    return qb, keys, {"synced_at_max": synced, "bom": sorted(bom.fingerprint_by_parent.items())}


def _issue_count(qb: dict[str, Any], code: str) -> int:
    return sum(g["count"] for k, g in qb["issues"].items() if k.endswith("." + code))


def verdict(qb: dict[str, Any], st: dict[str, Any], sync: bool = False, acked: bool = False, foreign_in_plan: int = 0) -> dict[str, Any]:
    """ok_to_push is NOT permission to push. It is never true while any check is incomplete. `sync` (the account may hold what we pushed earlier):
    only FOREIGN products/suppliers/customers count as existing (purchase orders are then a warning, `purchase_orders_present`; they block the initial load), and a readable-code overlap
    is then covered by that count; `acked` (the caller's acknowledge_foreign equals the foreign hash) stops foreign records from counting; `foreign_in_plan` = foreign
    records that share a planned key (a push would overwrite them): warning."""
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add(into: list, code: str, count: int, **extra: Any) -> None:
        if count:
            into.append({"code": code, "count": count, **extra})

    ents, bom = qb["entities"], qb["bom"]
    add(blocking, "qb_snapshot_empty", int(ents["products"]["ok"] == 0))
    add(blocking, "qb_products_quarantined", ents["products"]["quarantined"])
    add(blocking, "qb_suppliers_quarantined", ents["suppliers"]["quarantined"])
    add(blocking, "bom_cycle_withheld", bom.get("quarantined_parents", 0))
    add(warnings, "qb_customers_quarantined", ents["customers"]["quarantined"])
    for code in ("internal_double_space", "non_ascii_key", "key_too_long", "unresolved_vendor", "bom_component_excluded", "bom_component_unresolved"):
        add(warnings, code, _issue_count(qb, code))
    add(warnings, "negative_stock", qb["negative_stock_count"])
    add(warnings, "subassemblies_without_bom", bom.get("subassemblies_without_bom", 0))
    status = st["status"]
    if status in ("skipped", "not_configured", "unreachable_or_bad_credentials"):
        add(blocking, f"stocktrim_{status}", 1)
        return {"ok_to_push": False, "blocking": blocking, "warnings": warnings}
    steps = {n: v for n, v in st.items() if isinstance(v, dict) and n != "boms"}
    incomplete = {n: v.get("errors") or 1 for n, v in steps.items() if v.get("errors") or v.get("capped") or v["status"] == "aborted"}
    add(blocking, "stocktrim_incomplete", len(incomplete), detail=incomplete)
    add(blocking, "stocktrim_suppliers_not_checked", int(steps.get("suppliers", {}).get("status") == "skipped"))
    add(warnings, "sales_orders_not_checked", int(steps.get("sales_orders", {}).get("status") == "skipped"))  # StockTrim keeps sales orders of unknown products: no products != no sales orders
    existing = {n: (0 if acked else v["foreign"]) if sync and "foreign" in v else v.get("count", v.get("found", 0)) for n, v in steps.items() if v["status"] == "ok"}
    if sync:  # only POs that are not in our ledger count here (an acknowledgement does not silence them)
        po = existing.pop("purchase_orders", None) is not None and steps["purchase_orders"]
        add(warnings, "purchase_orders_present", po.get("foreign", po.get("count", 0)) if po else 0)
    add(warnings, "foreign_in_plan", foreign_in_plan)
    p_id = 0 if sync else steps.get("products", {}).get("overlap_product_id", {}).get("count", 0)
    add(blocking, "stocktrim_not_empty", sum(existing.values()), detail={**{k: v for k, v in existing.items() if v}, **({"products_overlap_product_id": p_id} if p_id else {})})
    add(blocking, "stocktrim_readable_conflict", 0 if sync else steps.get("products", {}).get("overlap_readable", {}).get("count", 0))  # duplicate readable codes make BOM keys ambiguous
    return {"ok_to_push": not blocking, "blocking": blocking, "warnings": warnings}


def preflight_client(settings: Settings, client_kw: dict[str, Any], **kw: Any) -> StockTrimClient:
    """The ONLY way the pre-flight talks to StockTrim: a client whose request hook refuses anything but GET (even if code calls it directly)."""
    return StockTrimClient.from_settings(settings, **{"event_hooks": {"request": [read_only_guard]}, **kw, **client_kw})


def _stocktrim(settings: Settings, opts: PreflightOptions, keys: Keys, client_kw: dict[str, Any], ledger: Optional[Ledger], sink: Optional[dict]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not opts.stocktrim:
        return {"status": "skipped"}, {}
    try:  # credential check on its own short-retry client (a wrong api-auth-id answers 500 and would retry for ~15 s)
        with closing(preflight_client(settings, client_kw, max_attempts=2)) as c:
            first = StockTrimApi(c).get_products(page_no=1)  # unknown api-auth-id -> 500 + empty body, bad signature -> 401; page 1 is reused below
    except StockTrimConfigError:
        return {"status": "not_configured"}, {}
    except StockTrimError as e:
        log.info("preflight: StockTrim credential check failed (%s)", type(e).__name__)
        return {"status": "unreachable_or_bad_credentials", "error": type(e).__name__}, {}
    with closing(preflight_client(settings, client_kw)) as c:
        return inventory(StockTrimApi(c), first, *keys, check_suppliers=opts.check_suppliers, check_suppliers_by_code=opts.check_suppliers_by_code,
                         check_sales_orders=opts.check_sales_orders, limit=opts.sample_limit, ledger=ledger,
                         probe_exempt=settings.stocktrim_environment == "test", sink=sink)


def _plan(keys: Keys, bom_edges: int, rps: float) -> dict[str, Any]:
    """Upper bound for a later push, one call per record (batches would be fewer); the pre-flight's own reads are not included."""
    calls = {"products": len(keys.products), "suppliers": len(keys.suppliers), "customers": len(keys.customers), "bom_edges": bom_edges}
    total = sum(calls.values())
    return {"planned_calls": calls, "total_calls": total, "rps": rps, "eta_seconds": math.ceil(total / rps)}


def foreign_in_plan(sink: dict[str, Any], keys: Keys) -> int:
    """Foreign StockTrim records (normalised keys) that are ALSO planned keys: a push would overwrite (adopt) them."""
    return sum(len(set(v["foreign"]) & {norm(k) for k, _ in getattr(keys, e)}) for e, v in sink.items())


def foreign_hash(sink: dict[str, Any], in_plan: int = 0) -> Optional[str]:
    """sha256 over the sorted JSON list of 'entity:normalised key' of every foreign StockTrim record, plus a distinct 'foreign_in_plan:N' entry when N > 0 (None when there is none)."""
    bad = sorted(f"{e}:{k}" for e, v in sink.items() for k in v["foreign"])
    return hashlib.sha256(json.dumps(bad + ([f"foreign_in_plan:{in_plan}"] if in_plan else [])).encode()).hexdigest() if bad else None


def build_report(settings: Settings, opts: PreflightOptions, client_kw: Optional[dict[str, Any]] = None, ledger: Optional[Ledger] = None,
                 sink: Optional[dict[str, Any]] = None, acknowledge_foreign: Optional[str] = None) -> PreflightReport:
    """`ledger` (push engine only) = sync mode, see stocktrim_inventory.inventory; `sink` receives the StockTrim key sets; `acknowledge_foreign` = the foreign_hash of an earlier verdict."""
    sink = {} if sink is None else sink
    with session_scope() as s:  # released before the (slow) StockTrim reads
        qb, keys, snap = qb_report(s, settings.product_key_field, opts.sample_limit)
    st, facts = _stocktrim(settings, opts, keys, client_kw or {}, ledger, sink)
    fip = foreign_in_plan(sink, keys)
    fh = foreign_hash(sink, fip)
    # fingerprint: a later push can require it to be unchanged (pushable keys, BOM edges and what StockTrim held, incl. full overlap lists)
    fp = hashlib.sha256(json.dumps([sorted(keys.products), sorted(keys.suppliers), snap["bom"], facts], sort_keys=True, default=str).encode()).hexdigest()
    return PreflightReport.model_validate({"quickbooks": qb, "stocktrim": st, "verdict": {**verdict(qb, st, ledger is not None, fh is not None and fh == acknowledge_foreign, fip), "foreign_hash": fh}, "snapshot": {"synced_at_max": snap["synced_at_max"]},
                                           "plan": _plan(keys, qb["bom"]["edges"], settings.stocktrim_max_rps), "fingerprint": fp})
