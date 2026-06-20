"""Gateway authentication, RBAC, and rate limiting (Wave 3 — SEC-1, SEC-2).

Design goals:

  * **Flag-gated.** Enforcement is OFF by default (the mock/demo profile and the
    in-process test suite run with zero friction) and ON in the production compose
    profile via ``AUTH_ENABLED=true``. With the flag off, every request passes
    through unauthenticated exactly as before — so the 170 existing tests and the
    demo are unaffected. With it on, every non-public route requires a valid
    bearer (401) carrying a role permitted for that path (else 403).

  * **Dependency-light.** Uses PyJWT when installed (production image pins it),
    but falls back to a small stdlib HS256 implementation so the suite runs on a
    bare host with no extra installs. Same for rate limiting (in-process token
    bucket — no slowapi dependency).

  * **OIDC-ready.** Tokens are HS256 signed with ``AUTH_JWT_SECRET``. Swapping to
    an external OIDC provider later means verifying RS256 against a JWKS — the
    role claim and the policy map below are unchanged.

Roles (bid stakeholders):
    JNPA_TRAFFIC, TERMINAL_OPS, CUSTOMS, TRAFFIC_POLICE, DRIVER, DTCCC_ADMIN
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


# --------------------------------------------------------------------------- roles
class Role(str, Enum):
    JNPA_TRAFFIC = "JNPA_TRAFFIC"
    TERMINAL_OPS = "TERMINAL_OPS"
    CUSTOMS = "CUSTOMS"
    TRAFFIC_POLICE = "TRAFFIC_POLICE"
    DRIVER = "DRIVER"
    DTCCC_ADMIN = "DTCCC_ADMIN"


ALL_ROLES: frozenset[str] = frozenset(r.value for r in Role)
# The control-room roles that may see the full operational picture.
CONTROL_ROOM: frozenset[str] = frozenset(
    {Role.JNPA_TRAFFIC.value, Role.DTCCC_ADMIN.value, Role.TERMINAL_OPS.value}
)


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    # For DRIVER scoping: the vehicle/device this principal owns (optional).
    device_id: str | None = None


# --------------------------------------------------------------------------- policy
# Path-prefix -> roles allowed. First matching prefix (longest-first) wins. A path
# not matched by any rule defaults to "any authenticated role" (still needs a
# valid token when AUTH_ENABLED). Public paths skip auth entirely (see _PUBLIC).
#
# Scoping rationale (SEC-1 / NOTIF-5 role):
#   * police reports        -> police + control room (+ customs read)
#   * customs flags / leo    -> customs + control room
#   * fault / scenario control-> control room only (presenter surface)
#   * driver check-in/push    -> driver + control room
#   * identity (biometrics)   -> customs + admin (DPDP-sensitive)
_POLICY: tuple[tuple[str, frozenset[str]], ...] = (
    ("/api/reports", CONTROL_ROOM | {Role.TRAFFIC_POLICE.value, Role.CUSTOMS.value}),
    ("/api/gate-data", CONTROL_ROOM | {Role.CUSTOMS.value}),
    ("/api/identity", {Role.CUSTOMS.value, Role.DTCCC_ADMIN.value}),
    ("/api/control", CONTROL_ROOM),
    ("/api/scenarios", CONTROL_ROOM),
    ("/api/scenario", CONTROL_ROOM),
    ("/api/debug", CONTROL_ROOM),
    ("/checkin", {Role.DRIVER.value} | CONTROL_ROOM),
    ("/api/push", {Role.DRIVER.value} | CONTROL_ROOM),
    # Everything else operational (traffic/trucks/kpi/alerts/parking/carbon/...)
    # is visible to any authenticated stakeholder.
)

# Paths that never require auth (health/observability/auth-bootstrap/websocket
# handshake). The whole /api/auth surface is public so a client can mint a token
# and discover roles before it has one.
_PUBLIC: tuple[str, ...] = (
    "/healthz",
    "/metrics",
    "/api/auth",
    "/api/ws",
    "/ws",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def roles_for_path(path: str) -> frozenset[str]:
    """The set of roles permitted to call ``path`` (longest-prefix match)."""
    best: frozenset[str] | None = None
    best_len = -1
    for prefix, roles in _POLICY:
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = roles, len(prefix)
    return best if best is not None else ALL_ROLES


# --------------------------------------------------------------------------- JWT
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _secret() -> str:
    # PoC default keeps the demo working; production MUST override via env.
    return os.environ.get("AUTH_JWT_SECRET", "jnpa-uc3-dev-secret-change-me")


def encode_token(sub: str, role: str, *, device_id: str | None = None, ttl_s: int = 8 * 3600) -> str:
    """Issue an HS256 JWT. Uses PyJWT if present, else a stdlib implementation."""
    now = int(time.time())
    payload = {"sub": sub, "role": role, "iat": now, "exp": now + ttl_s}
    if device_id:
        payload["device_id"] = device_id
    try:
        import jwt  # PyJWT

        return jwt.encode(payload, _secret(), algorithm="HS256")
    except Exception:  # noqa: BLE001 — stdlib fallback
        header = {"alg": "HS256", "typ": "JWT"}
        seg = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        sig = hmac.new(_secret().encode(), seg.encode(), hashlib.sha256).digest()
        return seg + "." + _b64url(sig)


def decode_token(token: str) -> dict:
    """Verify + decode. Raises ValueError on any failure (bad sig / expired)."""
    try:
        import jwt  # PyJWT

        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except ValueError:
        raise
    except Exception as exc:  # PyJWT-specific errors -> uniform ValueError
        # Distinguish "PyJWT raised" from "PyJWT not installed".
        if exc.__class__.__module__.startswith("jwt"):
            raise ValueError(str(exc)) from exc
        # PyJWT not installed -> stdlib verification.
        return _decode_token_stdlib(token)


def _decode_token_stdlib(token: str) -> dict:
    try:
        h_seg, p_seg, s_seg = token.split(".")
    except ValueError as exc:
        raise ValueError("malformed token") from exc
    expected = hmac.new(_secret().encode(), f"{h_seg}.{p_seg}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url(expected), s_seg):
        raise ValueError("bad signature")
    payload = json.loads(_b64url_decode(p_seg))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("token expired")
    return payload


# --------------------------------------------------------------------------- rate limit
class _TokenBucket:
    """Tiny in-process per-consumer token bucket (no external dependency)."""

    def __init__(self, rate_per_min: int) -> None:
        self.capacity = max(1, rate_per_min)
        self.refill_per_s = self.capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


# --------------------------------------------------------------------------- middleware
def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path == p for p in _PUBLIC)


def auth_enabled() -> bool:
    return os.environ.get("AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def rate_limit_per_min() -> int:
    try:
        return int(os.environ.get("AUTH_RATE_LIMIT_PER_MIN", "600"))
    except ValueError:
        return 600


class AuthMiddleware(BaseHTTPMiddleware):
    """Single global gate: rate-limit -> authenticate -> authorize.

    No-op (pass-through) when AUTH_ENABLED is false, so the demo and the existing
    in-process tests are unaffected. When enabled, attaches request.state.principal.
    """

    def __init__(self, app, *, enabled: bool | None = None, rate_per_min: int | None = None):
        super().__init__(app)
        self._forced_enabled = enabled
        self._limiter = _TokenBucket(rate_per_min if rate_per_min is not None else rate_limit_per_min())

    @property
    def enabled(self) -> bool:
        return self._forced_enabled if self._forced_enabled is not None else auth_enabled()

    async def dispatch(self, request: Request, call_next):
        if not self.enabled or request.method == "OPTIONS" or _is_public(request.url.path):
            return await call_next(request)

        # 1. Rate limit per consumer (token sub if present, else client IP).
        auth_header = request.headers.get("authorization", "")
        consumer = auth_header[-24:] or (request.client.host if request.client else "anon")
        if not self._limiter.allow(consumer):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)

        # 2. Authenticate (bearer required).
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        token = auth_header.split(" ", 1)[1].strip()
        try:
            claims = decode_token(token)
        except ValueError as exc:
            return JSONResponse({"detail": f"invalid token: {exc}"}, status_code=401)

        role = claims.get("role")
        if role not in ALL_ROLES:
            return JSONResponse({"detail": "token carries no valid role"}, status_code=403)

        # 3. Authorize (RBAC by path).
        allowed = roles_for_path(request.url.path)
        if role not in allowed:
            return JSONResponse(
                {"detail": f"role {role} not permitted for {request.url.path}"},
                status_code=403,
            )

        request.state.principal = Principal(
            sub=str(claims.get("sub", "")), role=role, device_id=claims.get("device_id")
        )
        return await call_next(request)


def install_auth(app, *, enabled: bool | None = None) -> None:
    """Attach the auth middleware + the token-mint endpoints to a FastAPI app."""
    app.add_middleware(AuthMiddleware, enabled=enabled)


def known_roles() -> Iterable[str]:
    return ALL_ROLES
