"""Only QBWC_PATH is reachable through the tunnel/proxy; everything else is 404 unless a direct loopback call.

ngrok's agent connects from 127.0.0.1 (loopback alone proves nothing), but ngrok/any reverse proxy adds
X-Forwarded-*; uvicorn's proxy-headers handling may also rewrite the client to the real remote IP.
"""
import ipaddress

from starlette.responses import JSONResponse

QBWC_PATH = "/qbwc"
_PROXY_HEADERS = {b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto", b"forwarded"}


def _loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TunnelGate:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"] != QBWC_PATH:
            host = (scope.get("client") or ("",))[0]
            if not _loopback(host) or any(k in _PROXY_HEADERS for k, _ in scope["headers"]):
                if scope["type"] == "http":
                    return await JSONResponse({"detail": "Not Found"}, 404)(scope, receive, send)
                return await send({"type": "websocket.close"})
        await self.app(scope, receive, send)
