"""Admin-UI auth: users, JWT, refresh rotation, lockout, rate limit, CSRF, CLI. Offline, synthetic data, injectable clock (no sleeps)."""
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import auth, cli
from app.config import Settings
from app.db import session_scope
from app.models import AppUser, RefreshToken

from .conftest import ADMIN_ROUTES, HEADERS, TOKEN, Env

PW = "correct horse battery"  # synthetic
A = "/api/v1/auth"
HTTPS = "https://testserver"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(auth, "_ph", ph)
    monkeypatch.setattr(auth, "_DUMMY", ph.hash("dummy"))


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth, "now", c)
    return c


@pytest.fixture
def c(env: Env, clock) -> TestClient:
    return TestClient(env.app, base_url=HTTPS, client=("127.0.0.1", 50000))  # https: the refresh cookie is Secure


@pytest.fixture
def open_gate(monkeypatch):
    """TunnelGate 404s non-loopback traffic until the next stage exposes /api/v1; open it here to test the dependency's own proxied-vs-direct check."""
    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, *a):
            await self.app(*a)
    monkeypatch.setattr("app.main.TunnelGate", Open)


def add_user(name="alice", pw=PW, **kw):
    with session_scope() as s:
        s.add(AppUser(username=name, password_hash=auth.hash_password(pw), **kw))


def login(c, name="alice", pw=PW, **kw):
    return c.post(f"{A}/login", json={"username": name, "password": pw}, **kw)


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def token_of(r):
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def refresh_with(c, raw=None, **kw):
    if raw:
        c.cookies.clear()
        kw["headers"] = {**kw.get("headers", {}), "Cookie": f"qb_refresh={raw}"}
    return c.post(f"{A}/refresh", **kw)


def user_row(name="alice"):
    with session_scope() as s:
        return s.scalar(select(AppUser).where(AppUser.username == name))


def live_tokens():
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.revoked_at.is_(None)))


# ---- login ---------------------------------------------------------------------------------------

