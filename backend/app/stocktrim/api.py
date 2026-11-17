"""One typed method per StockTrim endpoint we use. Behaviour notes come from the live probe: backend/docs/stocktrim-api-notes.md."""
from collections.abc import Callable, Iterable
from typing import Any, Optional, Union

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from .client import BulkItem, StockTrimClient
from .dto import (
    BomRequest,
    BomResponse,
    CustomerRequest,
    CustomerResponse,
    ProductRequest,
    ProductResponse,
    PurchaseOrderRequest,
    PurchaseOrderResponse,
    SalesOrderBulkRequest,
    SalesOrderRequest,
    SalesOrderResponse,
    SupplierRequest,
    SupplierResponse,
)
from .errors import StockTrimBadResponse, StockTrimRejected

_PRODUCTS, _SUPPLIERS = TypeAdapter(list[ProductResponse]), TypeAdapter(list[SupplierResponse])
_CUSTOMERS, _POS = TypeAdapter(list[CustomerResponse]), TypeAdapter(list[PurchaseOrderResponse])
_SALES_ORDERS = TypeAdapter(list[SalesOrderResponse])


def _parse(target: Union[TypeAdapter, type[BaseModel]], resp: httpx.Response, identifier: Optional[str] = None) -> Any:
    """Parse a 2xx body; an empty/non-JSON/off-schema body becomes StockTrimBadResponse. For JSON content types its message never
    contains body text (only status, content-type, length, validation locations/types); `from None` keeps pydantic's input out of tracebacks."""
    try:
        return (target.validate_json if isinstance(target, TypeAdapter) else target.model_validate_json)(resp.content)
    except ValidationError as e:  # also raised for invalid JSON
        summary = ", ".join(f"{'.'.join(map(str, x['loc'])) or '<root>'}:{x['type']}" for x in e.errors(include_input=False, include_url=False)[:5])
        raise StockTrimBadResponse(resp.status_code, resp.headers.get("content-type", ""), resp.text, identifier, summary) from None


