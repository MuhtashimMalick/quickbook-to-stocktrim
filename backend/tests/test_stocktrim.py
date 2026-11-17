import json
import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.stocktrim.api import StockTrimApi
from app.stocktrim.client import RateLimiter, StockTrimClient, _retry_after, raise_on_failures
from app.stocktrim.dto import (
    BomRequest,
    BomResponse,
    CustomerRequest,
    ProductRequest,
    ProductResponse,
    PurchaseOrderLine,
    PurchaseOrderRequest,
    PurchaseOrderResponse,
    PurchaseOrderSupplier,
    SalesOrderRequest,
)
from app.stocktrim.errors import (
    StockTrimAborted,
    StockTrimAuthError,
    StockTrimBadResponse,
    StockTrimBulkError,
    StockTrimConfigError,
    StockTrimRejected,
    StockTrimUnavailable,
)

ID, SIG = "test-id-123", "test-sig-SECRET-456"


def prod(pid, **kw):
    return ProductRequest(product_id=pid, product_code_readable=pid, stock_on_hand=0, **kw)


def make(handler: Callable[[httpx.Request], httpx.Response], **kw):
    sleeps: list[float] = []
    client = StockTrimClient("https://st.test", ID, SIG, transport=httpx.MockTransport(handler), sleep=sleeps.append,
                             limiter=RateLimiter(kw.get("max_rps", 5), clock=kw.get("clock", time.monotonic), sleep=sleeps.append), **kw)
    return client, StockTrimApi(client), sleeps


def test_headers_and_null_fields_omitted():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"productId": "A"})

    _, api, _ = make(h)
    api.post_product(prod("A", name=None, cost=1.5))
    req = seen[0]
    assert req.headers["api-auth-id"] == ID and req.headers["api-auth-signature"] == SIG
    assert json.loads(req.content) == {"productId": "A", "productCodeReadable": "A", "stockOnHand": 0.0, "cost": 1.5}  # camelCase, no nulls


def test_none_query_params_are_not_sent():
    seen = []
    _, api, _ = make(lambda r: seen.append(r) or httpx.Response(200, json=[]))
    api.get_products(code=None, page_no=2)
    assert dict(seen[0].url.params) == {"pageNo": "2"}


@pytest.mark.parametrize("status,expected", [
    # 503: no limiter deferral. Attempt 1 takes the first free slot (no wait); the retry sleeps Retry-After (7); attempt 2 waits 0.2 s for the
    # limiter slot (5 rps, frozen clock), sleeps 7 again; attempt 3 waits 0.4 s for its slot and succeeds.
    (503, [7, 0.2, 7, 0.4]),
    # 429: each 429 defers the shared limiter by Retry-After. Attempt 1 (no wait) -> 429 -> limiter next slot = t+7, retry sleeps 7;
    # attempt 2 waits 7 for the slot; 429 -> defer keeps t+7.2 -> sleeps 7; attempt 3 waits 7.2 for its slot and succeeds.
    (429, [7, 7, 7, 7.2]),
])
def test_retries_then_succeeds_honouring_retry_after(status, expected):
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(status, headers={"Retry-After": "7"}) if len(calls) < 3 else httpx.Response(200, json={"productId": "A"})

    _, api, sleeps = make(h, clock=lambda: 100.0)
    assert api.post_product(prod("A")).product_id == "A"
    assert len(calls) == 3 and sleeps == pytest.approx(expected)


def test_retries_transport_error():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[])

    _, api, _ = make(h)
    assert api.get_products() == [] and len(calls) == 2


def test_400_is_not_retried_and_carries_status_body_identifier():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(400, text='{"errors":{"ProductId":["required"]}}')

    _, api, _ = make(h)
    with pytest.raises(StockTrimRejected) as e:
        api.post_product(prod("SKU-1"))
    assert len(calls) == 1 and e.value.status == 400 and "required" in e.value.body and e.value.identifier == "SKU-1"


