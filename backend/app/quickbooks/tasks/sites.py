from .base import QuerySpec, QueryTask


class SitesTask(QueryTask):
    job_type = "sites"
    # Needs Advanced Inventory; if QB rejects it the job is marked "unsupported" (3100/3250).
    # Iterator support for ItemSitesQueryRq must be confirmed by the captured fixture; if QB
    # rejects the iterator the session reports a QB error and the job is marked "error".
    specs = [QuerySpec("InventorySite", paginate=False), QuerySpec("ItemSites")]
