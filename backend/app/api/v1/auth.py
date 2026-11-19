"""Admin-UI auth routes (login/refresh/logout are public; /me needs a Bearer token) and the `require_admin` dependency."""
import secrets
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from ... import auth
from ...models import AppUser
from ...qbwc.gate import is_direct

COOKIE, COOKIE_PATH = "qb_refresh", "/api/v1/auth"
_token_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def current_user(request: Request, cred: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> AppUser:
    if cred is None:
        raise HTTPException(401, "invalid_token")
    return auth.user_from_access_token(cred.credentials, request.app.state.settings.jwt_secret.get_secret_value())


def require_admin(request: Request, token: Optional[str] = Security(_token_header), cred: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> None:
    """Bearer access token, or X-Admin-Token on a direct loopback call only (the CLI); on a proxied request X-Admin-Token is ignored."""
    if cred is not None:
        current_user(request, cred)
        return
    expected = request.app.state.settings.admin_token.get_secret_value()
    if token and is_direct(request.scope) and secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return
    raise HTTPException(401, "invalid_token")


def client_ip(request: Request) -> str:
    """Behind Caddy the real client is the last X-Forwarded-For entry (Caddy sets it); a direct call uses the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[-1].strip() if xff else (request.client.host if request.client else "")


def same_origin(request: Request) -> None:
    """CSRF guard for the cookie routes: browsers send Sec-Fetch-Site/Origin; non-browser clients send neither."""
    site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
    o = urlparse(origin) if origin is not None else None
    if (site is not None and site not in ("same-origin", "none")) or (o and (o.netloc != request.headers.get("host") or o.scheme not in ("https", request.url.scheme))):
        raise HTTPException(403, "cross_site_request")


def rate_limited(request: Request) -> None:
    auth.rate_limit(client_ip(request))


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(_M):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenOut(_M):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class MeOut(_M):
    username: str
    last_login_at: Optional[datetime]


router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(request: Request, response: Response, user: AppUser, refresh: str, expires_at: datetime) -> TokenOut:
    st = request.app.state.settings
    max_age = max(0, int((expires_at - auth.now()).total_seconds()))  # remaining session time: a refresh never extends it
    response.set_cookie(COOKIE, refresh, max_age=max_age, httponly=True, secure=True, samesite="strict", path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    return TokenOut(access_token=auth.make_access_token(user, st.jwt_secret.get_secret_value(), st.access_token_minutes), expires_in=st.access_token_minutes * 60)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(same_origin), Depends(rate_limited)])
def login(body: LoginIn, request: Request, response: Response) -> TokenOut:
    user, raw, expires = auth.login(body.username, body.password, request.app.state.settings.refresh_token_hours, client_ip(request), request.headers.get("user-agent", ""))
    return _token_response(request, response, user, raw, expires)


@router.post("/refresh", response_model=TokenOut, dependencies=[Depends(same_origin), Depends(rate_limited)])
def refresh(request: Request, response: Response) -> Any:
    st = request.app.state.settings
    try:
        user, new, expires = auth.rotate_refresh(request.cookies.get(COOKIE), st.refresh_reuse_grace_seconds, client_ip(request), request.headers.get("user-agent", ""))
    except auth.BenignReplay:
        raise
    except HTTPException as e:  # an exception drops `response`'s headers, so build the 401 here to clear the dead cookie
        out = JSONResponse({"detail": e.detail}, e.status_code)
        out.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=True, samesite="strict")
        return out
    return _token_response(request, response, user, new, expires)


@router.post("/logout", status_code=204, dependencies=[Depends(same_origin)])
def logout(request: Request) -> Response:
    auth.revoke_refresh(request.cookies.get(COOKIE))
    out = Response(status_code=204)
    out.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=True, samesite="strict")
    return out


@router.get("/me", response_model=MeOut)
def me(user: AppUser = Depends(current_user)) -> MeOut:
    return MeOut(username=user.username, last_login_at=user.last_login_at)
