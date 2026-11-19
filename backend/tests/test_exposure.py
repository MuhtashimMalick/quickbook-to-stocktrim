"""Public exposure (stage B2): the REAL gate behind a simulated proxy (XFF + ProxyHeadersMiddleware, like Caddy + uvicorn)."""
import asyncio
import re
import threading
import time

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from qbwc_kit.testing import FakeWebConnector
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import auth
from app.api.v1.auth import COOKIE, COOKIE_PATH
from app.qbwc.gate import TunnelGate, is_cli_only

from .conftest import HEADERS, PASSWORD, QB, USER, make_records
from .test_auth import add_user, bearer, fast_hash, login, token_of  # noqa: F401 (fast_hash: autouse fixture)

XFF = {"X-Forwarded-For": "203.0.113.9"}
NOT_FOUND = b'{"detail":"Not Found"}'
# FROZEN: the only API routes reachable through the proxy. A new route is CLI-only (direct_only) unless added to `public` AND to this set.
PROXY_REACHABLE = {
    ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/refresh"), ("POST", "/api/v1/auth/logout"), ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/health"), ("POST", "/api/v1/scheduler/run-now"), ("POST", "/api/v1/preflight"),
    ("GET", "/api/v1/jobs"), ("GET", "/api/v1/jobs/{job_id}"), ("GET", "/api/v1/push/runs/{run_id}"),
    # stage B4: read APIs of the admin UI (GET only)
    ("GET", "/api/v1/cycles"), ("GET", "/api/v1/cycles/{cycle_id}"), ("GET", "/api/v1/events"), ("GET", "/api/v1/push/runs"), ("GET", "/api/v1/push/runs/{run_id}/entries"),
    ("GET", "/api/v1/push/runs/{run_id}/entries.csv"), ("GET", "/api/v1/settings"), ("GET", "/api/v1/reason-codes"), ("GET", "/api/v1/overview"),
    # stage B5: the stored sync schedule
    ("GET", "/api/v1/schedule"), ("PUT", "/api/v1/schedule"),
}
CLI_ONLY = {
    ("GET", "/api/v1/qwc"), ("GET", "/api/v1/records/{entity}"), ("GET", "/api/v1/log"), ("POST", "/api/v1/jobs/all-reads"), ("POST", "/api/v1/jobs/{job_type}"),
    ("POST", "/api/v1/push"), ("POST", "/api/v1/push/runs/{run_id}/cancel"), ("POST", "/api/v1/push/runs/{run_id}/resume"),
}
AUTH_PUBLIC = {("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/refresh"), ("POST", "/api/v1/auth/logout")}


@pytest.fixture
def proxied(env):
    """Through Caddy: uvicorn rewrites the peer to the real client IP, Caddy adds X-Forwarded-For."""
    return TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), base_url="https://testserver", client=("203.0.113.9", 1), headers=XFF, follow_redirects=False)


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _api_routes(app):
    """EVERY route under /api/v1 (included routers flattened); anything that is not a plain APIRoute (Mount, WebSocketRoute) fails."""
    out = []
    for rc in iter_route_contexts(app.routes):
        if (rc.path or "").startswith("/api/v1"):
            assert isinstance(rc.original_route, APIRoute), rc.path
            out.append(rc)
    return out


def _assert_hidden(r, what):
    assert (r.status_code, r.content) == (404, NOT_FOUND), (what, r.status_code, r.content[:80])


def test_frozen_set_of_proxy_reachable_routes(env, proxied):
    """Every method of every route, probed through the proxy with a valid Bearer; a JSON array fails body validation (422) only after the dependencies ran, so preflight is not executed for real."""
    add_user()
    tok = {**bearer(token_of(login(proxied))), "Content-Type": "application/json"}
    reach, hidden, routes = set(), set(), _api_routes(env.app)
    for rc in routes:
        for m in rc.methods:
            r = proxied.request(m, _fill(rc.path), headers=tok, content=b"[]" if m == "POST" else None)
            (hidden if (r.status_code, r.content) == (404, NOT_FOUND) else reach).add((m, rc.path))
            assert is_cli_only(rc) == ((m, rc.path) in CLI_ONLY), (m, rc.path)  # structural: not frozen as reachable => direct_only is a dependency
    assert reach == PROXY_REACHABLE
    assert hidden == CLI_ONLY
    assert {(m, rc.path) for rc in routes for m in rc.methods} == PROXY_REACHABLE | CLI_ONLY