def test_unavailable_after_max_attempts_never_success():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503)

    _, api, _ = make(h)
    with pytest.raises(StockTrimUnavailable):
        api.post_product(prod("A"))
    assert len(calls) == 5


def test_404_on_reads_is_empty_but_other_404s_raise():
    _, api, _ = make(lambda r: httpx.Response(404, json={"title": "Not Found"}))
    assert api.get_products(code="x") == [] and api.get_supplier("x") is None
    with pytest.raises(StockTrimRejected):
        api.delete_product("x")  # a failed delete is never reported as success


def test_po_status_string_and_int_both_parse():
    for raw, want in [("Approved", "Approved"), (1, "Approved"), ("2", "Sent"), (0, "Draft"), (3, "Received"), ("received", "Received")]:
        assert PurchaseOrderResponse.model_validate({"status": raw, "purchaseOrderLineItems": []}).status == want
    with pytest.raises(ValidationError):
        PurchaseOrderResponse.model_validate({"status": 9})


def test_po_list_parses_mixed_status_shapes():
    body = [{"referenceNumber": "1", "status": "Sent", "purchaseOrderLineItems": [{"productId": "A", "quantity": 2.0}], "unknownField": 1},
            {"referenceNumber": "2", "status": 1, "purchaseOrderLineItems": []}]
    _, api, _ = make(lambda r: httpx.Response(200, json=body))
    assert [p.status for p in api.list_purchase_orders(page=1, page_size=10)] == ["Sent", "Approved"]


def test_po_request_omits_nulls_and_validates_status():
    po = PurchaseOrderRequest(supplier=PurchaseOrderSupplier(supplier_code="S"), purchase_order_line_items=[PurchaseOrderLine(product_id="A", quantity=1)],
                              client_reference_number="C1", status="Approved")
    assert po.payload() == {"supplier": {"supplierCode": "S"}, "purchaseOrderLineItems": [{"productId": "A", "quantity": 1.0}],
                            "clientReferenceNumber": "C1", "status": "Approved"}
    with pytest.raises(ValidationError):
        PurchaseOrderRequest(supplier=PurchaseOrderSupplier(), purchase_order_line_items=[], client_reference_number="C", status="Bogus")


def test_request_models_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        prod("A", bogus=1)


@pytest.mark.parametrize("pid,cid", [("", "C"), ("P", ""), ("", "")])
def test_bom_delete_and_get_refuse_missing_ids(pid, cid):
    calls = []
    _, api, _ = make(lambda r: calls.append(r) or httpx.Response(200))
    with pytest.raises(ValueError):
        api.delete_bom(product_code=pid, component_code=cid)
    with pytest.raises(ValueError):
        api.get_bom(product_code=pid, component_code=cid)
    with pytest.raises(TypeError):  # the ids are keyword-only and required
        api.delete_bom(product_code="P")  # type: ignore[call-arg]
    assert calls == []


def test_bom_get_and_delete_send_readable_codes_and_get_reads_nested_readable_codes():
    seen = []

    def h(req):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"productId": "13150143", "componentId": "13150144",
                                             "skuProduct": {"productId": "A", "productCodeReadable": "A-R"},
                                             "skuComponent": {"productId": "B", "productCodeReadable": "B-R"}})
        return httpx.Response(200)

    _, api, _ = make(h)
    api.delete_bom(product_code="A-R", component_code="B-R")
    assert dict(seen[0].url.params) == {"productId": "A-R", "componentId": "B-R"}
    b = api.get_bom(product_code="A-R", component_code="B-R")
    assert dict(seen[1].url.params) == {"productId": "A-R", "componentId": "B-R"}
    assert (b.product_code, b.component_code) == ("A-R", "B-R")
    assert BomRequest(product_code="T", component_code="S", quantity=0).payload()["quantity"] == 0.0  # 0 is kept, only None is dropped


