from .base import QuerySpec, QueryTask


class ItemsTask(QueryTask):
    job_type = "items"
    specs = [QuerySpec("ItemInventory")]