def test_cli_only_routes_404_proxied_even_with_bearer_and_work_direct(env, proxied):
    add_user()
    tok = bearer(token_of(login(proxied)))
    for method, path in CLI_ONLY:
        for h in ({}, tok, HEADERS):
            _assert_hidden(proxied.request(method, _fill(path), headers=h), (method, path))
    _assert_hidden(proxied.post("/api/v1/jobs/create_test_po", headers=tok), "create_test_po")
    assert env.get("/api/v1/qwc").status_code == 200 and env.get("/api/v1/records/customers").status_code == 200  # the CLI is unchanged
    assert env.client.post("/api/v1/jobs/customers", headers=HEADERS).status_code == 200


def test_cli_only_existence_is_hidden_on_405_422_and_trailing_slash(env, proxied):
    add_user()
    tok = bearer(token_of(login(proxied)))
    for h in ({}, tok, HEADERS):
        _assert_hidden(proxied.post("/api/v1/qwc", headers=h), "405")  # wrong method
        _assert_hidden(proxied.get("/api/v1/push", headers=h), "405 push")
        _assert_hidden(proxied.post("/api/v1/push", content=b"{", headers={**h, "Content-Type": "application/json"}), "422 malformed")
        _assert_hidden(proxied.post("/api/v1/push", content=b'{"steps": 5}', headers={**h, "Content-Type": "application/json"}), "422 invalid")
        _assert_hidden(proxied.get("/api/v1/qwc/", headers=h), "slash")  # redirect_slashes is off: no 307
        _assert_hidden(proxied.get("/api/v1/jobs/", headers=h), "slash jobs")
    # direct (the CLI) keeps the real answers
    assert env.client.post("/api/v1/qwc", headers=HEADERS).status_code == 405
    assert env.client.post("/api/v1/push", content=b"{", headers={**HEADERS, "Content-Type": "application/json"}).status_code == 422
    # a public route keeps its real 405/422 for the authenticated UI
    assert proxied.post("/api/v1/health", headers=tok).status_code == 405
    assert proxied.post("/api/v1/auth/login", content=b"{", headers={"Content-Type": "application/json"}).status_code == 422


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/health", "/", "/api/v1", "/api/v1x", "/api/v1/q%77c", "/api/v1/qwc/", "/api/v1//qwc",
                                  "/api/v1/records/customers", "/api/v1/jobs/create_test_po", "/api/v1/%2e%2e/openapi.json", "/QBWC", "/qbwc/x", "/api/v2/health"])
def test_blocked_paths_404_with_and_without_bearer(env, proxied, path):
    add_user()
    for h in ({}, bearer(token_of(login(proxied))), HEADERS):
        _assert_hidden(proxied.request("POST" if path.endswith("create_test_po") else "GET", path, headers=h), path)


def test_websockets_are_closed_through_the_proxy(env, proxied):
    for path in ("/api/v1/x", "/docs", "/api/v1/auth/me"):
        with pytest.raises(WebSocketDisconnect):
            with proxied.websocket_connect(path):
                pass


def test_gate_decides_on_the_decoded_path_without_normalising():
    reached, status = [], []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def send(msg):
        if msg["type"] == "http.response.start":
            status.append(msg["status"])

    for path, ok in [("/api/v1/../openapi.json", True), ("/qbwc/../docs", False), ("//api/v1/health", False), ("/api/v1", False), ("/qbwc", True), ("/api/v1/health", True)]:
        reached.clear(), status.clear()
        scope = {"type": "http", "path": path, "raw_path": b"/qbwc", "headers": [(b"x-forwarded-for", b"203.0.113.9")], "client": ("127.0.0.1", 1)}
        asyncio.run(TunnelGate(app)(scope, None, send))
        assert (reached == [path]) is ok and (status == [404]) is (not ok), path  # raw_path is never consulted
    # a dot-segment path passes the prefix check only; no route matches it, so the router answers 404 (see the parametrised test above for the real app)


