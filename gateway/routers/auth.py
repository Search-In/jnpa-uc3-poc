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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import (
    ALL_ROLES,
    Role,
    auth_enabled,
    dev_tokens_enabled,
    encode_token,
    is_production_like,
)

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


@router.get("/roles")
async def roles() -> dict:
    return {"roles": sorted(ALL_ROLES), "auth_enabled": auth_enabled()}