class StockTrimApi:
    def __init__(self, client: StockTrimClient):
        self.c = client

    def _or_none(self, method: str, path: str, **kw: Any) -> Optional[httpx.Response]:
        """The API answers 404 for 'no match'; that is a normal empty result for reads (all other errors still raise)."""
        try:
            return self.c.request(method, path, **kw)
        except StockTrimRejected as e:
            if e.status == 404:
                return None
            raise

    def map_concurrent(self, fn: Callable[[Any], Any], items: Iterable[Any]) -> list[BulkItem]:
        """Rate-limited, concurrent bulk helper of the underlying client (see StockTrimClient.map_concurrent)."""
        return self.c.map_concurrent(fn, items)

    # products (V1). `code` filters on productCodeReadable (NOT productId); pages hold 50; 404 = no match
    def post_product(self, p: ProductRequest) -> ProductResponse:
        return _parse(ProductResponse, self.c.request("POST", "/api/Products", json=p.payload(), identifier=p.product_id), p.product_id)

    def get_products(self, code: Optional[str] = None, page_no: Optional[int] = None) -> list[ProductResponse]:
        """`code` matches productCodeReadable (not productId), case-insensitively. Trailing spaces are ignored on both the query and the
        stored value; a LEADING space or a trailing TAB is significant (404). So codes differing only by trailing spaces cannot be told
        apart here: callers must compare the returned code and detect that collision themselves (see docs/stocktrim-api-notes.md)."""
        r = self._or_none("GET", "/api/Products", params={"code": code, "pageNo": page_no}, identifier=code)
        return _parse(_PRODUCTS, r, code) if r else []

    def delete_product(self, product_id: str) -> None:
        self.c.request("DELETE", "/api/Products", params={"productId": product_id}, identifier=product_id)

    # suppliers: POST takes an array and returns an array; GET ?code= returns ONE object (404 if absent); GET /api/SuppliersBulk lists them all
    def post_suppliers(self, suppliers: list[SupplierRequest]) -> list[SupplierResponse]:
        ident = ",".join(s.supplier_code for s in suppliers)[:200]
        return _parse(_SUPPLIERS, self.c.request("POST", "/api/Suppliers", json=[s.payload() for s in suppliers], identifier=ident), ident)

    def get_supplier(self, code: str) -> Optional[SupplierResponse]:
        r = self._or_none("GET", "/api/Suppliers", params={"code": code}, identifier=code)
        return _parse(SupplierResponse, r, code) if r else None

    def list_suppliers_bulk(self) -> list[SupplierResponse]:
        """GET /api/SuppliersBulk: every supplier in one array. No paging (page/pageNo/pageSize/code are ignored); 404 is treated as empty."""
        r = self._or_none("GET", "/api/SuppliersBulk")
        return _parse(_SUPPLIERS, r) if r else []

    def delete_supplier(self, code_or_name: str) -> None:
        self.c.request("DELETE", "/api/Suppliers", params={"supplierCodeOrName": code_or_name}, identifier=code_or_name)

    # customers: PUT is a proven upsert by code (case-insensitive, FULL replace: omitted fields are cleared), so it is retried like POSTs. 200 + the customer as body (ignored)
    def put_customer(self, cust: CustomerRequest) -> None:
        self.c.request("PUT", "/api/Customers", json=cust.payload(), identifier=cust.code)

    def get_customers(self) -> list[CustomerResponse]:
        r = self._or_none("GET", "/api/Customers")
        return _parse(_CUSTOMERS, r) if r else []

    # BOM: keyed by productCodeReadable (NOT productId). GET only answers when BOTH keys are given; DELETE must always carry both
    def post_bom(self, b: BomRequest) -> BomResponse:
        ident = f"{b.product_code}>{b.component_code}"
        return _parse(BomResponse, self.c.request("POST", "/api/boms", json=b.payload(), identifier=ident), ident)

    def get_bom(self, *, product_code: str, component_code: str) -> Optional[BomResponse]:
        """Both arguments are productCodeReadable values (sent as the productId/componentId query parameters)."""
        _need_both(product_code, component_code)
        ident = f"{product_code}>{component_code}"
        r = self._or_none("GET", "/api/boms", params={"productId": product_code, "componentId": component_code}, identifier=ident)
        return _parse(BomResponse, r, ident) if r else None

    def delete_bom(self, *, product_code: str, component_code: str) -> None:
        _need_both(product_code, component_code)  # a one-code DELETE could remove many edges: refuse it here
        self.c.request("DELETE", "/api/boms", params={"productId": product_code, "componentId": component_code}, identifier=f"{product_code}>{component_code}")

    # sales orders: upsert by externalReferenceId (required by our DTO)
    def post_sales_order(self, so: SalesOrderRequest) -> SalesOrderResponse:
        r = self.c.request("POST", "/api/SalesOrders", json=so.payload(), identifier=so.external_reference_id)
        return _parse(SalesOrderResponse, r, so.external_reference_id)

    def post_sales_orders(self, lines: list[SalesOrderRequest]) -> None:
        """Upsert of lines that share one customer (see SalesOrderBulkRequest); the response body is not needed."""
        body = SalesOrderBulkRequest(order_date=lines[0].order_date, customer_code=lines[0].customer_code,
                                     sale_order_line_items=[ln.model_copy(update={"customer_code": None}) for ln in lines])
        self.c.request("POST", "/api/SalesOrdersBulk", json=body.payload(), identifier=f"{len(lines)} sales order lines")

    def get_sales_orders(self, product_id: str) -> list[SalesOrderResponse]:
        """Not listable without a product: one call per productId; 404 = none."""
        r = self._or_none("GET", "/api/SalesOrders", params={"productId": product_id}, identifier=product_id)
        return _parse(_SALES_ORDERS, r, product_id) if r else []

    # purchase orders: V2 list (status comes back as a name or 0-3, both parsed); POST upserts by clientReferenceNumber
    def list_purchase_orders(self, page: Optional[int] = None, page_size: Optional[int] = None, status: Optional[str] = None) -> list[PurchaseOrderResponse]:
        r = self.c.request("GET", "/api/V2/PurchaseOrders", params={"page": page, "pageSize": page_size, "status": status})
        return _parse(_POS, r)

    def post_purchase_order(self, po: PurchaseOrderRequest) -> PurchaseOrderResponse:
        r = self.c.request("POST", "/api/PurchaseOrders", json=po.payload(), identifier=po.client_reference_number)
        return _parse(PurchaseOrderResponse, r, po.client_reference_number)

    def run_forecast(self) -> None:
        self.c.request("POST", "/api/RunForecastCalculations", idempotent=False)  # semantics unknown: never re-sent after a lost response


def _need_both(product_code: str, component_code: str) -> None:
    if not (product_code and component_code):
        raise ValueError("BOM calls require both product_code and component_code")