def test_bom_post_echo_and_request_body_use_readable_codes():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"productId": "A-R", "componentId": "B-R", "skuProduct": None, "skuComponent": None})

    _, api, _ = make(h)
    b = api.post_bom(BomRequest(product_code="A-R", component_code="B-R", quantity=2))
    assert seen[0] == {"productId": "A-R", "componentId": "B-R", "quantity": 2.0}
    assert (b.product_code, b.component_code) == ("A-R", "B-R")
    assert BomRequest(productId="A-R", componentId="B-R").product_code == "A-R"  # wire names still accepted


def test_bom_code_falls_back_to_nested_product_id_when_readable_is_null():
    b = BomResponse.model_validate({"productId": "1", "componentId": "2", "skuProduct": {"productId": "A", "productCodeReadable": None},
                                    "skuComponent": {"productId": "B"}})
    assert (b.product_code, b.component_code) == ("A", "B")
    assert BomResponse.model_validate({"productId": "1", "componentId": "2"}).product_code == "1"  # numeric id: no nested product


def test_rate_limiter_spacing_with_fake_clock():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    rl = RateLimiter(5, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        rl.acquire()
    assert sleeps == pytest.approx([0.2, 0.2, 0.2])  # first call is free, then 5 per second
    now[0] += 10
    sleeps.clear()
    rl.acquire()
    assert sleeps == []


def test_concurrency_helper_returns_per_item_failures_and_hides_nothing():
    client, _, _ = make(lambda r: httpx.Response(200), concurrency=3)

    def fn(x):
        if x % 2:
            raise StockTrimRejected(400, "bad", str(x))
        return x * 10

    res = client.map_concurrent(fn, range(6))
    assert [r.item for r in res] == list(range(6))  # input order kept, batch not aborted
    assert [r.result for r in res if r.ok] == [0, 20, 40]
    assert all(isinstance(r.error, StockTrimRejected) for r in res if not r.ok) and len([r for r in res if not r.ok]) == 3
    with pytest.raises(StockTrimBulkError) as e:
        raise_on_failures(res)
    assert len(e.value.failures) == 3
    raise_on_failures([r for r in res if r.ok])  # all ok: no error


def test_bulk_over_real_calls_reports_rejected_items():
    def h(req):
        return httpx.Response(400, text="no") if json.loads(req.content)["productId"] == "BAD" else httpx.Response(200, json={"productId": "ok"})

    client, api, _ = make(h)
    res = client.map_concurrent(lambda i: api.post_product(prod(i)), ["A", "BAD", "B"])
    assert [r.ok for r in res] == [True, False, True] and res[1].error.status == 400


def _settings(**kw):
    base = dict(database_url="postgresql+psycopg://u:p@h/d", qbwc_username="u", qbwc_password="p", admin_token="t",
                qwc_owner_id="977e21e5-98f1-440d-ac64-461878a8262c", qwc_file_id="21dfbdc4-f9b7-44bc-aed1-605d1aa21cc0")
    return Settings(_env_file=None, **base, **kw)


def test_config_error_without_credentials_and_settings_optional():
    s = _settings()
    assert s.stocktrim_max_rps == 5 and s.stocktrim_concurrency == 4 and s.stocktrim_base_url == "https://api.stocktrim.com"
    with pytest.raises(StockTrimConfigError):
        StockTrimClient.from_settings(s)
    with pytest.raises(StockTrimConfigError):
        StockTrimClient.from_settings(_settings(stocktrim_auth_id="", stocktrim_auth_signature=""))  # compose passes empty strings
    c = StockTrimClient.from_settings(_settings(stocktrim_auth_id=ID, stocktrim_auth_signature=SIG, stocktrim_max_rps=2))
    assert c._limiter.interval == 0.5


def test_credentials_never_logged_or_in_settings_repr(caplog):
    caplog.set_level(logging.DEBUG)
    _, api, _ = make(lambda r: httpx.Response(503))
    with pytest.raises(StockTrimUnavailable):
        api.run_forecast()
    with pytest.raises(StockTrimUnavailable):
        api.post_product(prod("A", name="PRIVATE-NAME"))
    text = caplog.text
    assert "status=503" in text and "attempt=" in text
    assert ID not in text and SIG not in text and "PRIVATE-NAME" not in text
    s = _settings(stocktrim_auth_id=ID, stocktrim_auth_signature=SIG)
    assert SIG not in repr(s) and ID not in repr(s)


# --- review fixes -------------------------------------------------------------------------------------------------

def test_identity_and_required_fields_enforced_by_types():
    with pytest.raises(ValidationError):
        SalesOrderRequest(product_id="A", order_date="2026-11-16T00:00:00Z", quantity=1)  # no external_reference_id
    with pytest.raises(ValidationError):
        PurchaseOrderRequest(supplier=PurchaseOrderSupplier(supplier_code="S"), purchase_order_line_items=[])  # no client_reference_number
    with pytest.raises(ValidationError):
        ProductRequest(product_id="", product_code_readable="x", stock_on_hand=0)
    with pytest.raises(ValidationError):
        ProductRequest(product_id="A")  # readable code and stock on hand are required (omitting them clears them in StockTrim)
    with pytest.raises(ValidationError):
        BomRequest(product_code="", component_code="C")
    with pytest.raises(ValidationError):
        SalesOrderRequest(product_id="A", order_date="2026-11-16T00:00:00", quantity=1, external_reference_id="x")  # naive datetime
    with pytest.raises(ValidationError):
        SalesOrderRequest(product_id="A", order_date="2026-11-16T00:00:00Z", quantity=1, external_reference_id="  ")  # whitespace-only key


def test_aware_dates_serialise_as_iso():
    so = SalesOrderRequest(product_id="A", order_date=datetime(2026, 11, 16, tzinfo=timezone.utc), quantity=1, external_reference_id="x")
    assert so.payload()["orderDate"] == "2026-11-16T00:00:00Z"


def test_non_idempotent_read_timeout_sent_once_but_idempotent_retries():
    calls = []

    def h(req):
        calls.append(req.url.path)
        raise httpx.ReadTimeout("slow", request=req)

    _, api, _ = make(h)
    with pytest.raises(StockTrimUnavailable) as e:
        api.run_forecast()
    assert len(calls) == 1 and "AUTH_ID" not in str(e.value)  # a timeout is not the wrong-id symptom
    calls.clear()
    with pytest.raises(StockTrimUnavailable):
        api.put_customer(CustomerRequest(code="C"))
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(StockTrimUnavailable):
        api.post_product(prod("A"))  # proven upsert: full retry
    assert len(calls) == 5


@pytest.mark.parametrize("make_exc", [lambda r: httpx.ConnectError("x", request=r), lambda r: httpx.PoolTimeout("x", request=r)])
def test_non_idempotent_retries_when_request_never_reached_server_and_on_429(make_exc):
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            raise make_exc(req)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(calls) == 2 else httpx.Response(200)

    _, api, _ = make(h)
    api.run_forecast()
    assert len(calls) == 3


def test_non_idempotent_5xx_not_retried():
    calls = []
    _, api, _ = make(lambda r: calls.append(1) or httpx.Response(500))
    with pytest.raises(StockTrimUnavailable):
        api.run_forecast()
    assert len(calls) == 1


def test_delete_404_after_transient_failure_is_success_but_first_attempt_404_raises():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost", request=req)
        return httpx.Response(404, json={"title": "No product could be found"})

    _, api, _ = make(h)
    api.delete_product("A")  # no exception
    assert len(calls) == 2
    n = []
    _, api, _ = make(lambda r: n.append(1) or (httpx.Response(503) if len(n) == 1 else httpx.Response(404)))
    api.delete_product("A")  # 5xx first, then 404: also success
    _, api, _ = make(lambda r: httpx.Response(404))
    with pytest.raises(StockTrimRejected):
        api.delete_product("A")  # 404 on the first attempt is a real 404
    n.clear()
    _, api, _ = make(lambda r: n.append(1) or (httpx.Response(503) if len(n) == 1 else httpx.Response(404)))
    with pytest.raises(StockTrimRejected):
        api.post_product(prod("A"))  # only DELETE gets this treatment


def test_auth_errors_not_retried_and_are_rejections():
    for status in (401, 403):
        calls = []
        _, api, _ = make(lambda r: calls.append(1) or httpx.Response(status, text="Unauthorized"))
        with pytest.raises(StockTrimAuthError) as e:
            api.get_products()
        assert isinstance(e.value, StockTrimRejected) and e.value.status == status and len(calls) == 1


def test_bulk_aborts_on_503_and_marks_pending_items():
    calls, started = [], []
    client, api, _ = make(lambda r: calls.append(1) or httpx.Response(503), concurrency=3)

    def fn(i):
        started.append(i)
        return api.post_product(prod(str(i)))

    with pytest.raises(StockTrimUnavailable) as e:
        client.map_concurrent(fn, range(20))
    res = e.value.partial_results
    assert len(res) == 20 and len(started) <= 3 and len(calls) <= 5 * 3
    aborted = [r for r in res if isinstance(r.error, StockTrimAborted)]
    assert len(aborted) >= 17 and not any(r.ok for r in aborted)


def test_bulk_aborts_on_401():
    calls = []
    client, api, _ = make(lambda r: calls.append(1) or httpx.Response(401), concurrency=2)
    with pytest.raises(StockTrimAuthError) as e:
        client.map_concurrent(lambda i: api.post_product(prod(str(i))), range(10))
    assert len(calls) <= 2 and sum(isinstance(r.error, StockTrimAborted) for r in e.value.partial_results) >= 8


def test_bulk_stops_on_keyboard_interrupt():
    client, _, _ = make(lambda r: httpx.Response(200), concurrency=1)
    started = []

    def fn(i):
        started.append(i)
        if i == 1:
            raise KeyboardInterrupt  # BaseException: not caught by the per-item handler
        time.sleep(0.01)
        return i

    with pytest.raises(KeyboardInterrupt):
        client.map_concurrent(fn, range(50))
    assert len(started) < 50


@pytest.mark.parametrize("body,ctype", [(b"", None), (b"<html>gateway</html>", "text/html")])
def test_200_with_unparseable_body_is_stocktrim_bad_response(body, ctype):
    _, api, _ = make(lambda r: httpx.Response(200, content=body, headers={"content-type": ctype} if ctype else {}))
    for call in (lambda: api.post_product(prod("A")), lambda: api.get_products(), lambda: api.list_purchase_orders(), lambda: api.get_supplier("S")):
        with pytest.raises(StockTrimBadResponse) as e:
            call()
        assert e.value.status == 200 and e.value.body == body.decode()[:300]
    assert e.value.content_type == (ctype or "")


def test_response_models_tolerate_extra_supplier_fields_and_null_po_lines():
    p = ProductResponse.model_validate({"productId": "A", "suppliers": [{"supplierId": "S", "isDefault": True}]})
    assert p.suppliers[0].supplier_id == "S"
    assert PurchaseOrderResponse.model_validate({"purchaseOrderLineItems": None}).purchase_order_line_items == []


def test_per_call_time_cap_stops_retries():
    t, calls = [0.0], []

    def h(req):
        calls.append(1)
        t[0] += 20  # each attempt "takes" 20 s
        return httpx.Response(503)

    client = StockTrimClient("https://st.test", ID, SIG, transport=httpx.MockTransport(h), clock=lambda: t[0],
                             sleep=lambda s: t.__setitem__(0, t[0] + s), max_call_seconds=45,
                             limiter=RateLimiter(5, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s)))
    with pytest.raises(StockTrimUnavailable):
        StockTrimApi(client).post_product(prod("A"))
    assert len(calls) == 3  # stopped by the time cap, not by the 5-attempt limit


