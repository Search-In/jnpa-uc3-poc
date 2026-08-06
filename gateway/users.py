"""Console user store — database-backed accounts for the dashboard login.

Replaces the PoC ``_seed_users()`` dict in :mod:`gateway.routers.auth`, which
held one PLAINTEXT password per role (including the well-known ``admin/admin``)
directly in source. Accounts now live in ``core.app_user`` (migration 0123) and
carry a PBKDF2-HMAC-SHA256 digest; nothing in this module or anywhere else in the
tree contains a password. Accounts are created only by
``scripts/seed_auth_users.py`` or by an admin through ``POST /api/users``.

Persistence follows the same contract as :mod:`gateway.enrollment`: Postgres is
the source of truth, and when it is unreachable the module degrades to an
in-process dict *in development only* — in production an unavailable database
raises :class:`~gateway.mode.ProductionSafetyError` so the route returns 503
rather than silently authenticating against a store that isn't there.

The in-memory fallback starts **empty** and is never auto-populated. That is the
whole point of this rewrite: a database that is down must make login fail closed,
never re-materialise a default credential.

Hashing is dependency-light on purpose, matching the stated design goal in
:mod:`gateway.auth`: PBKDF2-HMAC-SHA256 from ``hashlib`` needs no bcrypt/passlib
wheel in the image. The iteration count is stored inside each digest, so raising
``AUTH_PBKDF2_ITERATIONS`` later keeps every existing hash verifiable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from .auth import ALL_ROLES, normalize_role
from .logging import get_logger
from .mode import ProductionSafetyError, allow_memory_store, production_mode

log = get_logger("gateway.users")

# --------------------------------------------------------------------------- policy
# Minimum length accepted by create/change-password. Deliberately a floor, not a
# composition rule: length beats character-class theatre, and the seed script
# generates far longer secrets than this.
MIN_PASSWORD_LENGTH = 8

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 200_000
_SALT_BYTES = 16


def _iterations() -> int:
    """PBKDF2 rounds for NEW hashes. Overridable so the in-process test suite can
    drop it to something instant; existing digests carry their own count and keep
    verifying regardless of this value."""
    try:
        return max(1, int(os.environ.get("AUTH_PBKDF2_ITERATIONS", _DEFAULT_ITERATIONS)))
    except ValueError:
        return _DEFAULT_ITERATIONS


# --------------------------------------------------------------------------- hashing
def hash_password(password: str) -> str:
    """Hash a password as ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``.

    Each call draws a fresh random salt, so the same password hashes differently
    for two users and the digests cannot be compared or rainbow-tabled."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    iterations = _iterations()
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "{}${}${}${}".format(
        _ALGORITHM,
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify of ``password`` against a stored digest.

    Returns False (never raises) for a malformed or unknown-algorithm digest, so
    a corrupted row denies access instead of 500-ing the login route."""
    if not password or not stored:
        return False
    try:
        algorithm, iter_s, salt_b64, hash_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


_DUMMY_HASH: Dict[int, str] = {}


def _dummy_hash() -> str:
    """A throwaway digest used to equalise the cost of an unknown-user login.

    Cached per iteration count so the equalising comparison costs the same as a
    real one (one PBKDF2 pass) rather than double."""
    n = _iterations()
    if n not in _DUMMY_HASH:
        _DUMMY_HASH[n] = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH[n]


def validate_password(password: str) -> None:
    """Raise ValueError when a password fails policy. Shared by create + change."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def normalize_username(username: str) -> str:
    """Canonical form for storage + lookup: trimmed and lowercased, matching the
    ``uq_app_user_username_lower`` index so login is case-insensitive."""
    return (username or "").strip().lower()


# --------------------------------------------------------------------------- backend
# Memoised per-DSN backend choice ("db" | "mem"), mirroring gateway.enrollment.
_BACKEND: Dict[str, str] = {}
# Development-only fallback store: username -> row dict. Starts empty and is only
# ever filled by an explicit create_user() call.
_MEM_USERS: Dict[str, dict] = {}


def reset_memory_store() -> None:
    """Drop the in-process fallback store (test hook — never called at runtime)."""
    _MEM_USERS.clear()
    _BACKEND.clear()


async def _backend(dsn: str) -> str:
    """Resolve (and memoise) whether to read users from Postgres or memory.

    In development an unreachable database pins the empty in-memory store, so a
    local run without infra still boots (login simply has no accounts to match —
    the demo profile runs with AUTH_ENABLED=false and never reaches login). In
    production it raises, because authenticating against a store that silently
    lost its rows is worse than returning 503.
    """
    key = dsn or ""
    cached = _BACKEND.get(key)
    if cached:
        return cached
    if not key:
        if production_mode():
            raise ProductionSafetyError("postgres", "POSTGRES_DSN is not set")
        _BACKEND[key] = "mem"
        return "mem"
    try:
        from jnpa_shared.db import execute  # lazy import

        await execute("SELECT 1", dsn=dsn)
        _BACKEND[key] = "db"
        log.info("user_store_backend", backend="db")
        return "db"
    except Exception as exc:  # noqa: BLE001
        if not allow_memory_store():
            log.error("user_store_db_unavailable_production", error=str(exc))
            raise ProductionSafetyError("postgres", str(exc)) from exc
        _BACKEND[key] = "mem"
        log.warning("user_store_db_unavailable_using_memory", error=str(exc))
        return "mem"


def _public(row: Any) -> dict:
    """Row -> API-safe dict. Drops password_hash unconditionally: no caller has a
    reason to see it, so it must not be able to leak through a response model."""
    d = dict(row)
    d.pop("password_hash", None)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# --------------------------------------------------------------------------- reads
async def get_user(dsn: str, username: str, *, with_hash: bool = False) -> Optional[dict]:
    """Fetch one account by (case-insensitive) username, or None."""
    uname = normalize_username(username)
    if not uname:
        return None
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        row = await fetch_one(
            "SELECT user_id, username, password_hash, role, full_name, email, "
            "is_active, must_change_password, last_login_at, created_at, updated_at "
            "FROM core.app_user WHERE lower(username) = :u LIMIT 1",
            {"u": uname}, dsn=dsn)
        if not row:
            return None
        return dict(row) if with_hash else _public(row)
    rec = _MEM_USERS.get(uname)
    if not rec:
        return None
    return dict(rec) if with_hash else _public(rec)


async def list_users(dsn: str) -> List[dict]:
    """All accounts, newest first. Never includes password hashes."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        rows = await fetch_all(
            "SELECT user_id, username, role, full_name, email, is_active, "
            "must_change_password, last_login_at, created_at, updated_at "
            "FROM core.app_user ORDER BY created_at DESC, username ASC",
            dsn=dsn)
        return [_public(r) for r in rows]
    return sorted(
        (_public(r) for r in _MEM_USERS.values()),
        key=lambda r: str(r.get("username", "")),
    )


async def count_users(dsn: str) -> int:
    """Number of accounts — used by the seed script to report an empty install."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        row = await fetch_one("SELECT count(*) AS n FROM core.app_user", dsn=dsn)
        return int(dict(row)["n"]) if row else 0
    return len(_MEM_USERS)


# --------------------------------------------------------------------------- writes
async def create_user(dsn: str, *, username: str, password: str, role: str,
                      full_name: Optional[str] = None, email: Optional[str] = None,
                      must_change_password: bool = True) -> dict:
    """Create an account. Raises ValueError on bad input or a duplicate username.

    ``role`` accepts either a canonical role or an operator-facing alias
    (ADMIN / OPERATOR / GATE_USER); it is normalised before it is stored, so the
    column always matches gateway.auth.Role and the 0123 CHECK constraint."""
    uname = normalize_username(username)
    if not uname:
        raise ValueError("username is required")
    canonical = normalize_role(role)
    if canonical is None or canonical not in ALL_ROLES:
        raise ValueError(f"unknown role {role!r}")
    validate_password(password)
    if await get_user(dsn, uname) is not None:
        raise ValueError(f"user {uname!r} already exists")

    pw_hash = hash_password(password)
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        await execute(
            "INSERT INTO core.app_user "
            "(username, password_hash, role, full_name, email, must_change_password) "
            "VALUES (:u, :h, :r, :fn, :em, :mcp)",
            {"u": uname, "h": pw_hash, "r": canonical, "fn": full_name,
             "em": email, "mcp": must_change_password}, dsn=dsn)
        created = await get_user(dsn, uname)
        if created is None:  # pragma: no cover — insert succeeded, row must exist
            raise RuntimeError("user insert did not persist")
        return created

    now = datetime.now().astimezone()
    _MEM_USERS[uname] = {
        "user_id": len(_MEM_USERS) + 1,
        "username": uname,
        "password_hash": pw_hash,
        "role": canonical,
        "full_name": full_name,
        "email": email,
        "is_active": True,
        "must_change_password": must_change_password,
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    return _public(_MEM_USERS[uname])


async def set_active(dsn: str, username: str, active: bool) -> Optional[dict]:
    """Enable/disable an account. Returns the updated row, or None if unknown.

    Disabling is the supported way to revoke access. Note it does not invalidate
    an already-issued JWT (tokens are stateless, max 8 h) — it blocks the next
    login and every /api/auth/me check the console makes on load."""
    uname = normalize_username(username)
    if await get_user(dsn, uname) is None:
        return None
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        await execute(
            "UPDATE core.app_user SET is_active = :a, updated_at = now() "
            "WHERE lower(username) = :u",
            {"a": active, "u": uname}, dsn=dsn)
        return await get_user(dsn, uname)
    _MEM_USERS[uname]["is_active"] = active
    _MEM_USERS[uname]["updated_at"] = datetime.now().astimezone()
    return _public(_MEM_USERS[uname])


async def set_password(dsn: str, username: str, password: str, *,
                       must_change_password: bool = False) -> Optional[dict]:
    """Replace an account's password. Returns the updated row, or None if unknown."""
    uname = normalize_username(username)
    validate_password(password)
    if await get_user(dsn, uname) is None:
        return None
    pw_hash = hash_password(password)
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        await execute(
            "UPDATE core.app_user SET password_hash = :h, must_change_password = :mcp, "
            "updated_at = now() WHERE lower(username) = :u",
            {"h": pw_hash, "mcp": must_change_password, "u": uname}, dsn=dsn)
        return await get_user(dsn, uname)
    _MEM_USERS[uname]["password_hash"] = pw_hash
    _MEM_USERS[uname]["must_change_password"] = must_change_password
    _MEM_USERS[uname]["updated_at"] = datetime.now().astimezone()
    return _public(_MEM_USERS[uname])


async def touch_last_login(dsn: str, username: str) -> None:
    """Stamp last_login_at after a successful authentication (best-effort — a
    failure here must never turn a good login into an error)."""
    uname = normalize_username(username)
    try:
        if await _backend(dsn) == "db":
            from jnpa_shared.db import execute

            # now() server-side rather than a bound parameter: asyncpg rejects an
            # ISO string for timestamptz, and the DB clock is the right authority.
            await execute(
                "UPDATE core.app_user SET last_login_at = now() WHERE lower(username) = :u",
                {"u": uname}, dsn=dsn)
        elif uname in _MEM_USERS:
            _MEM_USERS[uname]["last_login_at"] = datetime.now().astimezone()
    except Exception as exc:  # noqa: BLE001
        log.warning("last_login_stamp_failed", username=uname, error=str(exc))


# --------------------------------------------------------------------------- auth
class AuthResult:
    """Outcome of :func:`authenticate` — distinguishes *why* a login failed so the
    route can log the reason while still returning one opaque 401 to the client."""

    __slots__ = ("user", "reason")

    def __init__(self, user: Optional[dict], reason: str = "") -> None:
        self.user = user
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.user is not None


async def authenticate(dsn: str, username: str, password: str) -> AuthResult:
    """Verify credentials against the store.

    Always runs the PBKDF2 comparison, even for an unknown username, so the
    response time does not reveal whether an account exists.
    """
    uname = normalize_username(username)
    rec = await get_user(dsn, uname, with_hash=True) if uname else None

    # Unknown user: still run one PBKDF2 comparison, against a throwaway digest,
    # so "no such user" and "wrong password" take the same time and the endpoint
    # is not an account-existence oracle.
    stored = str(rec.get("password_hash")) if rec else _dummy_hash()
    matched = verify_password(password or "", stored)

    if not rec or not matched:
        return AuthResult(None, "bad_credentials")
    if not rec.get("is_active", False):
        return AuthResult(None, "inactive")
    return AuthResult(_public(rec))
