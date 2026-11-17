"""Read-only inventory of the StockTrim account for the pre-flight. GET calls only (the client is built with a guard, see report.py);
only counts and our own ListIDs leave this module, never StockTrim record content. A full sales-order check is ~1 call per product
(about 2,346 calls, ~8 minutes at 5 rps): it is opt-in. Our keys come as (key, list_id) pairs: probes use the key, samples report the ListID."""
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any, Optional

from ..domain.keys import norm
from ..stocktrim.api import StockTrimApi
from ..stocktrim.dto import PO_STATUSES, ProductResponse
from ..stocktrim.errors import StockTrimError

log = logging.getLogger("app.preflight")
PRODUCT_PAGE = 50  # StockTrim product page size (fixed server side; products are walked until an empty page)
PO_PAGE_SIZE = 50  # pageSize we ask for on the V2 purchase-order list
MAX_PAGES = 400  # safety cap per listing: 20,000 rows
Pairs = list[tuple[str, str]]  # (key, ListID)
PROBE_PREFIX = "zz-probe-"  # records our write probes created (documented, authorised): ours in sync mode
Ledger = dict[str, set[str]]  # sync mode: normalised keys we pushed per entity (suppliers, products, customers)


def _paged(fetch: Callable[[int], list], stop_below: Optional[int], first: Optional[list] = None) -> tuple[list, bool]:
    """(rows, capped): pages 1.. until an empty page (or one shorter than stop_below); `first` is an already fetched page 1."""
    rows: list = []
    for page in range(1, MAX_PAGES + 1):
        got = first if page == 1 and first is not None else fetch(page)
        rows += got
        if not got or (stop_below and len(got) < stop_below):
            return rows, False
    return rows, True


def _samples(refs: Iterable[str], limit: int, code: str) -> list[dict[str, str]]:
    return [{"ref": r, "code": code} for _, r in zip(range(limit), refs)]


def _overlap(refs: list[str], limit: int, code: str) -> dict[str, Any]:
    return {"count": len(refs), "samples": _samples(refs, limit, code)}


def _foreign(codes: Iterable[str], ours: Optional[set[str]]) -> dict[str, int]:
    """Sync mode only: how many StockTrim records are NOT in our ledger (and not a ZZ-PROBE record). Empty when not in sync mode."""
    return {} if ours is None else {"foreign": sum(1 for c in codes if norm(c) not in ours and not norm(c).startswith(PROBE_PREFIX))}


def _products(api: StockTrimApi, first: list[ProductResponse], ours: Pairs, limit: int, facts: dict, ledger: Optional[Ledger]) -> dict[str, Any]:
    rows, capped = _paged(lambda p: api.get_products(page_no=p), None, first)
    mine = {norm(k): lid for k, lid in ours}
    by_id = [mine[norm(r.product_id)] for r in rows if norm(r.product_id) in mine]
    readable = [r for r in rows if r.product_code_readable and norm(r.product_code_readable) in mine]
    by_read = [mine[norm(r.product_code_readable)] for r in readable]
    read_only = [mine[norm(r.product_code_readable)] for r in readable if norm(r.product_id) not in mine]
    dup = sum(1 for n in Counter(norm(r.product_code_readable) for r in rows if r.product_code_readable).values() if n > 1)
    facts["products"] = {"count": len(rows), "id": sorted(by_id), "readable": sorted(by_read)}
    return {"count": len(rows), "capped": capped, "overlap_product_id": _overlap(by_id, limit, "overlap_product_id"),
            "overlap_readable": _overlap(by_read, limit, "overlap_readable"), "overlap_readable_only": _overlap(read_only, limit, "overlap_readable_only"),
            "duplicate_readable_groups": dup, **_foreign((r.product_id for r in rows), ledger and ledger["products"]),
            "readable_differs_from_product_id": sum(1 for r in rows if r.product_code_readable and r.product_code_readable != r.product_id)}


def _customers(api: StockTrimApi, ours: Pairs, limit: int, facts: dict, ledger: Optional[Ledger]) -> dict[str, Any]:
    rows = api.get_customers()
    codes = [c.code for c in rows if c.code and c.code.strip()]
    mine = {norm(k): lid for k, lid in ours}
    clash = [mine[norm(c)] for c in codes if norm(c) in mine]
    facts["customers"] = {"count": len(rows), "clash": sorted(clash)}
    return {"count": len(rows), "code_collisions": len(clash), "samples": _samples(clash, limit, "code_collision"),
            "duplicate_code_groups": sum(1 for n in Counter(map(norm, codes)).values() if n > 1), **_foreign(codes, ledger and ledger["customers"])}


