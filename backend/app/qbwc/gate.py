"""Proxied requests reach only QBWC_PATH and API_PREFIX* (decoded scope path; nothing is normalised); everything else is 404 unless a direct loopback call.
Within API_PREFIX a route is proxy-reachable only if it is registered on the `public` router (api/v1/router.py); the rest carry `direct_only`.

ngrok's agent connects from 127.0.0.1 (loopback alone proves nothing), but ngrok/any reverse proxy adds
X-Forwarded-*; uvicorn's proxy-headers handling may also rewrite the client to the real remote IP.
"""
import ipaddress

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.responses import JSONResponse

QBWC_PATH = "/qbwc"
API_PREFIX = "/api/v1/"
_PROXY_HEADERS = {b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto", b"forwarded"}


def _loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_direct(scope) -> bool:
    """A direct loopback call (the CLI inside the container): loopback client and no proxy headers."""
    host = (scope.get("client") or ("",))[0]
    return _loopback(host) and not any(k in _PROXY_HEADERS for k, _ in scope["headers"])


def direct_only(request: Request) -> None:
    """Route-level guard (runs on the matched route, so encoded/trailing-slash variants cannot bypass it): CLI-only routes are 404 when proxied."""
    if not is_direct(request.scope):
        raise HTTPException(404, "Not Found")


def is_cli_only(route) -> bool:
    return any(d.call is direct_only for d in route.dependant.dependencies)


def hide_cli_only(app: FastAPI) -> None:
    """A proxied 405 (wrong method) or 422 (bad body; FastAPI decodes it before the dependencies run) on a CLI-only route must look like a 404 too.
    Call after every router is included (the scope's route is the original APIRoute; its dependencies live on the effective route context)."""
    cli = {id(rc.original_route) for rc in iter_route_contexts(app.routes) if isinstance(rc.original_route, APIRoute) and is_cli_only(rc)}

    def hidden(request: Request) -> bool:
        return id(request.scope.get("route")) in cli and not is_direct(request.scope)

    async def on_405(request: Request, exc):
        return JSONResponse({"detail": "Not Found"}, 404) if hidden(request) else await http_exception_handler(request, exc)

    async def on_422(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Not Found"}, 404) if hidden(request) else await request_validation_exception_handler(request, exc)

    app.add_exception_handler(405, on_405)
    app.add_exception_handler(RequestValidationError, on_422)


class TunnelGate:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"] != QBWC_PATH and not scope["path"].startswith(API_PREFIX):
            if not is_direct(scope):
                if scope["type"] == "http":
                    return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
                return await send({"type": "websocket.close"})
        await self.app(scope, receive, send)
