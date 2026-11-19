"""Admin API call from inside the container (the only place TunnelGate accepts it): python -m app.cli GET /jobs
PUT/POST with a body: echo '{"quantity": 5}' | python -m app.cli POST /jobs/create_test_po --json
Admin-UI users (direct DB access, no HTTP; the password is prompted, never an argument): python -m app.cli users add|passwd|disable|enable <username> / users list
OpenAPI of the proxy-reachable routes: python -m app.cli openapi"""
import argparse
import getpass
import os
import sys
import urllib.error
import urllib.request


def users(argv):
    from sqlalchemy import select  # noqa: PLC0415 - the HTTP commands need neither the DB nor JWT_SECRET

    from . import auth  # noqa: PLC0415
    from .config import DatabaseSettings  # noqa: PLC0415
    from .db import init_engine, session_scope  # noqa: PLC0415
    from .models import AppUser  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="python -m app.cli users")
    ap.add_argument("action", choices=["add", "passwd", "disable", "enable", "list"])
    ap.add_argument("username", nargs="?")
    a = ap.parse_args(argv)
    if (a.action == "list") == bool(a.username):
        ap.error("list takes no username; every other action needs one")
    init_engine(DatabaseSettings().database_url)
    name = auth.norm_username(a.username or "")
    with session_scope() as s:
        if a.action == "list":
            for u in s.scalars(select(AppUser).order_by(AppUser.username)):
                locked = "yes" if u.locked_until and u.locked_until > auth.now() else "no"
                print(f"{u.username}	active={u.is_active}	locked={locked}	last_login={u.last_login_at.isoformat() if u.last_login_at else '-'}")
            return
        user = s.scalar(select(AppUser).where(AppUser.username == name))
        if a.action == "add":
            if user or not auth.USERNAME_RE.match(name):
                sys.exit("user exists" if user else "username must be 3-40 chars of a-z 0-9 . _ -")
        elif user is None:
            sys.exit("no such user")
        if a.action in ("add", "passwd"):
            pw = getpass.getpass("Password: ")
            if pw != getpass.getpass("Repeat: "):
                sys.exit("passwords differ")
            try:
                auth.check_password(name, pw)
            except ValueError as e:
                sys.exit(str(e))
            if a.action == "add":
                s.add(AppUser(username=name, password_hash=auth.hash_password(pw)))
            else:
                user.password_hash, user.failed_logins, user.locked_until, user.tokens_valid_after = auth.hash_password(pw), 0, None, auth.now()
                auth.revoke_all_refresh(s, user.id)
        else:
            user.is_active = a.action == "enable"
            if a.action == "disable":
                user.tokens_valid_after = auth.now()
                auth.revoke_all_refresh(s, user.id)
            else:
                user.failed_logins, user.locked_until, user.last_failed_at = 0, None, None
    print(f"{a.action} {name}: ok")


def prune(argv):
    from .config import Settings  # noqa: PLC0415
    from .db import init_engine  # noqa: PLC0415
    from .runlog import prune as run_prune  # noqa: PLC0415

    argparse.ArgumentParser(prog="python -m app.cli prune", description="Apply the run-log retention now (HISTORY_RETENTION_DAYS).").parse_args(argv)
    settings = Settings()
    init_engine(settings.database_url)
    print(run_prune(settings))


def openapi(argv):
    import json  # noqa: PLC0415

    from fastapi import FastAPI  # noqa: PLC0415

    from .api.v1.auth import router as auth_router  # noqa: PLC0415
    from .api.v1.router import public  # noqa: PLC0415

    argparse.ArgumentParser(prog="python -m app.cli openapi", description="Print the OpenAPI JSON of the proxy-reachable routes only (no settings or database needed).").parse_args(argv)
    app = FastAPI(title="QuickBook admin API", redirect_slashes=False)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(public, prefix="/api/v1")
    print(json.dumps(app.openapi(), indent=1, sort_keys=True))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["openapi"]:
        return openapi(argv[1:])
    if argv[:1] == ["prune"]:
        return prune(argv[1:])
    if argv[:1] == ["users"]:
        return users(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Call /api/v1<path> with the ADMIN_TOKEN from the environment.")
    ap.add_argument("method", choices=["GET", "POST", "PUT"])
    ap.add_argument("path", help="starts with /, e.g. /jobs")
    ap.add_argument("--json", action="store_true", help="send stdin as a JSON body")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for the response (default 600; /preflight with supplier checks takes minutes)")
    a = ap.parse_args(argv)
    if not a.path.startswith("/"):
        ap.error("path must start with /")
    headers = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
    if a.json:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/v1{a.path}", method=a.method, headers=headers, data=sys.stdin.buffer.read() if a.json else None)
    try:
        sys.stdout.buffer.write(urllib.request.urlopen(req, timeout=a.timeout).read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")


if __name__ == "__main__":
    main()
