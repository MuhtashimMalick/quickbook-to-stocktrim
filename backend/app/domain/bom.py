"""BOM planner (plan 0.5/0.10): one edge per parent->component, repeats summed, bad edges/cycles rejected, bottom-up order."""
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from .from_qb import as_list, first_dict
from .models import BomEdge, Issue, IssueCode, Product, QuarantineResult


class BomPlan(BaseModel):
    edges_by_parent: dict[str, list[BomEdge]] = Field(default_factory=dict)
    fingerprint_by_parent: dict[str, str] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)  # parents, deepest sub-assemblies first
    quarantined_parents: list[str] = Field(default_factory=list)  # withheld: cycle members AND their ancestors
    issues: list[Issue] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)  # ints, plus depth_histogram {level: count}


def fingerprint(edges: Iterable[BomEdge]) -> str:
    """Stable hash of sorted (component, quantity); '2' and '2.0' hash equal."""
    rows = sorted((e.component_sku, format(e.quantity.normalize(), "f")) for e in edges)
    return hashlib.sha256("\n".join(f"{c}\t{q}" for c, q in rows).encode()).hexdigest()


def _cyclic(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (self-loops are removed earlier). Iterative Tarjan."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    for root in graph:
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(graph[root])))]
        while work:
            node, it = work[-1]
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))
    return out


def _ancestors(parents_of: dict[str, set[str]], members: list[str]) -> set[str]:
    """Reverse transitive closure of `members` (excluding them), iterative."""
    seen = set(members)
    todo = list(members)
    while todo:
        for p in parents_of.get(todo.pop(), ()):
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return seen.difference(members)


