from .base import QuerySpec, QueryTask


class ItemsTask(QueryTask):
    job_type = "items"
    specs = [QuerySpec("ItemInventory", active_status="All")]


class OtherItemsTask(QueryTask):
    job_type = "other_items"
    # each QuickBooks item type keeps its own entity name so per-entity prune never crosses types
    specs = [QuerySpec(e, active_status="All") for e in ("ItemNonInventory", "ItemService", "ItemOtherCharge")]
