"""Admin-UI auth: argon2id passwords, HS256 access JWTs, rotating hashed refresh tokens, login lockout, per-IP rate limit.

Nothing here logs or returns passwords, hashes or tokens. Errors are stable `detail` codes (HTTPException).
`now` is the only clock (tests replace it); the limiter state is in memory (one uvicorn worker).
"""
import hashlib
import ipaddress
import logging
import re
import secrets
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException
from sqlalchemy import delete, select, update

from .db import session_scope
from .models import AppUser, RefreshToken, utcnow

log = logging.getLogger(__name__)
now = utcnow
ALG = "HS256"
MAX_FAILS, LOCK_MINUTES = 5, 15
RATE_PER_MINUTE = 10
MAX_RATE_KEYS = 50_000
VERIFY_WAIT = 0.1  # a busy argon2 slot answers 429 almost at once: a login flood must not hold the shared worker threads (/qbwc needs them)
GLOBAL_PER_MINUTE = 60  # all IPs together (login + refresh)
_VERIFY_SLOTS = threading.BoundedSemaphore(4)  # argon2 uses ~64 MB each: cap concurrent verifies
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")
COMMON_PASSWORDS = frozenset("""password123456 passwordpassword password1234567 passwordpassword1 p@ssw0rdp@ssw0rd p@ssword123456 12345678901234
123456789012345 1234567890123456 12345678910111213 qwertyuiopasdf qwertyuiop1234 qwertyuiopasdfgh qwerty123456789 qwertyqwertyqwerty 1qaz2wsx3edc4rfv
1q2w3e4r5t6y7u8i qazwsxedcrfvtgb zaq12wsxcde3rfv4 abcdefghijklmn abcdefghijklmnop abcd1234abcd1234 letmeinletmein letmein12345678 welcomewelcome
welcome1234567 changemechangeme changeit1234567 administrator123 adminadminadmin iloveyouiloveyou iloveyou123456 trustno1trustno1
monkeymonkeymonkey footballfootball baseballbaseball sunshinesunshine superman123456 starwarsstarwars 00000000000000 0000000000000000 11111111111111
1111111111111111 aaaaaaaaaaaaaa correcthorsebatterystaple thequickbrownfox thequickbrownfoxjumps quickbooks123456 stocktrim123456 specialtyarchery
specialtyarchery1""".split())

class BenignReplay(HTTPException):
    """Same 401 invalid_refresh, but the route must not clear the cookie (it already holds the successor)."""


_ph = PasswordHasher()
_DUMMY = _ph.hash("dummy-password-for-timing")  # unknown users cost one verify too


def norm_username(u: str) -> str:
    return u.strip().lower()


def check_password(username: str, password: str) -> None:
    """Policy for new passwords; raises ValueError with the reason."""
    if len(password) < 14:
        raise ValueError("password must be at least 14 characters")
    if password.lower() == username.lower():
        raise ValueError("password must not equal the username")
    if password.lower() in COMMON_PASSWORDS:
        raise ValueError("password is too common")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def _verify(hashed: str, password: str) -> bool:
    if not _VERIFY_SLOTS.acquire(timeout=VERIFY_WAIT):
        raise HTTPException(429, "rate_limited", headers={"Retry-After": "5"})
    try:
        return _ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False
    finally:
        _VERIFY_SLOTS.release()


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---- per-IP rate limit (login + refresh) ----------------------------------------------------

_hits: "OrderedDict[str, deque]" = OrderedDict()  # least recently used first
_hits_lock = threading.Lock()
_global: deque = deque()


def _ip_key(ip: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)  # one IPv6 subscriber = a /64


def rate_limit(ip: str) -> None:
    t, key = now().timestamp(), _ip_key(ip)
    with _hits_lock:
        while _global and _global[0] <= t - 60:
            _global.popleft()
        if len(_global) >= GLOBAL_PER_MINUTE:
            raise HTTPException(429, "rate_limited", headers={"Retry-After": "60"})
        while _hits and next(iter(_hits.values()))[-1] <= t - 60:  # drop keys whose last hit is out of the window
            _hits.popitem(last=False)
        q = _hits.setdefault(key, deque())
        _hits.move_to_end(key)
        while q and q[0] <= t - 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            raise HTTPException(429, "rate_limited", headers={"Retry-After": "60"})
        q.append(t)
        _global.append(t)
        while len(_hits) > MAX_RATE_KEYS:  # flood of distinct IPs: evict the least recently used, never reset everyone
            _hits.popitem(last=False)


# ---- access tokens ---------------------------------------------------------------------------

def make_access_token(user: AppUser, secret: str, minutes: int) -> str:
    t = now()
    claims = {"sub": str(user.id), "username": user.username, "iat": int(t.timestamp()),
              "exp": int((t + timedelta(minutes=minutes)).timestamp()), "jti": secrets.token_hex(8)}
    return jwt.encode(claims, secret, algorithm=ALG)