def plan_bom(products: QuarantineResult[Product], assemblies: Iterable[dict[str, Any]], extra_components: Iterable[Product] = (),
             unusable_list_ids: Iterable[str] = ()) -> BomPlan:
    """`products`: the key-validated products (ok = pushable, quarantined = known but excluded). `assemblies`: raw
    ItemInventoryAssembly payloads. `unusable_list_ids`: ListIDs of records that failed to parse (`{i.ref for i in snapshot.issues}`);
    treated like quarantined ones. Components not found (non-inventory types, unknown) are EXCLUDED and counted (policy A12);
    pass `extra_components` (already key-validated products of other item types) to include them later.

    Cycles: every cycle member AND every ancestor of one (a parent whose BOM would reference a withheld BOM) is withheld and listed in
    `quarantined_parents` (ancestors also counted in stats `cycle_ancestors`). Descendants below a cycle stay pushable.
    Stats: `max_depth`/`depth_histogram` count the longest chain of pushed BOMs (an assembly is a level only if it has at least one
    pushed edge). `summed_duplicates` counts LINES merged into an earlier line; `assemblies_with_summed_duplicates` counts assemblies.
    `subassemblies_without_bom`: assemblies used as a component of a pushed edge that have no pushed BOM of their own."""
    by_lid = {p.list_id: p for p in [*products.ok, *extra_components] if p.list_id}
    bad_lids = ({q.record.list_id for q in products.quarantined} | set(unusable_list_ids)) - {""}
    lid_of = {p.sku: p.list_id for p in by_lid.values()}
    plan = BomPlan()
    issues = plan.issues
    summed = excluded = dropped = n_assemblies = no_lines = all_excluded = with_summed = 0
    sums: dict[str, dict[str, Decimal]] = {}

    def issue(code: IssueCode, ref: str, severity: str, msg: str) -> None:
        issues.append(Issue(entity="bom", ref=ref, code=code, severity=severity, message=msg))  # type: ignore[arg-type]

    for a in assemblies:
        n_assemblies += 1
        lines = [ln for ln in as_list(a.get("ItemInventoryAssemblyLine")) if isinstance(ln, dict)]
        no_lines += not lines
        parent = by_lid.get(a.get("ListID") or "")
        if parent is None:
            if (a.get("ListID") or "") in bad_lids:
                excluded += len(lines)
                all_excluded += bool(lines)
                issue("bom_component_excluded", a["ListID"], "warning", "assembly is quarantined or unparseable: its BOM is not pushed")
            continue
        comps = sums.setdefault(parent.sku, {})
        n_excluded = n_summed = 0
        for ln in lines:
            lid = first_dict(ln.get("ItemInventoryRef")).get("ListID") or ""
            comp = by_lid.get(lid)
            if comp is None:
                n_excluded += 1
                if lid in bad_lids:
                    issue("bom_component_excluded", parent.list_id, "warning", f"component of {parent.sku!r} is a quarantined or unparseable product")
                else:
                    issue("bom_component_unresolved", parent.list_id, "warning", f"component of {parent.sku!r} is not an inventory/assembly item")
                continue
            if comp.list_id == parent.list_id:
                dropped += 1
                issue("bom_self_reference", parent.list_id, "warning", f"{parent.sku!r} lists itself as a component; line dropped")
                continue
            try:
                qty = Decimal(str(ln.get("Quantity")).strip())
                ok = qty.is_finite() and qty > 0
            except InvalidOperation:
                ok = False
            if not ok:
                dropped += 1
                issue("nonpositive_quantity", parent.list_id, "warning", f"line {comp.sku!r} of {parent.sku!r} has no positive quantity; dropped")
                continue
            if comp.sku in comps:
                n_summed += 1
                comps[comp.sku] += qty
            else:
                comps[comp.sku] = qty
        excluded += n_excluded
        summed += n_summed
        with_summed += bool(n_summed)
        all_excluded += bool(lines) and n_excluded == len(lines)

    graph = {p: set(c) for p, c in sums.items() if c}
    all_parents_of: dict[str, set[str]] = defaultdict(set)
    for p, comps in graph.items():
        for c in comps:
            all_parents_of[c].add(p)
    cyclic: set[str] = set()
    ancestors: dict[str, set[str]] = defaultdict(set)  # ancestor -> cycle members it depends on
    for members in _cyclic(graph):
        cyclic.update(members)
        for sku in members:
            issue("bom_cycle", lid_of[sku], "quarantine", f"{sku!r} is in a BOM cycle with: {', '.join(repr(m) for m in members)}")
        for anc in _ancestors(all_parents_of, members):
            ancestors[anc].update(members)
    for anc in ancestors.keys() - cyclic:
        shown = sorted(ancestors[anc])
        more = f" (+{len(shown) - 5} more)" if len(shown) > 5 else ""
        issue("bom_cycle_ancestor", lid_of[anc], "quarantine",
              f"{anc!r} depends on a BOM cycle ({', '.join(repr(m) for m in shown[:5])}{more}); withheld")
    withheld = cyclic | ancestors.keys()
    plan.quarantined_parents = sorted(withheld)
    for sku in withheld:
        graph.pop(sku)

    # Kahn, bottom-up: a parent is ready when every component that is itself a parent has been emitted.
    parents_of: dict[str, list[str]] = defaultdict(list)
    waiting = {p: 0 for p in graph}
    for p, comps in graph.items():
        for c in comps:
            if c in graph:
                parents_of[c].append(p)
                waiting[p] += 1
    level = {p: 1 for p, n in waiting.items() if n == 0}
    queue = sorted(level)
    while queue:
        nxt: list[str] = []
        for c in queue:
            plan.order.append(c)
            for p in parents_of[c]:
                level[p] = max(level.get(p, 1), level[c] + 1)
                waiting[p] -= 1
                if waiting[p] == 0:
                    nxt.append(p)
        queue = sorted(nxt)

    for p in plan.order:
        edges = [BomEdge(parent_sku=p, component_sku=c, quantity=q) for c, q in sorted(sums[p].items())]
        plan.edges_by_parent[p] = edges
        plan.fingerprint_by_parent[p] = fingerprint(edges)
    kind_of = {p.sku: p.kind for p in by_lid.values()}
    used = {e.component_sku for es in plan.edges_by_parent.values() for e in es}
    plan.stats = {
        "assemblies": n_assemblies, "assemblies_with_edges": len(plan.order), "edges": sum(len(e) for e in plan.edges_by_parent.values()),
        "summed_duplicates": summed, "assemblies_with_summed_duplicates": with_summed, "excluded_lines": excluded, "dropped_edges": dropped,
        "quarantined_parents": len(withheld), "cycle_ancestors": len(withheld - cyclic), "max_depth": max(level.values(), default=0),
        "depth_histogram": dict(sorted(Counter(level.values()).items())), "assemblies_without_lines": no_lines,
        "assemblies_all_lines_excluded": all_excluded, "subassemblies_without_bom": sum(1 for c in used if kind_of.get(c) == "assembly" and c not in graph),
    }
    return plan
