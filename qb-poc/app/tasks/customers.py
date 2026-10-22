from .base import QuerySpec, QueryTask


class CustomersTask(QueryTask):
    job_type = "customers"
    specs = [QuerySpec("Customer")]
