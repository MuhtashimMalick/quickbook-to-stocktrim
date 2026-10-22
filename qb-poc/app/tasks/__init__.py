from .assemblies import AssembliesTask
from .base import JobTask
from .customers import CustomersTask
from .items import ItemsTask
from .purchase_orders import CreateTestPOTask, PurchaseOrdersTask
from .sales_orders import SalesOrdersTask
from .sites import SitesTask
from .vendors import VendorsTask

READ_TASKS: list[type[JobTask]] = [CustomersTask, VendorsTask, ItemsTask, AssembliesTask, SitesTask, SalesOrdersTask, PurchaseOrdersTask]
READ_JOB_TYPES = [t.job_type for t in READ_TASKS]
WRITE_JOB_TYPES = {CreateTestPOTask.job_type}
JOB_TYPES: dict[str, type[JobTask]] = {t.job_type: t for t in [*READ_TASKS, CreateTestPOTask]}

# job type -> QB entity names stored in qb_records
ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "customers": ("Customer",),
    "vendors": ("Vendor",),
    "items": ("ItemInventory",),
    "assemblies": ("ItemInventoryAssembly",),
    "sites": ("InventorySite", "ItemSites"),
    "sales_orders": ("SalesOrder",),
    "purchase_orders": ("PurchaseOrder",),
    "create_test_po": ("PurchaseOrder",),
}