def user_from_access_token(token: str, secret: str) -> AppUser:
    try:  # algorithm pinned (no 'none'); exp/iat are checked below against `now`
        p = jwt.decode(token, secret, algorithms=[ALG], options={"require": ["exp", "iat", "sub", "jti"], "verify_exp": False, "verify_iat": False})
        t = now().timestamp()
        if not (isinstance(p["exp"], int) and isinstance(p["iat"], int) and p["sub"].isdigit()):
            raise HTTPException(401, "invalid_token")
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid_token") from None
    if p["iat"] > t + 30:
        raise HTTPException(401, "invalid_token")
    if p["exp"] <= t:
        raise HTTPException(401, "token_expired")
    with session_scope() as s:
        user = s.get(AppUser, int(p["sub"]))
    if user is None or not user.is_active or (user.tokens_valid_after and p["iat"] < user.tokens_valid_after.timestamp()):
        raise HTTPException(401, "invalid_token")  # disabled, unknown, or issued before the last passwd/disable
    return user


# ---- login / refresh / logout ----------------------------------------------------------------

def _issue_refresh(s, user_id: int, expires_at: datetime, ip: str, ua: str) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(32)
    row = RefreshToken(user_id=user_id, token_sha256=_sha(raw), created_at=now(), expires_at=expires_at, ip=ip[:45], user_agent=ua[:200])
    s.add(row)
    s.flush()
    return raw, row


def login(username: str, password: str, hours: int, ip: str, ua: str) -> tuple[AppUser, str, datetime]:
    """Returns (user, refresh cookie value, its expiry) or raises 401 invalid_credentials (also while locked). The argon2 verify runs outside any
    transaction; the counters are then updated under a short row lock."""
    uname = norm_username(username)
    with session_scope() as s:
        found = s.scalar(select(AppUser).where(AppUser.username == uname))
        hashed = found.password_hash if found else _DUMMY
    ok = _verify(hashed, password)  # always one verify: same timing for unknown users
    out = None
    with session_scope() as s:
        user = s.scalar(select(AppUser).where(AppUser.username == uname).with_for_update()) if found else None
        t = now()
        if user and user.locked_until and user.locked_until > t:
            log.info("login rejected (locked) user_id=%s ip=%s", user.id, ip)
        elif not (user and user.is_active and ok):
            if user and user.is_active:
                if user.last_failed_at is None or t - user.last_failed_at > timedelta(minutes=LOCK_MINUTES):
                    user.failed_logins = 0  # 5 failures WITHIN 15 minutes
                user.failed_logins, user.last_failed_at = user.failed_logins + 1, t
                if user.failed_logins >= MAX_FAILS:
                    user.locked_until, user.failed_logins = t + timedelta(minutes=LOCK_MINUTES), 0
                    log.warning("account locked user_id=%s ip=%s", user.id, ip)
            log.info("login failed username_len=%d ip=%s", len(uname), ip)
        else:
            user.failed_logins, user.locked_until, user.last_failed_at, user.last_login_at = 0, None, None, t
            old = select(RefreshToken.id).where(RefreshToken.expires_at < t - timedelta(days=1)).limit(100)  # opportunistic, bounded prune
            s.execute(delete(RefreshToken).where(RefreshToken.id.in_(old)))
            expires = t + timedelta(hours=hours)
            raw, _ = _issue_refresh(s, user.id, expires, ip, ua)
            out = (user, raw, expires)
            log.info("login ok user_id=%s ip=%s", user.id, ip)
    if out is None:
        raise HTTPException(401, "invalid_credentials")
    return out


def rotate_refresh(raw: Optional[str], grace: int, ip: str, ua: str) -> tuple[AppUser, str, datetime]:
    """Revoke the presented token and issue a successor that INHERITS its expiry (no sliding session). Replaying a revoked token revokes ALL of the
    user's tokens, except a benign race: rotated < grace ago and replayed from the same ip + user agent as the rotation."""
    out, benign = None, False
    if raw:
        with session_scope() as s:
            row = s.scalar(select(RefreshToken).where(RefreshToken.token_sha256 == _sha(raw)).with_for_update())
            t = now()
            if row is not None:
                user = s.get(AppUser, row.user_id)
                if row.revoked_at is not None:
                    succ = s.get(RefreshToken, row.replaced_by_id) if row.replaced_by_id else None
                    within = row.rotated_at is not None and t - row.rotated_at < timedelta(seconds=grace)
                    if within and succ is not None and (succ.ip, succ.user_agent) == (ip[:45], ua[:200]):
                        benign = True  # concurrent refresh with the cookie the first response already replaced: the successor stays valid
                        log.info("benign refresh replay user_id=%s", row.user_id)
                    else:
                        revoke_all_refresh(s, row.user_id)
                        log.warning("refresh token reuse detected: all tokens revoked user_id=%s ip=%s", row.user_id, ip)
                elif row.expires_at > t and user.is_active:
                    new_raw, new = _issue_refresh(s, user.id, row.expires_at, ip, ua)
                    row.revoked_at = row.rotated_at = t
                    row.replaced_by_id = new.id
                    out = (user, new_raw, row.expires_at)
    if out is None:
        raise (BenignReplay if benign else HTTPException)(401, "invalid_refresh")
    return out


def revoke_refresh(raw: Optional[str]) -> None:
    if raw:
        with session_scope() as s:
            s.execute(update(RefreshToken).where(RefreshToken.token_sha256 == _sha(raw), RefreshToken.revoked_at.is_(None)).values(revoked_at=now()))


def revoke_all_refresh(s, user_id: int) -> None:
    s.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now()))