def test_auth_and_other_admin_routes_through_the_proxy(env, proxied):
    add_user()
    bad = proxied.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong-password-zzz"})
    assert (bad.status_code, bad.json()) == (401, {"detail": "invalid_credentials"})
    tok = bearer(token_of(login(proxied)))
    assert proxied.get("/api/v1/auth/me", headers=tok).json()["username"] == "alice"
    assert proxied.get("/api/v1/health", headers=tok).status_code == 200
    assert proxied.get("/api/v1/jobs", headers=tok).status_code == 200
    for m, p in PROXY_REACHABLE - AUTH_PUBLIC:
        for h in ({}, HEADERS, bearer("garbage")):  # X-Admin-Token is ignored once proxied
            assert proxied.request(m, _fill(p), headers=h).status_code == 401, (m, p)


def test_refresh_cookie_is_never_sent_to_qbwc(env, proxied):
    add_user()
    set_cookie = login(proxied).headers["set-cookie"]
    assert f"Path={COOKIE_PATH}" in set_cookie and "HttpOnly" in set_cookie and "Secure" in set_cookie and "SameSite=strict" in set_cookie
    assert "domain=" not in set_cookie.lower()  # host-only
    for url, expect in (("https://testserver/qbwc", False), ("https://testserver/qbwc?wsdl", False), ("https://testserver/api/v1/auth/refresh", True), ("https://testserver/api/v1/health", False)):
        req = proxied.build_request("GET", url)
        assert (f"{COOKIE}=" in req.headers.get("cookie", "")) is expect, url


def test_qbwc_wsdl_and_soap_session_through_the_proxy(env, proxied):
    assert "definitions" in proxied.get("/qbwc?wsdl").text
    env.queue("customers")
    c = FakeWebConnector(lambda body: proxied.post("/qbwc", content=body, headers={"Content-Type": "text/xml"}).text, USER, PASSWORD, qbxml_version=(13, 0))
    assert c.run_update(QB(entities={"Customer": make_records("Customer", 2)})).authenticated
    assert len(env.records("customers")) == 2


def test_login_returns_429_at_once_when_every_argon2_slot_is_busy(env, proxied):
    add_user()
    held = [auth._VERIFY_SLOTS.acquire() for _ in range(4)]
    try:
        t = time.monotonic()
        r = proxied.post("/api/v1/auth/login", json={"username": "alice", "password": "whatever-password-1"})
        assert r.status_code == 429 and r.json() == {"detail": "rate_limited"} and time.monotonic() - t < 0.5
    finally:
        for _ in held:
            auth._VERIFY_SLOTS.release()
    assert login(proxied).status_code == 200


def test_global_login_bucket_blocks_new_ips(env):
    add_user()
    for i in range(auth.GLOBAL_PER_MINUTE):
        c = TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), base_url="https://testserver", client=("198.51.100.1", 1), headers={"X-Forwarded-For": f"10.{i // 250}.{i % 250}.7"})
        assert c.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password-zzz"}).status_code == 401
    fresh = TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), base_url="https://testserver", client=("198.51.100.1", 1), headers={"X-Forwarded-For": "192.0.2.77"})
    r = fresh.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password-zzz"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert fresh.post("/api/v1/auth/refresh").status_code == 429


def test_health_probe_is_single_flight(env, proxied, monkeypatch):
    from app.api.v1 import ops
    add_user()
    tok = bearer(token_of(login(proxied)))
    gate, entered = threading.Event(), threading.Event()

    def slow(*a, **k):
        entered.set()
        gate.wait(5)
        raise ops.StockTrimConfigError("x")
    monkeypatch.setattr(ops, "preflight_client", slow)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=proxied.get("/api/v1/health?probe=true", headers=tok)))
    t.start()
    assert entered.wait(5)
    second = proxied.get("/api/v1/health?probe=true", headers=tok).json()["stocktrim"]
    gate.set()
    t.join(5)
    assert second == {"reachable": False, "error": "busy"}
    assert first["r"].json()["stocktrim"] == {"reachable": False, "error": "not_configured"}
