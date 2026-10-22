from typing import Any, Optional

from qbwc_kit import TaskContext

from .. import store
from .base import QuerySpec, QueryTask, as_list

MAX_DEPTH = 10


def _name(rec: dict[str, Any]) -> str:
    return rec.get("FullName") or rec.get("Name") or "?"


def _lines(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return as_list(rec.get("ItemInventoryAssemblyLine"))


def build_node(rec: dict[str, Any], qty: Optional[str], by_id: dict[str, dict[str, Any]], seen: frozenset, depth: int = 0) -> dict[str, Any]:
    """parent -> components with quantities; sub-assemblies are expanded, never flattened."""
    node: dict[str, Any] = {"list_id": rec.get("ListID"), "full_name": _name(rec), "quantity": qty, "is_assembly": True, "components": []}
    for line in _lines(rec):
        ref = line.get("ItemInventoryRef") or {}
        lid, q = ref.get("ListID"), line.get("Quantity")
        sub = by_id.get(lid)
        # cycle/depth guard: a cyclic or over-deep sub-assembly is shown as a leaf, not expanded.
        if sub is not None and lid not in seen and depth < MAX_DEPTH:
            node["components"].append(build_node(sub, q, by_id, seen | {lid}, depth + 1))
        else:
            node["components"].append({"list_id": lid, "full_name": ref.get("FullName"), "quantity": q, "is_assembly": sub is not None, "components": []})
    return node


def render_tree(node: dict[str, Any], depth: int = 0) -> list[str]:
    if depth == 0:
        lines = [node["full_name"]]
    else:
        tag = " [assembly]" if node["is_assembly"] else ""
        lines = [f"{' ' * (4 * depth - 2)}- {node['quantity']} x {node['full_name']}{tag}"]
    for c in node["components"]:
        lines += render_tree(c, depth + 1)
    return lines


class AssembliesTask(QueryTask):
    job_type = "assemblies"
    specs = [QuerySpec("ItemInventoryAssembly")]

    def after_fetch(self, ctx: TaskContext, records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        assemblies = records["ItemInventoryAssembly"]
        by_id = {a["ListID"]: a for a in assemblies if a.get("ListID")}
        used = {(ln.get("ItemInventoryRef") or {}).get("ListID") for a in assemblies for ln in _lines(a)}
        roots = [a for a in assemblies if a.get("ListID") not in used] or assemblies  # roots = not a component elsewhere
        tree = [build_node(a, None, by_id, frozenset({a.get("ListID")})) for a in roots]
        if tree:
            text = "\n".join(line for n in tree for line in render_tree(n))
            store.log_event(self.job_id, "info", "ItemInventoryAssembly", None, "assembly tree:\n" + text)
        return {"tree": tree}
