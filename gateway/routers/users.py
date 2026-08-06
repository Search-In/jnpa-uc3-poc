"""Console user administration — admin-only CRUD over ``core.app_user``.

Mounted at ``/api/users``, which ``gateway.auth._POLICY`` restricts to
``DTCCC_ADMIN``. It lives here rather than under ``/api/auth`` on purpose: the
whole ``/api/auth`` prefix is in ``auth._PUBLIC`` and skips the middleware, so
routes that mint or revoke other people's access must not sit there.

``require_admin`` repeats the role check locally as defence-in-depth and to name
the actor for the audit log. It follows the established convention in this
package (see ``performance_upload.require_admin``): with AUTH_ENABLED off the
whole app is open anyway, so it allows and attributes to "dev".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .. import users as user_store
from ..auth import ROLE_ALIASES, Role, auth_enabled, normalize_role
from ..logging import get_logger
from ..mode import ProductionSafetyError
from ..state import GatewayState, get_state

log = get_logger("gateway.users_router")

router = APIRouter(prefix="/api/users", tags=["users"])


def require_admin(request: Request) -> str:
    """Admin-only gate; returns the acting username for the audit trail."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    if principal is None or getattr(principal, "role", None) != Role.DTCCC_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "admin_required",
                    "detail": "User administration requires the DTCCC_ADMIN role"},
        )
    return getattr(principal, "sub", "admin")


def _dsn(state: GatewayState) -> str:
    return state.cfg.postgres_dsn


def _unavailable(exc: ProductionSafetyError) -> HTTPException:
    log.error("user_store_unavailable", error=str(exc))
    return HTTPException(status_code=503, detail="user store unavailable")


@router.get("")
async def list_users(request: Request, state: GatewayState = Depends(get_state)) -> dict:
    """Every console account. Password hashes are stripped by the store layer."""
    require_admin(request)
    try:
        rows = await user_store.list_users(_dsn(state))
    except ProductionSafetyError as exc:
        raise _unavailable(exc) from exc
    return {"users": rows, "count": len(rows), "roles": sorted(ROLE_ALIASES.keys())}


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str
    full_name: str | None = None
    email: str | None = None
    # Seeded/admin-created accounts start needing a password change by default;
    # the console prompts on first login and the flag clears on change.
    must_change_password: bool = True


@router.post("", status_code=201)
async def create_user(body: CreateUserBody, request: Request,
                      state: GatewayState = Depends(get_state)) -> dict:
    """Create an account. ``role`` accepts a canonical role or an alias
    (ADMIN / OPERATOR / GATE_USER / TRANSPORTER)."""
    actor = require_admin(request)
    try:
        created = await user_store.create_user(
            _dsn(state),
            username=body.username,
            password=body.password,
            role=body.role,
            full_name=body.full_name,
            email=body.email,
            must_change_password=body.must_change_password,
        )
    except ProductionSafetyError as exc:
        raise _unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("user_created", username=created.get("username"),
             role=created.get("role"), actor=actor)
    return created


async def _set_active(username: str, active: bool, request: Request,
                      state: GatewayState) -> dict:
    actor = require_admin(request)
    normalized = user_store.normalize_username(username)
    # An admin disabling their own account would lock themselves out the moment
    # the console re-validates the session against /api/auth/me.
    if not active and auth_enabled() and normalized == user_store.normalize_username(actor):
        raise HTTPException(status_code=400, detail="you cannot disable your own account")
    try:
        updated = await user_store.set_active(_dsn(state), normalized, active)
    except ProductionSafetyError as exc:
        raise _unavailable(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"user {normalized!r} not found")
    log.info("user_active_changed", username=normalized, is_active=active, actor=actor)
    return updated


@router.post("/{username}/disable")
async def disable_user(username: str, request: Request,
                       state: GatewayState = Depends(get_state)) -> dict:
    """Revoke console access. The account's next login and its next /api/auth/me
    check both fail; an already-issued JWT stays valid until it expires (max 8 h),
    since tokens are stateless by design."""
    return await _set_active(username, False, request, state)


@router.post("/{username}/enable")
async def enable_user(username: str, request: Request,
                      state: GatewayState = Depends(get_state)) -> dict:
    return await _set_active(username, True, request, state)


class ResetPasswordBody(BaseModel):
    new_password: str
    must_change_password: bool = True


@router.post("/{username}/reset-password")
async def reset_password(username: str, body: ResetPasswordBody, request: Request,
                         state: GatewayState = Depends(get_state)) -> dict:
    """Admin password reset (no knowledge of the old password). Distinct from
    ``/api/auth/change-password``, which is self-service and re-authenticates."""
    actor = require_admin(request)
    try:
        updated = await user_store.set_password(
            _dsn(state), username, body.new_password,
            must_change_password=body.must_change_password)
    except ProductionSafetyError as exc:
        raise _unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} not found")
    log.info("user_password_reset", username=updated.get("username"), actor=actor)
    return {"ok": True, "username": updated.get("username")}


@router.get("/roles")
async def roles(request: Request) -> dict:
    """Role names accepted by create-user, with the alias mapping resolved."""
    require_admin(request)
    return {
        "roles": sorted({*ROLE_ALIASES.values(), *(r.value for r in Role)}),
        "aliases": {k: normalize_role(k) for k in sorted(ROLE_ALIASES)},
    }