def test_retry_after_seconds_http_date_and_garbage():
    now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)

    def ra(v):
        return _retry_after(httpx.Response(429, headers={"Retry-After": v}), now)

    assert ra("7") == 7 and ra("999") == 60 and ra("-5") == 0
    assert ra("Mon, 16 Nov 2026 12:00:30 GMT") == 30
    assert ra("Mon, 16 Nov 2026 11:00:00 GMT") == 0 and ra("Mon, 16 Nov 2026 14:00:00 GMT") == 60
    assert ra("nan") == 0 and ra("inf") == 0 and ra("soon") == 0
    assert _retry_after(httpx.Response(429), now) == 0


def test_base_url_must_be_https_except_localhost():
    for bad in ("http://api.stocktrim.com", "ftp://x", "api.stocktrim.com"):
        with pytest.raises(ValidationError):
            _settings(stocktrim_base_url=bad)
    for ok in ("https://api.stocktrim.com", "http://localhost:9000", "http://127.0.0.1:9000"):
        assert _settings(stocktrim_base_url=ok).stocktrim_base_url == ok
    with pytest.raises(ValidationError):
        _settings(stocktrim_max_call_seconds=5)
    assert _settings().stocktrim_max_call_seconds == 90


def test_per_call_cap_counts_the_sleep_about_to_happen():
    t, calls = [0.0], []

    def h(req):
        calls.append(1)
        return httpx.Response(503, headers={"Retry-After": "60"})  # attempts take 0 s

    client = StockTrimClient("https://st.test", ID, SIG, transport=httpx.MockTransport(h), clock=lambda: t[0],
                             sleep=lambda s: t.__setitem__(0, t[0] + s), max_call_seconds=45,
                             limiter=RateLimiter(5, lambda: t[0], lambda s: t.__setitem__(0, t[0] + s)))
    with pytest.raises(StockTrimUnavailable):
        StockTrimApi(client).post_product(prod("A"))
    assert len(calls) == 1 and t[0] <= 45


