"""Key validation + collision detection (plan 0.10).

StockTrim ids are case-insensitive and `GET ?code=` ignores TRAILING spaces but not leading spaces/tabs, so: keys with
edge whitespace, control characters or no content are quarantined; keys that collide on casefold + trailing-strip are
ALL quarantined (comparison uses NFKC + casefold; the key itself is never altered); length/double-space/non-ASCII keys are kept exactly and only reported.
"""
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import TypeVar

from .models import Customer, Issue, IssueCode, Product, Quarantined, QuarantineResult, Supplier

MAX_KEY_LEN = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
T = TypeVar("T")


def norm(key: str) -> str:
    """Comparison form ONLY (the pushed key stays exact): trailing spaces stripped, NFKC, casefold ('Straße' == 'STRASSE')."""
    return unicodedata.normalize("NFKC", key.rstrip(" ")).casefold()


def key_problems(key: str) -> list[tuple[IssueCode, str, str]]:
    """(code, severity, message) for one key; independent of other records."""
    if not key.strip():
        return [("empty_key", "quarantine", "key is empty or blank")]
    out: list[tuple[IssueCode, str, str]] = []
    if key != key.strip():
        out.append(("whitespace_key", "quarantine", f"key has leading/trailing whitespace: {key!r}"))
    if _CONTROL.search(key):
        out.append(("control_chars", "quarantine", f"key contains control characters: {key!r}"))
    if len(key) > MAX_KEY_LEN:
        out.append(("key_too_long", "warning", f"key is {len(key)} characters (> {MAX_KEY_LEN}): {key[:40]!r}..."))
    if "  " in key:
        out.append(("internal_double_space", "warning", f"key has an internal double space: {key!r}"))
    if not key.isascii():
        out.append(("non_ascii_key", "warning", f"key has non-ASCII characters: {key!r}"))
    return out


def resolve(records: Iterable[T], entity: str, key_of: Callable[[T], str], lid_of: Callable[[T], str]) -> QuarantineResult[T]:
    """Validate every key and ListID, then quarantine every member of each key-collision or duplicate-ListID group."""
    recs = list(records)
    groups: dict[str, list[int]] = defaultdict(list)
    lid_groups: dict[str, list[int]] = defaultdict(list)
    issues: list[list[Issue]] = []
    warnings: list[Issue] = []
    refs = [lid_of(r) or f"<no ListID>#{i}" for i, r in enumerate(recs)]  # a ref is a ListID (or this placeholder), never a name

    def add(i: int, code: IssueCode, severity: str, msg: str) -> None:
        issues[i].append(Issue(entity=entity, ref=refs[i], code=code, severity=severity, message=msg))  # type: ignore[arg-type]

    for i, r in enumerate(recs):
        key = key_of(r)
        issues.append([])
        for c, sev, m in key_problems(key):
            add(i, c, sev, m)
        if key.strip():
            groups[norm(key)].append(i)
        if lid_of(r):
            lid_groups[lid_of(r)].append(i)
        else:
            add(i, "empty_list_id", "quarantine", "record has no ListID")
    for idx in groups.values():
        if len(idx) > 1:
            keys = ", ".join(repr(key_of(recs[i])) for i in idx)
            for i in idx:
                add(i, "duplicate_key", "quarantine", f"{len(idx)} records collide case-insensitively: {keys}")
    for lid, idx in lid_groups.items():
        if len(idx) > 1:
            for i in idx:
                add(i, "duplicate_key", "quarantine", f"{len(idx)} records share ListID {lid!r}")
    result: QuarantineResult[T] = QuarantineResult()
    for r, mine in zip(recs, issues):
        if any(x.severity == "quarantine" for x in mine):
            result.quarantined.append(Quarantined(record=r, issues=mine))
        else:
            result.ok.append(r)
            warnings.extend(mine)
    result.warnings = warnings
    return result


def resolve_suppliers(suppliers: Iterable[Supplier]) -> QuarantineResult[Supplier]:
    """Phase 2's supplier push must use EXACTLY `.ok`: valid keys, unique, and IsActive. Inactive vendors are valid but go to
    `.inactive` (with an `inactive_supplier` warning) and are never pushed, so no product may link to them either."""
    res = resolve(suppliers, "supplier", lambda s: s.code, lambda s: s.list_id)
    res.inactive = [s for s in res.ok if not s.is_active]
    res.ok = [s for s in res.ok if s.is_active]
    res.warnings += [Issue(entity="supplier", ref=s.list_id, code="inactive_supplier", severity="warning", message=f"supplier {s.code!r} is inactive: not pushed")
                     for s in res.inactive]
    return res


def resolve_customers(customers: Iterable[Customer]) -> QuarantineResult[Customer]:
    """Customers are keyed by ListID (always unique); duplicate Names are only a warning. Like suppliers, the push must use exactly
    `.ok`: IsActive=false customers go to `.inactive` (with an `inactive_customer` warning) and are never pushed."""
    res = resolve(customers, "customer", lambda c: c.code, lambda c: c.list_id)
    res.inactive = [c for c in res.ok if not c.is_active]
    res.ok = [c for c in res.ok if c.is_active]
    res.warnings += [Issue(entity="customer", ref=c.list_id, code="inactive_customer", severity="warning", message="customer is inactive: not pushed")
                     for c in res.inactive]
    by_name: dict[str, list[str]] = defaultdict(list)
    for c in res.ok:
        by_name[c.name.strip().casefold()].append(c.list_id)
    for ids in by_name.values():
        if len(ids) > 1:
            res.warnings.append(Issue(entity="customer", ref=ids[0], code="duplicate_name", severity="warning",
                                      message=f"{len(ids)} customers share a name; ListIDs: {', '.join(ids)}"))
    return res


def resolve_products(products: Iterable[Product], suppliers: QuarantineResult[Supplier] | None = None) -> QuarantineResult[Product]:
    """One namespace across inventory + assembly items. With `suppliers`, the preferred vendor is replaced by the Vendor
    key via ListID (`preferred_vendor_code`, set only here). An unknown, quarantined or inactive vendor leaves it None (never push
    a supplier StockTrim would auto-create) with an `unresolved_vendor` warning."""
    res = resolve(products, "product", lambda p: p.sku, lambda p: p.list_id)
    if suppliers is not None:
        code_by_lid = {s.list_id: s.code for s in suppliers.ok}
        for i, p in enumerate(res.ok):
            if p.preferred_vendor_list_id is None:
                continue
            code = code_by_lid.get(p.preferred_vendor_list_id or "")
            if code is None:
                res.warnings.append(Issue(entity="product", ref=p.list_id or p.sku, code="unresolved_vendor", severity="warning",
                                          message=f"preferred vendor of {p.sku!r} is not among the pushable suppliers"))
            res.ok[i] = p.model_copy(update={"preferred_vendor_code": code})
    return res
