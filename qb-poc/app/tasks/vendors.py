from .base import QuerySpec, QueryTask


class VendorsTask(QueryTask):
    job_type = "vendors"
    specs = [QuerySpec("Vendor")]
