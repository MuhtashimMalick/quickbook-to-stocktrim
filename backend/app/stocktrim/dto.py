"""StockTrim request/response models, hand-written from https://api.stocktrim.com/swagger/v1/swagger.json (info.version v1,
fetched 2026-11-16; only the endpoints we use). Requests: extra="forbid" (swagger additionalProperties:false) and serialised
with `payload()`, which omits None (never send nulls). Responses: tolerant (unknown fields ignored)."""
from typing import Annotated, Any, Literal, Optional

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

PO_STATUSES = ("Draft", "Approved", "Sent", "Received")


def _po_status(v: Any) -> Any:
    """The API returns the status as a name or as an integer 0-3 (possibly quoted); accept both, case-insensitively."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v)
    if isinstance(v, int) and 0 <= v < len(PO_STATUSES):
        return PO_STATUSES[v]
    if isinstance(v, str):
        return next((s for s in PO_STATUSES if s.lower() == v.strip().lower()), v)
    return v


PurchaseOrderStatus = Annotated[Literal["Draft", "Approved", "Sent", "Received"], BeforeValidator(_po_status)]
_CAMEL = dict(alias_generator=to_camel, populate_by_name=True)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", **_CAMEL)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class Response(BaseModel):
    model_config = ConfigDict(extra="ignore", **_CAMEL)


class ProductSupplier(Request):
    supplier_id: str = Field(min_length=1)
    supplier_name: Optional[str] = None
    supplier_sku_code: Optional[str] = None


class ProductRequest(Request):
    # product_code_readable and stock_on_hand are required by US, not swagger: omitting them resets/clears them in StockTrim
    product_id: str = Field(min_length=1, pattern=r"\S")
    product_code_readable: str = Field(min_length=1, pattern=r"\S")
    stock_on_hand: float
    name: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    service_level: Optional[float] = None
    lead_time: Optional[int] = None
    stock_on_order: Optional[float] = None
    cost: Optional[float] = None
    price: Optional[float] = None
    supplier_code: Optional[str] = None
    suppliers: Optional[list[ProductSupplier]] = None
    forecast_period: Optional[int] = None
    manufacturing_time: Optional[int] = None
    order_frequency: Optional[int] = None
    minimum_order_quantity: Optional[float] = None
    minimum_shelf_level: Optional[float] = None
    maximum_shelf_level: Optional[float] = None
    batch_size: Optional[float] = None
    barcode: Optional[str] = None
    discontinued: Optional[bool] = None
    unstocked: Optional[bool] = None
    option1: Optional[str] = None
    option2: Optional[str] = None
    option3: Optional[str] = None
    overridden_demand: Optional[float] = None
    overridden_demand_period: Optional[int] = None
    parent_id: Optional[str] = None
    variant_type: Optional[str] = None
    variant: Optional[str] = None
    ignore_seasonality: Optional[bool] = None
    weight: Optional[float] = None
    height: Optional[float] = None
    width: Optional[float] = None
    length: Optional[float] = None


class ProductSupplierResponse(Response):
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_sku_code: Optional[str] = None


class ProductResponse(Response):
    id: Optional[int] = None
    product_id: str
    product_code_readable: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    stock_on_hand: Optional[float] = None
    stock_on_order: Optional[float] = None
    cost: Optional[float] = None
    price: Optional[float] = None
    supplier_code: Optional[str] = None
    lead_time: Optional[int] = None
    barcode: Optional[str] = None
    discontinued: Optional[bool] = None
    unstocked: Optional[bool] = None
    minimum_shelf_level: Optional[float] = None
    maximum_shelf_level: Optional[float] = None
    suppliers: Optional[list[ProductSupplierResponse]] = None


class SupplierRequest(Request):
    supplier_code: str = Field(min_length=1, pattern=r"\S")
    supplier_name: Optional[str] = None
    email_address: Optional[str] = None
    primary_contact_name: Optional[str] = None
    external_id: Optional[str] = None
    default_lead_time: Optional[int] = None
    street_address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    post_code: Optional[str] = None


class SupplierResponse(Response):
    id: Optional[int] = None
    supplier_code: str
    supplier_name: Optional[str] = None
    email_address: Optional[str] = None
    external_id: Optional[str] = None
    default_lead_time: Optional[int] = None


class CustomerRequest(Request):
    code: Optional[str] = None
    name: Optional[str] = None
    street_address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    post_code: Optional[str] = None
    email_address: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None


class CustomerResponse(Response):
    code: Optional[str] = None
    name: Optional[str] = None
    email_address: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None


class BomRequest(Request):
    """productCodeReadable, NOT productId (probe: /api/boms is keyed by productCodeReadable)."""

    product_code: str = Field(min_length=1, pattern=r"\S", alias="productId")
    component_code: str = Field(min_length=1, pattern=r"\S", alias="componentId")
    quantity: Optional[float] = None  # float on the wire: exact up to 15 significant digits; the planner rounds to 5 decimals and drops anything >= 1e14


class BomResponse(Response):
    """BOM calls are keyed by productCodeReadable, NOT productId (probed 2026-11-16). POST echoes that key in product_id/component_id;
    GET (both keys) puts StockTrim's numeric ids there and the products in sku_product/sku_component: use product_code/component_code
    (the readable key).
    Fallback order: the nested product's productCodeReadable, else its nested productId, else the raw id, which in a GET response
    without nested products is the NUMERIC id."""

    id: Optional[int] = None
    product_id: str
    component_id: str
    quantity: Optional[float] = None
    assembly_time_days: Optional[int] = None
    sku_product: Optional[ProductResponse] = None
    sku_component: Optional[ProductResponse] = None

    @property
    def product_code(self) -> str:
        return (self.sku_product.product_code_readable or self.sku_product.product_id) if self.sku_product else self.product_id

    @property
    def component_code(self) -> str:
        return (self.sku_component.product_code_readable or self.sku_component.product_id) if self.sku_component else self.component_id


class SalesOrderRequest(Request):
    product_id: str = Field(min_length=1, pattern=r"\S")
    order_date: AwareDatetime  # serialised as ISO 8601, e.g. 2026-11-16T00:00:00Z
    quantity: float
    external_reference_id: str = Field(min_length=1, pattern=r"\S")  # required by US: it is the upsert key; without it every POST (and retry) adds a row
    unit_price: Optional[float] = None
    location_code: Optional[str] = None
    location_name: Optional[str] = None
    customer_code: Optional[str] = None
    customer_name: Optional[str] = None


class SalesOrderResponse(Response):
    id: Optional[int] = None
    product_id: str
    external_reference_id: Optional[str] = None
    order_date: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None


class PurchaseOrderSupplier(Request):
    supplier_code: Optional[str] = None
    supplier_name: Optional[str] = None


class PurchaseOrderLocation(Request):
    location_code: Optional[str] = None
    location_name: Optional[str] = None


class PurchaseOrderLine(Request):
    product_id: str = Field(min_length=1, pattern=r"\S")
    quantity: float
    received_date: Optional[AwareDatetime] = None
    eta_date: Optional[AwareDatetime] = None
    unit_price: Optional[float] = None


class PurchaseOrderRequest(Request):
    supplier: PurchaseOrderSupplier
    purchase_order_line_items: list[PurchaseOrderLine]
    order_date: Optional[AwareDatetime] = None
    created_date: Optional[AwareDatetime] = None
    fully_received_date: Optional[AwareDatetime] = None
    external_id: Optional[str] = None
    reference_number: Optional[str] = None
    client_reference_number: str = Field(min_length=1, pattern=r"\S")  # required by US: it is the upsert key
    location: Optional[PurchaseOrderLocation] = None
    status: Optional[PurchaseOrderStatus] = None


class PurchaseOrderLineResponse(Response):
    product_id: str
    quantity: float
    received_date: Optional[str] = None
    eta_date: Optional[str] = None
    unit_price: Optional[float] = None


class PurchaseOrderSupplierResponse(Response):
    supplier_code: Optional[str] = None
    supplier_name: Optional[str] = None


class PurchaseOrderResponse(Response):
    id: Optional[int] = None
    message: Optional[str] = None
    reference_number: Optional[str] = None
    client_reference_number: Optional[str] = None
    external_id: Optional[str] = None
    order_date: Optional[str] = None
    created_date: Optional[str] = None
    fully_received_date: Optional[str] = None
    status: Optional[PurchaseOrderStatus] = None
    supplier: Optional[PurchaseOrderSupplierResponse] = None
    purchase_order_line_items: list[PurchaseOrderLineResponse] = []  # v1 GET answers null

    @field_validator("purchase_order_line_items", mode="before")
    @classmethod
    def _none_is_empty(cls, v: Any) -> Any:
        return [] if v is None else v
