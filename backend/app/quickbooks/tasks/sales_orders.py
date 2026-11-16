from .base import QuerySpec, QueryTask


class SalesOrdersTask(QueryTask):
    job_type = "sales_orders"
    specs = [QuerySpec("SalesOrder", extra={"IncludeLineItems": True})]
