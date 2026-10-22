from decimal import Decimal
from typing import Any

from qbwc_kit import TaskContext, qbxml

from .. import store
from .base import Flow, JobTask, Outcome, QuerySpec, QueryTask

MEMO = "Created by integration - Review Required"


class PurchaseOrdersTask(QueryTask):
    job_type = "purchase_orders"
    specs = [QuerySpec("PurchaseOrder", extra={"IncludeLineItems": True})]


class CreateTestPOTask(JobTask):
    """Idempotent PO create: look up by RefNumber first, only then Add."""

    job_type = "create_test_po"
    is_write = True

    def _done(self, po: dict[str, Any], message: str) -> Outcome:
        store.upsert_records("PurchaseOrder", [po], self.job_id)
        result = {"txn_id": po.get("TxnID"), "edit_sequence": po.get("EditSequence"), "ref_number": po.get("RefNumber")}
        return Outcome("done", message, result)

    def execute(self, ctx: TaskContext) -> Flow:
        if not self.settings.writes_enabled:
            return Outcome("error", "writes disabled")
        job = store.get_job(self.job_id)
        ref = job.ref_number if job else None
        if not ref:
            return Outcome("error", "job has no ref_number")

        # Step 1: did a previous attempt already create it? (no MaxReturned/iterator: RefNumber is a schema choice branch)
        rs = yield from self.send(ctx, qbxml.query("PurchaseOrder", extra={"RefNumber": ref}), "PurchaseOrder_refquery")
        if rs is None:
            return Outcome("error", "empty response to RefNumber lookup")
        if rs.status_code not in (0, 1, 500):
            return self.failure("PurchaseOrder", rs)
        found = next((r for r in rs.records if r.get("RefNumber") == ref), None) if rs.status_code == 0 else None
        if found:
            return self._done(found, "already existed; not re-created")

        # Step 2: add (keep schema element order).
        p = self.params
        add = qbxml.add("PurchaseOrder", {
            "VendorRef": {"ListID": p["vendor_list_id"]},
            "RefNumber": ref,
            "Memo": MEMO,
            "PurchaseOrderLineAdd": {
                "ItemRef": {"ListID": p["item_list_id"]},
                "Quantity": format(Decimal(p["quantity"]), "f"),
                "Rate": f"{Decimal(p['rate']):.2f}",
            },
        })
        rs = yield from self.send(ctx, add, "PurchaseOrderAdd")
        if rs is None:
            return Outcome("error", "add response lost; retry will verify by RefNumber")
        if rs.status_code != 0:
            return self.failure("PurchaseOrder", rs)
        po = rs.records[0] if rs.records else {}
        if not po.get("TxnID"):
            return Outcome("error", "add succeeded but response had no TxnID; retry will verify by RefNumber")
        return self._done(po, "created")