def test_bad_json_response_never_leaks_body_text():
    _, api, _ = make(lambda r: httpx.Response(200, json={"productId": None, "name": "PRIVATE"}))
    with pytest.raises(StockTrimBadResponse) as e:
        api.post_product(prod("A"))
    txt = str(e.value) + "".join(traceback.format_exception(e.value))
    assert "PRIVATE" not in txt and "product_id" not in str(e.value) and "productId" in str(e.value) and "200" in str(e.value)
    assert "PRIVATE" in e.value.body  # kept as an attribute only


def test_delete_429_then_404_is_still_rejected():
    n = []
    _, api, _ = make(lambda r: n.append(1) or (httpx.Response(429) if len(n) == 1 else httpx.Response(404)))
    with pytest.raises(StockTrimRejected):
        api.delete_product("A")  # 429 means the first attempt was never processed


def test_hint_only_after_a_5xx_not_after_timeouts():
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    _, api, _ = make(boom)
    with pytest.raises(StockTrimUnavailable) as e:
        api.get_products()
    assert "STOCKTRIM_AUTH_ID" not in str(e.value)
    _, api, _ = make(lambda r: httpx.Response(500))
    with pytest.raises(StockTrimUnavailable) as e:
        api.get_products()
    assert "STOCKTRIM_AUTH_ID" in str(e.value)


def test_bulk_with_three_simultaneous_failures_aborts_the_rest():
    barrier, calls, started = threading.Barrier(3, timeout=10), [], []
    client, api, _ = make(lambda r: calls.append(1) or httpx.Response(503), concurrency=3)

    def fn(i):
        started.append(i)
        barrier.wait()  # force 3 calls in flight together
        return api.post_product(prod(str(i)))

    with pytest.raises(StockTrimUnavailable) as e:
        client.map_concurrent(fn, range(20))
    res = e.value.partial_results
    assert len(started) == 3 and len(calls) <= 15
    assert sum(isinstance(r.error, StockTrimAborted) for r in res) == 17
