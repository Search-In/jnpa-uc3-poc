"""Auth endpoints — token minting for the dashboard / PWA (Wave 3).

``/api/auth/login`` validates against ``core.app_user`` (migration 0123) via
:mod:`gateway.users`: PBKDF2-hashed passwords, per-account role, and an
``is_active`` flag. It replaces the PoC ``_seed_users()`` dict of plaintext
role-name/role-name pairs (``admin/admin`` and five siblings) that shipped in
this file — there are no credentials in source any more, and no environment
variable that can reintroduce them. Accounts come from
``scripts/seed_auth_users.py`` or ``POST /api/users``.

``/api/auth/dev-token`` mints a token for a named role without a password —
enabled only when AUTH_DEV_TOKENS=true and never in a production-like
environment. ``/api/auth/device-token`` mints the DRIVER-scoped PWA token.

The whole ``/api/auth`` prefix is listed in ``auth._PUBLIC`` so a client can
bootstrap before it holds a token. That means the global middleware does NOT
populate ``request.state.principal`` here, so the two routes that need a caller
identity (``/me``, ``/change-password``) verify the bearer themselves via
``_bearer_principal``. Console user administration deliberately lives elsewhere,
under the middleware-protected admin-only ``/api/users`` prefix.

The JWT contract is unchanged: HS256, ``{sub, role, iat, exp}`` (+ ``device_id``
for the PWA). ``sub`` now carries the account's username instead of a role name,
which is what makes per-user attribution possible downstream.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import enrollment, users
from ..auth import (
    ALL_ROLES,
    ROLE_ALIASES,
    Principal,
    Role,
    auth_enabled,
    dev_tokens_enabled,
    encode_token,
    is_production_like,
    principal_from_token,
)
from ..logging import get_logger
from ..mode import ProductionSafetyError
from ..state import GatewayState, get_state

log = get_logger("gateway.auth_router")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _bearer_principal(request: Request) -> Principal:
    """Resolve the calling principal from the Authorization header.

    Routes under ``/api/auth`` are public to the middleware, so they never get a
    ``request.state.principal``. Rather than open a hole, these routes repeat the
    same verification the middleware would have done (``principal_from_token``),
    so they behave identically whether or not AUTH_ENABLED is set.
    """
    existing = getattr(request.state, "principal", None)
    if isinstance(existing, Principal):
        return existing
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return principal_from_token(header.split(" ", 1)[1].strip())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


class LoginBody(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    auth_enabled: bool
    # Additive (existing clients ignore them): who signed in, and whether the
    # console should immediately prompt for a password change.
    username: str | None = None
    full_name: str | None = None
    must_change_password: bool = False


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody, state: GatewayState = Depends(get_state)) -> TokenResponse:
    dsn = state.cfg.postgres_dsn
    try:
        result = await users.authenticate(dsn, body.username, body.password)
    except ProductionSafetyError as exc:
        # The user store is the authority; if it is unreachable in production we
        # fail closed with a 503 rather than authenticating against anything else.
        log.error("login_store_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="user store unavailable") from exc

    if not result.ok:
        # One opaque message for every failure mode (unknown user, wrong password,
        # disabled account) so the endpoint cannot be used to enumerate accounts.
        # The distinguishing reason goes to the log, not the client.
        log.warning(
            "login_failed",
            username=users.normalize_username(body.username),
            reason=result.reason,
        )
        raise HTTPException(status_code=401, detail="invalid credentials")

    user = result.user or {}
    username = str(user.get("username", ""))
    role = str(user.get("role", ""))
    await users.touch_last_login(dsn, username)
    log.info("login_ok", username=username, role=role)
    return TokenResponse(
        access_token=encode_token(sub=username, role=role),
        role=role,
        auth_enabled=auth_enabled(),
        username=username,
        full_name=user.get("full_name"),
        must_change_password=bool(user.get("must_change_password", False)),
    )


@router.get("/me")
async def me(request: Request, state: GatewayState = Depends(get_state)) -> dict:
    """The signed-in identity behind the presented bearer token.

    The console calls this on load to decide whether its stored session is still
    good: an expired/forged token gives 401, and so does a token whose account has
    since been disabled — which is what makes ``is_active=false`` take effect
    without waiting out the token's 8 h TTL.
    """
    principal = _bearer_principal(request)
    try:
        user = await users.get_user(state.cfg.postgres_dsn, principal.sub)
    except ProductionSafetyError as exc:
        raise HTTPException(status_code=503, detail="user store unavailable") from exc

    if user is None:
        # Device and dev tokens have no console account — their sub is
        # "device:TRK-000001" / "dev:ROLE". Report the token's own identity so the
        # PWA and local dev can call /me without special-casing a 404.
        return {
            "username": principal.sub,
            "role": principal.role,
            "full_name": None,
            "is_active": True,
            "must_change_password": False,
            "device_id": principal.device_id,
            "account": False,
        }
    if not user.get("is_active", False):
        raise HTTPException(status_code=401, detail="account disabled")
    return {
        "username": user.get("username"),
        "role": user.get("role"),
        "full_name": user.get("full_name"),
        "email": user.get("email"),
        "is_active": True,
        "must_change_password": bool(user.get("must_change_password", False)),
        "last_login_at": user.get("last_login_at"),
        "device_id": None,
        "account": True,
    }


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(body: ChangePasswordBody, request: Request,
                          state: GatewayState = Depends(get_state)) -> dict:
    """Change the caller's own password (re-authenticating with the current one).

    Clears ``must_change_password``, which is how a seeded account graduates from
    its generated bootstrap password."""
    principal = _bearer_principal(request)
    dsn = state.cfg.postgres_dsn
    try:
        current = await users.authenticate(dsn, principal.sub, body.current_password)
    except ProductionSafetyError as exc:
        raise HTTPException(status_code=503, detail="user store unavailable") from exc
    if not current.ok:
        raise HTTPException(status_code=401, detail="current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="new password must differ from the current one")
    try:
        users.validate_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await users.set_password(dsn, principal.sub, body.new_password, must_change_password=False)
    log.info("password_changed", username=principal.sub)
    return {"ok": True, "username": principal.sub}


class DevTokenBody(BaseModel):
    role: str
    device_id: str | None = None


@router.post("/dev-token", response_model=TokenResponse)
async def dev_token(body: DevTokenBody) -> TokenResponse:
    # Hard environment guard (C3): the password-less seam is local-development
    # only. It is disabled in any production-like environment (staging/production)
    # regardless of AUTH_DEV_TOKENS, and otherwise only when the flag is on. Return
    # 404 so the route is indistinguishable from "not mounted" outside dev.
    if is_production_like() or not dev_tokens_enabled():
        raise HTTPException(status_code=404, detail="dev tokens disabled")
    if body.role not in ALL_ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role {body.role}")
    token = encode_token(sub=f"dev:{body.role}", role=body.role, device_id=body.device_id)
    return TokenResponse(access_token=token, role=body.role, auth_enabled=auth_enabled(),
                         username=f"dev:{body.role}")


class DeviceTokenBody(BaseModel):
    device_id: str
    pairing_secret: str | None = None


@router.post("/device-token", response_model=TokenResponse)
async def device_token(body: DeviceTokenBody,
                       state: GatewayState = Depends(get_state)) -> TokenResponse:
    """Mint a DRIVER-scoped, device-bound JWT for the Driver PWA at pairing.

    Unlike ``/dev-token`` this can ONLY ever issue the ``DRIVER`` role (never a
    control-room role), so it is safe to expose to the public PWA. It is gated by
    ``PWA_PAIRING_SECRET``:

      * when the secret is configured the request MUST present a matching
        ``pairing_secret`` (401 otherwise);
      * in a production-like environment the secret is REQUIRED — without it the
        endpoint 404s, exactly like ``/dev-token``.

    Driver-profile eligibility gate: when ``REQUIRE_DRIVER_PROFILE`` is enabled the
    entered Vehicle ID (== ``device_id``) MUST be assigned to an ACTIVE driver in
    core.driver_identity, otherwise the pairing is refused with 403. This closes the gap
    where any well-formed ``TRK-######`` could pair; the assignment is created by a
    Control-Room admin and confirmed on approval. Default-off for migration safety.

    This is the seam where a real OTP / device-attestation flow plugs in
    post-award; the token shape and DRIVER scoping stay the same.
    """
    expected = os.environ.get("PWA_PAIRING_SECRET", "").strip()
    if is_production_like() and not expected:
        # No pairing secret configured in prod → behave as if the route is absent.
        raise HTTPException(status_code=404, detail="device pairing not configured")
    if expected and (body.pairing_secret or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid pairing secret")
    device_id = body.device_id.strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")
    # Eligibility gate: the Vehicle ID must belong to an ACTIVE driver.
    if state.cfg.require_driver_profile:
        driver = await enrollment.get_active_driver_by_vehicle(
            state.cfg.postgres_dsn, device_id)
        if not driver:
            raise HTTPException(
                status_code=403, detail="Vehicle is not assigned to an active driver")
    # 12 h TTL: long enough for a driving shift, short enough to bound exposure.
    token = encode_token(
        sub=f"device:{device_id}", role=Role.DRIVER.value, device_id=device_id, ttl_s=12 * 3600
    )
    return TokenResponse(access_token=token, role=Role.DRIVER.value, auth_enabled=auth_enabled(),
                         username=f"device:{device_id}")


@router.get("/roles")
async def roles() -> dict:
    return {
        "roles": sorted(ALL_ROLES),
        # The operator-facing names accepted by user creation, and what each maps
        # to. Lets an admin UI offer "Gate user" without hardcoding the mapping.
        "aliases": dict(sorted(ROLE_ALIASES.items())),
        "auth_enabled": auth_enabled(),
    }