def _po_count(api: StockTrimApi, status: Optional[str]) -> tuple[int, bool]:
    """The paging base is not proven (page 0 vs 1) and a PO created without a status has integer status 0 that no filter finds:
    take the larger of the 1.. walk and one page=0 call."""
    rows, capped = _paged(lambda p: api.list_purchase_orders(page=p, page_size=PO_PAGE_SIZE, status=status), PO_PAGE_SIZE)
    return max(len(rows), len(api.list_purchase_orders(page=0, page_size=PO_PAGE_SIZE, status=status))), capped


def _purchase_orders(api: StockTrimApi, facts: dict) -> dict[str, Any]:
    by_status, capped = {}, False
    for s in (*PO_STATUSES, None):  # None = the unfiltered list
        by_status[s or "any"], cap = _po_count(api, s)
        capped = capped or cap
    count = max(sum(by_status[s] for s in PO_STATUSES), by_status["any"])
    facts["purchase_orders"] = {"count": count}
    return {"count": count, "by_status": by_status, "capped": capped}


def _suppliers(api: StockTrimApi, ours: Pairs, limit: int, facts: dict, ledger: Optional[Ledger]) -> dict[str, Any]:
    """GET /api/SuppliersBulk lists every supplier (6 on the TEST account), so no per-key probing is needed."""
    rows = api.list_suppliers_bulk()
    mine = {norm(k): lid for k, lid in ours}
    clash = [mine[norm(s.supplier_code)] for s in rows if norm(s.supplier_code) in mine]
    facts["suppliers"] = {"count": len(rows), "overlap": sorted(clash)}
    return {"count": len(rows), "overlap": len(clash), "samples": _samples(clash, limit, "overlap"), **_foreign((s.supplier_code for s in rows), ledger and ledger["suppliers"])}


def _probe(api: StockTrimApi, ours: Pairs, fn: Callable[[str], Any], limit: int, found_code: str, name: str, facts: dict) -> dict[str, Any]:
    """One GET per key through the rate-limited bulk helper; a fatal StockTrim error propagates (with partial_results). Samples carry the ListID."""
    results = api.map_concurrent(lambda kv: fn(kv[0]), ours)
    found = sorted(r.item[1] for r in results if r.ok and r.result)
    errors = sum(1 for r in results if not r.ok)
    facts[name] = {"found": len(found), "errors": errors}
    return {"checked": len(ours), "found": len(found), "errors": errors, "samples": _samples(found, limit, found_code)}


def inventory(api: StockTrimApi, first_products: list[ProductResponse], products: Pairs, suppliers: Pairs, customers: Pairs, *,
              check_suppliers: bool, check_suppliers_by_code: bool, check_sales_orders: bool, limit: int, ledger: Optional[Ledger] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(report section, facts for the fingerprint). `first_products` is page 1 from the credential check (not fetched twice).
    `ledger` switches on sync mode: products/customers/suppliers sections also report `foreign` (not pushed by us)."""
    facts: dict[str, Any] = {}

    def skipped() -> dict[str, Any]:
        return {"status": "skipped"}

    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("products", lambda: _products(api, first_products, products, limit, facts, ledger)),
        ("customers", lambda: _customers(api, customers, limit, facts, ledger)),
        ("purchase_orders", lambda: _purchase_orders(api, facts)),
        ("suppliers", (lambda: _suppliers(api, suppliers, limit, facts, ledger)) if check_suppliers else skipped),
    ]
    if check_suppliers_by_code:
        steps.append(("suppliers_by_code", lambda: _probe(api, suppliers, api.get_supplier, limit, "found", "suppliers_by_code", facts)))
    steps.append(("sales_orders", (lambda: _probe(api, products, api.get_sales_orders, limit, "has_orders", "sales_orders", facts)) if check_sales_orders else skipped))
    out: dict[str, Any] = {"status": "ok"}
    for name, step in steps:
        try:
            r = step()
            out[name] = r if "status" in r else {"status": "ok", **r}
        except StockTrimError as e:  # fatal mid-run or an unparseable listing: keep what was read, stop
            out[name] = {"status": "aborted", "error": type(e).__name__, **({"checked": sum(r.ok for r in e.partial_results)} if e.partial_results else {})}
            out["status"] = "aborted"
            break
    out["boms"] = {"status": "unknown_cannot_list"}
    log.info("preflight: StockTrim status=%s %s", out["status"], {k: v.get("count", v.get("found")) for k, v in out.items() if isinstance(v, dict) and k != "boms"})
    return out, facts