def test_login_ok_and_cookie_attributes(c):
    add_user()
    r = login(c)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in"} and body["token_type"] == "bearer" and body["expires_in"] == 900
    cookie = r.headers["set-cookie"].lower()
    for attr in ("qb_refresh=", "httponly", "secure", "samesite=strict", "path=/api/v1/auth", "max-age=86400"):
        assert attr in cookie, attr
    assert user_row().last_login_at == auth.now()
    me = c.get(f"{A}/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and set(me.json()) == {"username", "last_login_at"} and me.json()["username"] == "alice"


def test_username_is_case_insensitive(c):
    add_user()
    assert login(c, " ALICE ").status_code == 200


def test_unknown_user_and_bad_password_look_identical_and_both_verify(c, monkeypatch):
    add_user()
    calls = []
    real = auth._verify
    monkeypatch.setattr(auth, "_verify", lambda h, p: calls.append(h) or real(h, p))
    bad, unknown = login(c, pw="wrong password xx"), login(c, "nobody", PW)
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json() == {"detail": "invalid_credentials"}
    assert len(calls) == 2 and calls[1] == auth._DUMMY  # unknown user paid one argon2 verify too
    assert "set-cookie" not in unknown.headers


def test_disabled_user_cannot_login_and_looks_generic(c):
    add_user(is_active=False)
    r = login(c)
    assert r.status_code == 401 and r.json() == {"detail": "invalid_credentials"}


def test_login_requires_json_and_forbids_extra_fields(c):
    add_user()
    assert c.post(f"{A}/login", data={"username": "alice", "password": PW}).status_code == 422
    assert c.post(f"{A}/login", json={"username": "alice", "password": PW, "x": 1}).status_code == 422


def test_lockout_after_5_failures_unlocks_after_window_and_resets_on_success(c, clock, caplog):
    add_user()
    for _ in range(4):
        assert login(c, pw="wrong password xx").json() == {"detail": "invalid_credentials"}
    assert login(c).status_code == 200  # a success resets the counter
    assert user_row().failed_logins == 0
    clock.advance(seconds=61)  # stay under the per-IP rate limit
    for _ in range(5):
        assert login(c, pw="wrong password xx").status_code == 401
    assert "account locked user_id=" in caplog.text and "ip=127.0.0.1" in caplog.text
    r = login(c)  # correct password, but locked: indistinguishable from a wrong password
    assert r.status_code == 401 and r.json() == {"detail": "invalid_credentials"} and "set-cookie" not in r.headers
    clock.advance(minutes=14)
    assert login(c).status_code == 401
    clock.advance(minutes=2)
    assert login(c).status_code == 200
    u = user_row()
    assert u.failed_logins == 0 and u.locked_until is None


def test_login_and_refresh_rate_limited_per_ip(c, clock):
    add_user()
    for _ in range(10):
        assert login(c, "nobody", PW).status_code == 401
    r = login(c, "nobody", PW)
    assert r.status_code == 429 and r.json() == {"detail": "rate_limited"}
    assert refresh_with(c, "abc").status_code == 429  # shared bucket
    clock.advance(seconds=61)
    assert login(c).status_code == 200


def test_rate_limit_keys_on_the_proxy_client_ip(open_gate, make_env, clock):
    c = TestClient(make_env().app, base_url=HTTPS, client=("127.0.0.1", 1))
    for _ in range(10):
        assert login(c, "nobody", PW, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 401
    assert login(c, "nobody", PW, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    assert login(c, "nobody", PW, headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 401
    assert login(c, "nobody", PW, headers={"X-Forwarded-For": "10.0.0.1, 198.51.100.7"}).status_code == 429  # last entry = the one Caddy set


@pytest.mark.parametrize("pw,ok", [("short", False), ("a" * 13, False), ("tr0ub4dor&3-horse", True), ("alice-alice-alice", True),
                                   ("passwordpassword", False), ("PasswordPassword", False), ("Password1234567", False)])
def test_password_policy(pw, ok):
    if ok:
        auth.check_password("alice", pw)
    else:
        with pytest.raises(ValueError):
            auth.check_password("alice", pw)


def test_password_policy_username_and_common_messages():
    with pytest.raises(ValueError, match="username"):
        auth.check_password("long.user.name.x", "LONG.user.name.x")
    with pytest.raises(ValueError, match="common"):
        auth.check_password("alice", "passwordpassword")
    with pytest.raises(ValueError, match="14"):
        auth.check_password("alice", "x" * 13)


# ---- JWT -----------------------------------------------------------------------------------------

def secret(env):
    return env.settings.jwt_secret.get_secret_value()


def claims(**kw):
    t = int(datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc).timestamp())
    return {"sub": "1", "username": "alice", "iat": t, "exp": t + 900, "jti": "j", **kw}


def test_access_token_claims_and_me_needs_a_bearer(c, env):
    add_user()
    tok = token_of(login(c))
    p = jwt.decode(tok, secret(env), algorithms=["HS256"], options={"verify_exp": False, "verify_iat": False})
    assert set(p) == {"sub", "username", "iat", "exp", "jti"} and p["username"] == "alice" and p["exp"] - p["iat"] == 900
    assert jwt.get_unverified_header(tok)["alg"] == "HS256"
    assert c.get(f"{A}/me").status_code == 401
    assert c.get(f"{A}/me", headers=HEADERS).status_code == 401  # /me needs a user: X-Admin-Token is not enough


def test_expired_token_vs_invalid_token(c, clock):
    add_user()
    tok = token_of(login(c))
    assert c.get(f"{A}/me", headers=bearer(tok)).status_code == 200
    clock.advance(minutes=16)
    r = c.get(f"{A}/me", headers=bearer(tok))
    assert r.status_code == 401 and r.json() == {"detail": "token_expired"}
    assert c.get("/api/v1/jobs", headers=bearer(tok)).json() == {"detail": "token_expired"}


def test_bad_tokens_rejected(c, env):
    add_user()
    uid = user_row().id
    good = token_of(login(c))
    head, _, sig = good.split(".")
    forged_payload = jwt.encode(claims(sub=str(uid + 1)), secret(env), algorithm="HS256").split(".")[1]
    no_jti = {k: v for k, v in claims(sub=str(uid)).items() if k != "jti"}
    bad = {
        "wrong key": jwt.encode(claims(sub=str(uid)), "k" * 40, algorithm="HS256"),
        "tampered claims": f"{head}.{forged_payload}.{sig}",
        "alg none": jwt.encode(claims(sub=str(uid)), None, algorithm="none"),
        "alg HS512": jwt.encode(claims(sub=str(uid)), secret(env), algorithm="HS512"),
        "missing jti": jwt.encode(no_jti, secret(env), algorithm="HS256"),
        "iat in the future": jwt.encode(claims(sub=str(uid), iat=claims()["iat"] + 3600), secret(env), algorithm="HS256"),
        "non-numeric sub": jwt.encode(claims(sub="alice"), secret(env), algorithm="HS256"),
        "unknown user": jwt.encode(claims(sub="99999"), secret(env), algorithm="HS256"),
        "garbage": "not.a.jwt",
    }
    for why, tok in bad.items():
        r = c.get(f"{A}/me", headers=bearer(tok))
        assert (r.status_code, r.json()) == (401, {"detail": "invalid_token"}), why
    assert c.get(f"{A}/me", headers=bearer(good)).status_code == 200


def test_disabled_user_token_rejected_on_every_request(c):
    add_user()
    tok = token_of(login(c))
    assert c.get("/api/v1/jobs", headers=bearer(tok)).status_code == 200
    with session_scope() as s:
        s.execute(text("UPDATE app_user SET is_active = false"))
    assert c.get("/api/v1/jobs", headers=bearer(tok)).json() == {"detail": "invalid_token"}
    assert c.get(f"{A}/me", headers=bearer(tok)).status_code == 401


# ---- refresh / logout ----------------------------------------------------------------------------

def test_refresh_rotates_and_stores_only_hashes(c):
    add_user()
    login(c)
    old = c.cookies.get("qb_refresh")
    r = c.post(f"{A}/refresh")
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    new = c.cookies.get("qb_refresh")
    assert new and new != old
    cookie = r.headers["set-cookie"].lower()
    assert "path=/api/v1/auth" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    with session_scope() as s:
        rows = s.scalars(select(RefreshToken).order_by(RefreshToken.id)).all()
    assert len(rows) == 2 and rows[0].revoked_at is not None and rows[1].revoked_at is None
    assert {old, new}.isdisjoint(x.token_sha256 for x in rows) and len(rows[0].token_sha256) == 64
    assert c.get(f"{A}/me", headers=bearer(r.json()["access_token"])).status_code == 200


def test_refresh_reuse_revokes_all_tokens_of_the_user(c, clock):
    add_user()
    add_user("bob")
    login(c, "bob")
    bob_cookie = c.cookies.get("qb_refresh")
    login(c)
    login(c)  # a second session for alice
    first = c.cookies.get("qb_refresh")
    assert c.post(f"{A}/refresh").status_code == 200  # rotates `first`
    clock.advance(seconds=30)  # past the benign-race grace
    r = refresh_with(c, first)  # replay of the rotated token
    assert r.status_code == 401 and r.json() == {"detail": "invalid_refresh"}
    assert "max-age=0" in r.headers["set-cookie"].lower()  # the dead cookie is cleared
    with session_scope() as s:
        live = s.scalars(select(AppUser.username).join(RefreshToken, RefreshToken.user_id == AppUser.id).where(RefreshToken.revoked_at.is_(None))).all()
    assert live == ["bob"]  # all of alice's tokens are gone, bob untouched
    assert refresh_with(c, bob_cookie).status_code == 200


def test_concurrent_refresh_with_the_same_cookie_is_benign(c, clock):
    add_user()
    login(c)
    old = c.cookies.get("qb_refresh")
    assert c.post(f"{A}/refresh").status_code == 200
    successor = c.cookies.get("qb_refresh")
    clock.advance(seconds=9)
    r = refresh_with(c, old)  # the second request of a race: rotated < 10 s ago
    assert r.status_code == 401 and r.json() == {"detail": "invalid_refresh"}
    assert "set-cookie" not in r.headers  # nothing cleared: the browser already holds the successor
    assert live_tokens() == 1  # the successor was not revoked
    assert refresh_with(c, successor).status_code == 200  # and it still works


def test_replay_after_the_grace_window_revokes_everything(c, clock):
    add_user()
    login(c)
    old = c.cookies.get("qb_refresh")
    assert c.post(f"{A}/refresh").status_code == 200
    successor = c.cookies.get("qb_refresh")
    clock.advance(seconds=11)
    assert refresh_with(c, old).status_code == 401
    assert live_tokens() == 0
    assert refresh_with(c, successor).status_code == 401


def test_logout_then_immediate_refresh_is_not_a_benign_race(c):
    add_user()
    login(c)
    other = c.cookies.get("qb_refresh")
    login(c)
    raw = c.cookies.get("qb_refresh")
    assert c.post(f"{A}/logout").status_code == 204
    assert live_tokens() == 1  # the other session is still alive
    assert refresh_with(c, raw).status_code == 401  # replay of a logout-revoked token (no rotated_at): reuse
    assert live_tokens() == 0
    assert refresh_with(c, other).status_code == 401


def test_refresh_missing_unknown_and_expired(c, clock):
    add_user()
    assert c.post(f"{A}/refresh").json() == {"detail": "invalid_refresh"}
    assert refresh_with(c, "nope").status_code == 401
    login(c)
    clock.advance(hours=25)
    assert c.post(f"{A}/refresh").status_code == 401


def test_refresh_fails_for_disabled_user(c):
    add_user()
    login(c)
    with session_scope() as s:
        s.execute(text("UPDATE app_user SET is_active = false"))
    assert c.post(f"{A}/refresh").status_code == 401


def test_logout_revokes_and_clears_cookie(c):
    add_user()
    login(c)
    raw = c.cookies.get("qb_refresh")
    r = c.post(f"{A}/logout")
    assert r.status_code == 204 and "max-age=0" in r.headers["set-cookie"].lower()
    assert live_tokens() == 0
    assert refresh_with(c, raw).status_code == 401
    assert c.post(f"{A}/logout").status_code == 204  # idempotent, no cookie


@pytest.mark.parametrize("path", ["refresh", "logout"])
@pytest.mark.parametrize("headers,status", [
    ({}, "ok"), ({"Origin": "https://testserver"}, "ok"), ({"Sec-Fetch-Site": "same-origin"}, "ok"), ({"Sec-Fetch-Site": "none"}, "ok"),
    ({"Origin": "https://evil.example"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"}, 403), ({"Origin": "null"}, 403)])
def test_csrf_checks_on_cookie_routes(c, path, headers, status):
    add_user()
    login(c)
    r = c.post(f"{A}/{path}", headers=headers)
    if status == 403:
        assert r.status_code == 403 and r.json() == {"detail": "cross_site_request"}
        assert live_tokens() == 1  # nothing was rotated or revoked
    else:
        assert r.status_code in (200, 204)


# ---- every admin route ----------------------------------------------------------------------------

PUBLIC = {("post", f"{A}/login"), ("post", f"{A}/refresh"), ("post", f"{A}/logout")}


def test_public_routes_are_exactly_login_refresh_logout(env):
    paths = env.client.get("/openapi.json").json()["paths"]
    assert {(m, p) for p, ops in paths.items() if p != "/qbwc" for m, op in ops.items() if "security" not in op} == PUBLIC


def test_openapi_documents_both_auth_schemes(env):
    ops = env.client.get("/openapi.json").json()["paths"]
    assert ops["/api/v1/jobs"]["get"]["security"] == [{"APIKeyHeader": []}, {"HTTPBearer": []}]  # alternatives
    assert ops[f"{A}/me"]["get"]["security"] == [{"HTTPBearer": []}]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_route_auth_matrix(c, method, path):
    add_user()
    call = getattr(c, method)
    assert call(path).status_code == 401
    assert call(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert call(path, headers=bearer("garbage")).status_code == 401
    assert call(path, headers=bearer(token_of(login(c)))).status_code != 401
    assert call(path, headers=HEADERS).status_code != 401  # loopback + X-Admin-Token = the CLI path


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_token_ignored_on_proxied_requests_but_bearer_works(open_gate, make_env, clock, method, path):
    env = make_env()
    add_user()
    proxied = TestClient(env.app, base_url=HTTPS, client=("127.0.0.1", 1))
    remote = TestClient(env.app, base_url=HTTPS, client=("203.0.113.9", 1))
    xff = {"X-Forwarded-For": "203.0.113.9"}
    call = getattr(proxied, method)
    assert call(path, headers={**HEADERS, **xff}).status_code == 401
    assert call(path, headers={**HEADERS, "Forwarded": "for=203.0.113.9"}).status_code == 401
    assert getattr(remote, method)(path, headers=HEADERS).status_code == 401
    assert getattr(proxied, method)(path, headers=HEADERS).status_code != 401  # direct loopback still works
    tok = token_of(login(proxied, headers=xff))
    assert call(path, headers={**bearer(tok), **xff}).status_code != 401


def test_gate_still_hides_api_from_proxied_requests(env):
    xff = {"X-Forwarded-For": "203.0.113.9"}
    assert env.client.get("/api/v1/auth/me", headers=xff).status_code == 404
    assert env.client.post("/api/v1/auth/login", json={}, headers=xff).status_code == 404
    assert env.client.get("/docs", headers=xff).status_code == 404


# ---- leakage -------------------------------------------------------------------------------------

def test_no_secrets_in_logs_or_responses(c, caplog, env, clock):
    caplog.set_level(logging.DEBUG)
    sentinel_pw, sentinel_user, wrong = "Sentinel-PW-ZQX-98765", "sentinel-user-zqx", "Sentinel-wrong-ZQX-1"
    add_user("alice", sentinel_pw)
    hashed = user_row().password_hash
    bad = [login(c, sentinel_user, sentinel_pw), login(c, "alice", wrong)]
    ok = login(c, "alice", sentinel_pw)
    access, raw = token_of(ok), c.cookies.get("qb_refresh")
    c.get(f"{A}/me", headers=bearer(access))
    c.post(f"{A}/refresh")
    clock.advance(seconds=11)  # past the benign-race grace
    refresh_with(c, raw)  # reuse -> warning path
    assert "reuse detected" in caplog.text
    c.post(f"{A}/logout")
    out = caplog.text + "".join(r.text for r in bad)
    for s in (sentinel_pw, wrong, sentinel_user, hashed, "$argon2", access, raw, TOKEN, secret(env)):
        assert s not in out, s[:6]
    with session_scope() as s:
        stored = " ".join(s.scalars(select(RefreshToken.token_sha256)).all())
    assert raw not in stored and sentinel_pw not in stored


# ---- config --------------------------------------------------------------------------------------

def test_jwt_secret_required_and_min_32(monkeypatch):
    monkeypatch.delenv("JWT_SECRET")
    base = dict(_env_file=None, qbwc_username="u", qbwc_password="p", admin_token="t", qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), database_url="postgresql+psycopg://u:p@h/d")
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(**base)
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(**base, jwt_secret="x" * 31)
    assert Settings(**base, jwt_secret="x" * 32).access_token_minutes == 15


# ---- CLI -----------------------------------------------------------------------------------------

@pytest.fixture
def users_cli(clean_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", clean_db)
    answers = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": answers.pop(0))

    def run(*argv, pw=()):
        answers[:] = list(pw)
        cli.main(["users", *argv])
        return capsys.readouterr()
    return run


def test_cli_users_lifecycle(users_cli, env, clock):
    pw = "Sentinel-CLI-pw-4242"
    out = users_cli("add", "Bob", pw=[pw, pw])
    assert "ok" in out.out and pw not in out.out + out.err
    c = TestClient(env.app, base_url=HTTPS, client=("127.0.0.1", 1))
    assert login(c, "bob", pw).status_code == 200  # the CLI-created user can log in
    out = users_cli("list")
    assert "bob" in out.out and "active=True" in out.out and "last_login=2026-11-19" in out.out and "argon2" not in out.out and pw not in out.out
    new = "Another-sentinel-pw-777"
    users_cli("passwd", "bob", pw=[new, new])
    assert live_tokens() == 0  # passwd revoked the session from above
    assert login(c, "bob", pw).status_code == 401 and login(c, "bob", new).status_code == 200
    tok = token_of(login(c, "bob", new))
    users_cli("disable", "bob")
    assert "active=False" in users_cli("list").out
    assert login(c, "bob", new).status_code == 401 and c.get(f"{A}/me", headers=bearer(tok)).status_code == 401
    assert live_tokens() == 0
    users_cli("enable", "bob")
    assert login(c, "bob", new).status_code == 200


@pytest.mark.parametrize("argv,pw,msg", [
    (("add", "bob"), ["short", "short"], "14"),
    (("add", "bob"), ["a-long-enough-password", "different-long-password"], "differ"),
    (("add", "b!"), [], "username"),
    (("add", "bob"), ["passwordpassword", "passwordpassword"], "common"),
    (("passwd", "ghost"), [], "no such user"),
    (("disable", "ghost"), [], "no such user"),
])
def test_cli_users_errors(users_cli, argv, pw, msg):
    with pytest.raises(SystemExit) as e:
        users_cli(*argv, pw=pw)
    assert msg in str(e.value.code)


def test_cli_users_duplicate_and_argument_shape(users_cli):
    users_cli("add", "bob", pw=["a-long-enough-password"] * 2)
    with pytest.raises(SystemExit, match="exists"):
        users_cli("add", "BOB", pw=["a-long-enough-password"] * 2)
    for argv in (("add",), ("list", "bob"), ("nuke", "bob")):
        with pytest.raises(SystemExit) as e:
            users_cli(*argv)
        assert e.value.code == 2


def test_cli_users_needs_no_jwt_secret(users_cli, monkeypatch):
    monkeypatch.delenv("JWT_SECRET")
    assert users_cli("list").out == ""


# ---- session lifetime, argon2 slots, replay binding, lockout window, limiter, hardening -------------

def test_refresh_does_not_slide_the_session(c, clock):
    add_user()
    assert "max-age=86400" in login(c).headers["set-cookie"].lower()
    clock.advance(hours=12)
    assert "max-age=43200" in c.post(f"{A}/refresh").headers["set-cookie"].lower()  # remaining lifetime, not a fresh 24 h
    clock.advance(hours=11)  # 23 h after login
    r = c.post(f"{A}/refresh")
    assert r.status_code == 200 and "max-age=3600" in r.headers["set-cookie"].lower()
    clock.advance(hours=2)  # 25 h after login: the chain did not extend
    assert c.post(f"{A}/refresh").status_code == 401


def test_login_429_when_argon2_slots_are_exhausted(c, monkeypatch):
    add_user()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(auth, "_VERIFY_SLOTS", slots)
    monkeypatch.setattr(auth, "VERIFY_WAIT", 0.01)
    slots.acquire()
    r = login(c)
    assert r.status_code == 429 and r.json() == {"detail": "rate_limited"} and "set-cookie" not in r.headers
    slots.release()
    assert login(c).status_code == 200


def test_concurrent_bad_logins_count_exactly(c):
    add_user()

    def bad(_):
        try:
            auth.login("alice", "wrong password xx", 24, "198.51.100.1", "ua")
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(4) as ex:
        assert list(ex.map(bad, range(4))) == [401] * 4
    assert user_row().failed_logins == 4


@pytest.mark.parametrize("other,benign", [({}, True), ({"headers": {"User-Agent": "other-agent"}}, False), ({"peer": "127.0.0.2"}, False)])
def test_benign_replay_needs_same_ip_and_user_agent(c, clock, caplog, other, benign):
    caplog.set_level(logging.INFO)
    add_user()
    login(c)
    old = c.cookies.get("qb_refresh")
    assert c.post(f"{A}/refresh").status_code == 200
    client = TestClient(c.app, base_url=HTTPS, client=(other.get("peer", "127.0.0.1"), 1)) if "peer" in other else c
    clock.advance(seconds=2)
    r = refresh_with(client, old, **{k: v for k, v in other.items() if k == "headers"})
    assert r.status_code == 401
    assert ("benign refresh replay user_id=" in caplog.text) == benign
    assert live_tokens() == (1 if benign else 0)


def test_failures_must_fall_within_15_minutes_to_lock(c, clock):
    add_user()
    for _ in range(4):
        assert login(c, pw="wrong password xx").status_code == 401
    clock.advance(minutes=16)
    for _ in range(4):  # the counter restarted: 4 more failures do not lock
        assert login(c, pw="wrong password xx").status_code == 401
    assert login(c).status_code == 200


def test_disabled_and_locked_look_like_any_other_failure(c, clock):
    add_user(is_active=False, locked_until=clock() + timedelta(minutes=5))
    r = login(c)
    assert (r.status_code, r.json()) == (401, {"detail": "invalid_credentials"}) and "set-cookie" not in r.headers


def test_ipv6_is_limited_per_slash_64(clock):
    for i in range(10):
        auth.rate_limit(f"2001:db8:1:2::{i + 1:x}")
    with pytest.raises(HTTPException) as e:
        auth.rate_limit("2001:db8:1:2:ffff::1")
    assert e.value.status_code == 429
    auth.rate_limit("2001:db8:1:3::1")  # the neighbouring /64 is another bucket


def test_flood_of_distinct_ips_does_not_reset_an_existing_bucket(clock):
    for _ in range(10):
        auth.rate_limit("203.0.113.1")
    for i in range(10_000):
        auth.rate_limit(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    with pytest.raises(HTTPException):
        auth.rate_limit("203.0.113.1")
    clock.advance(seconds=61)
    auth.rate_limit("203.0.113.2")
    assert len(auth._hits) == 1  # idle keys are dropped once their window is over


def test_forged_loopback_xff_from_a_remote_peer_is_not_direct(env, open_gate_app):
    xff = {"X-Forwarded-For": "127.0.0.1", **HEADERS}
    behind = TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), client=("203.0.113.9", 1))  # uvicorn rewrites the peer to 127.0.0.1
    assert behind.get("/api/v1/jobs", headers=xff).status_code == 404  # the real gate
    assert TestClient(ProxyHeadersMiddleware(open_gate_app.app, trusted_hosts="*"), client=("203.0.113.9", 1)).get("/api/v1/jobs", headers=xff).status_code == 401


@pytest.fixture
def open_gate_app(open_gate, make_env):
    return make_env()


def test_every_api_route_rejects_unauthenticated_and_proxied_admin_token(open_gate, make_env):
    env = make_env()
    public = {f"{A}/login", f"{A}/refresh", f"{A}/logout"}
    spec = env.client.get("/openapi.json").json()["paths"]  # the route table (FastAPI nests included routers, so read the generated spec)
    routes = [(m, re.sub(r"\{[^}]+\}", "1", p)) for p, ops in spec.items() if p.startswith("/api/v1/") and p not in public for m in ops]
    paths = {p for _, p in routes}
    assert {"/api/v1/push", "/api/v1/preflight", "/api/v1/push/runs/1", "/api/v1/push/runs/1/cancel", "/api/v1/auth/me"} <= paths
    proxied = TestClient(env.app, base_url=HTTPS, client=("127.0.0.1", 1))
    for method, path in routes:
        call = getattr(proxied, method)
        assert call(path).status_code == 401, (method, path)
        assert call(path, headers=bearer("garbage")).status_code == 401, (method, path)
        assert call(path, headers={**HEADERS, "X-Forwarded-For": "203.0.113.9"}).status_code == 401, (method, path)


def test_login_is_csrf_checked_and_origin_must_be_https(c):
    add_user()
    assert login(c, headers={"Origin": "https://evil.example"}).status_code == 403
    assert login(c, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert login(c, headers={"Origin": "http://testserver"}).status_code == 403  # right host, wrong scheme (request is https)
    assert login(c, headers={"Origin": "https://testserver"}).status_code == 200


def test_old_access_tokens_die_on_passwd_and_disable(users_cli, c, clock):
    pw, new = "Sentinel-CLI-pw-4242", "Another-sentinel-pw-777"
    users_cli("add", "bob", pw=[pw, pw])
    tok = token_of(login(c, "bob", pw))
    assert c.get(f"{A}/me", headers=bearer(tok)).status_code == 200
    clock.advance(seconds=5)
    users_cli("passwd", "bob", pw=[new, new])
    assert c.get(f"{A}/me", headers=bearer(tok)).status_code == 401
    clock.advance(seconds=5)
    tok2 = token_of(login(c, "bob", new))
    assert c.get(f"{A}/me", headers=bearer(tok2)).status_code == 200
    clock.advance(seconds=5)
    users_cli("disable", "bob")
    users_cli("enable", "bob")
    assert c.get(f"{A}/me", headers=bearer(tok2)).status_code == 401  # re-enabling does not resurrect it


def test_cli_list_shows_locked_and_enable_clears_the_lock(users_cli, clock):
    users_cli("add", "bob", pw=["a-long-enough-password"] * 2)
    with session_scope() as s:
        s.execute(text("UPDATE app_user SET locked_until = :t, failed_logins = 3"), {"t": clock() + timedelta(minutes=10)})
    assert "locked=yes" in users_cli("list").out
    users_cli("enable", "bob")
    assert "locked=no" in users_cli("list").out and user_row("bob").failed_logins == 0


def test_expired_refresh_rows_are_pruned_on_login(c, clock):
    add_user()
    login(c)
    clock.advance(days=3)
    login(c)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(RefreshToken)) == 1


def test_secrets_must_differ_and_admin_token_not_the_placeholder():
    base = dict(_env_file=None, qbwc_username="u", qbwc_password="p" * 40, admin_token="t" * 40, qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(), database_url="postgresql+psycopg://u:p@h/d")
    for bad in (dict(jwt_secret="t" * 40), dict(jwt_secret="p" * 40), dict(jwt_secret="j" * 40, admin_token="change-me-admin")):
        with pytest.raises(ValidationError):
            Settings(**{**base, **bad})
    Settings(**base, jwt_secret="j" * 40)
