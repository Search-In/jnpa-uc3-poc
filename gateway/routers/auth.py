"""Auth endpoints — token minting for the dashboard / PWA (Wave 3).

`/api/auth/login` is the OIDC-ready seam: in the PoC it validates against a small
seeded user table (env-overridable) and returns a signed JWT carrying the role.
`/api/auth/dev-token` mints a token for a named role without a password — enabled
only when AUTH_DEV_TOKENS=true (default true in the demo profile, set false in
production). Both are public (listed in auth._PUBLIC) so a client can bootstrap.

In production, swap `/login` for the real IdP and disable `/dev-token`.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import enrollment
from ..auth import (
    ALL_ROLES,
    Role,
    auth_enabled,
    dev_tokens_enabled,
    encode_token,
    is_production_like,
)
from ..state import GatewayState, get_state

router = APIRouter(prefix="/api/auth", tags=["auth"])


# Seeded demo users (username -> (password, role)). Override the whole table via
# AUTH_USERS="user:pass:ROLE,..." in production-adjacent setups; replace with a
# real IdP post-award.
def _seed_users() -> dict[str, tuple[str, str]]:
    raw = os.environ.get("AUTH_USERS", "").strip()
    if raw:
        out: dict[str, tuple[str, str]] = {}
        for entry in raw.split(","):
            parts = entry.split(":")
            if len(parts) == 3 and parts[2] in ALL_ROLES:
                out[parts[0]] = (parts[1], parts[2])
        if out:
            return out
    # PoC defaults — one account per role.
    return {
        "traffic": ("traffic", Role.JNPA_TRAFFIC.value),
        "terminal": ("terminal", Role.TERMINAL_OPS.value),
        "customs": ("customs", Role.CUSTOMS.value),
        "police": ("police", Role.TRAFFIC_POLICE.value),
        "driver": ("driver", Role.DRIVER.value),
        "admin": ("admin", Role.DTCCC_ADMIN.value),
    }


class LoginBody(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    auth_enabled: bool


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody) -> TokenResponse:
    users = _seed_users()
    rec = users.get(body.username)
    if not rec or rec[0] != body.password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    password, role = rec
    token = encode_token(sub=body.username, role=role)
    return TokenResponse(access_token=token, role=role, auth_enabled=auth_enabled())


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
    return TokenResponse(access_token=token, role=body.role, auth_enabled=auth_enabled())


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
    jnpa.drivers, otherwise the pairing is refused with 403. This closes the gap
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
    return TokenResponse(access_token=token, role=Role.DRIVER.value, auth_enabled=auth_enabled())


@router.get("/roles")
async def roles() -> dict:
    return {"roles": sorted(ALL_ROLES), "auth_enabled": auth_enabled()}
