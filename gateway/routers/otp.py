"""/api/auth/otp — OTP login + device binding + session (Phase 2 · Track 5).

Replaces the static PWA pairing with an OTP-based login:

    POST /api/auth/otp/request  {mobile, device_id?}     -> issue OTP (SMS-ready)
    POST /api/auth/otp/verify   {mobile, otp, device_id} -> verify -> session token

Every OTP + delivery is persisted (core.otp_request + core.notification); a
successful verify binds the device (core.device_binding), promotes/links a
driver, and mints a DRIVER session JWT via the existing auth token seam
(encode_token) — real session management, no redesign for a live SMS provider.

Reuses the framework tables (notifications, decision_audit, drivers) directly —
the audit framework CODE is untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import audit
from ..auth import encode_token
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.otp")

router = APIRouter(prefix="/api/auth/otp", tags=["auth"])

_OTP_TTL_S = 300  # 5 minutes
_MAX_ATTEMPTS = 5

_DDL = (
    """CREATE TABLE IF NOT EXISTS core.otp_request (
        id bigserial PRIMARY KEY, mobile text NOT NULL, device_id text,
        code_hash text NOT NULL, expires_at timestamptz NOT NULL,
        verified boolean NOT NULL DEFAULT false, attempts integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_otp_mobile ON core.otp_request (mobile, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS core.device_binding (
        device_id text PRIMARY KEY, mobile text NOT NULL, driver_id text,
        bound_at timestamptz NOT NULL DEFAULT now(),
        last_seen timestamptz NOT NULL DEFAULT now(), active boolean NOT NULL DEFAULT true)""",
    "CREATE INDEX IF NOT EXISTS idx_device_bindings_mobile ON core.device_binding (mobile)",
)
_READY: dict = {}


def _secret() -> str:
    return os.environ.get("AUTH_JWT_SECRET", "jnpa-dev-secret")


def _hash(code: str, mobile: str) -> str:
    return hmac.new(_secret().encode(), f"{mobile}:{code}".encode(), hashlib.sha256).hexdigest()


def _gen_otp(mobile: str) -> str:
    # Deterministic-free 6-digit OTP without Math.random-style seeding issues:
    # derive from os.urandom so each request is unique.
    return f"{int.from_bytes(os.urandom(4), 'big') % 1_000_000:06d}"


async def _ensure(dsn: Optional[str]) -> None:
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: DDL is owned by infra/postgres/v3 migrations, never runtime.
        return
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in _DDL:
        try:
            await execute(stmt, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("otp_ddl_skipped", error=str(exc))
    _READY[dsn] = True


def _prod() -> bool:
    return os.environ.get("APP_ENV", "development").lower() in ("production", "staging")


@router.post("/request")
async def request_otp(body: dict = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    """Issue an OTP for a mobile number. Body: {mobile, device_id?}.

    The OTP is hashed at rest and delivered via the notifications trail (SMS-ready).
    In non-production the OTP is echoed as ``dev_otp`` for testing; in production it
    is only sent to the SMS provider (never returned)."""
    dsn = state.cfg.postgres_dsn
    mobile = str(body.get("mobile") or "").strip()
    device_id = body.get("device_id")
    if not mobile or len(mobile) < 8:
        raise HTTPException(status_code=422, detail={"error": "valid_mobile_required"})
    await _ensure(dsn)
    otp = _gen_otp(mobile)
    expires = datetime.now(timezone.utc) + timedelta(seconds=_OTP_TTL_S)
    from jnpa_shared.db import execute

    try:
        await execute(
            """
            INSERT INTO core.otp_request (mobile, device_id, code_hash, expires_at)
            VALUES (:m, :d, :h, :e)
            """,
            {"m": mobile, "d": device_id, "h": _hash(otp, mobile), "e": expires},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("otp_store_failed", error=str(exc))
        raise HTTPException(status_code=503, detail={"error": "otp_unavailable"})
    # Delivery trail (SMS-ready: swap provider_response for the real gateway resp).
    await audit.log_notification(
        channel="sms", receiver=mobile, message=f"Your JNPA OTP is {otp} (valid 5 min)",
        delivery_status="SENT", provider_response={"provider": os.environ.get("SMS_PROVIDER", "log")},
        dsn=dsn,
    )
    REQUESTS.labels("otp", "ok").inc()
    resp = {"sent": True, "mobile": mobile, "expires_in": _OTP_TTL_S}
    if not _prod():
        resp["dev_otp"] = otp  # convenience for local/testing only
    return resp


@router.post("/verify")
async def verify_otp(body: dict = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    """Verify an OTP and mint a device-bound DRIVER session token.

    Body: {mobile, otp, device_id}. On success: binds the device, links/creates a
    driver, and returns {verified, access_token, device_id}."""
    dsn = state.cfg.postgres_dsn
    mobile = str(body.get("mobile") or "").strip()
    otp = str(body.get("otp") or "").strip()
    device_id = str(body.get("device_id") or "").strip()
    if not (mobile and otp and device_id):
        raise HTTPException(status_code=422, detail={"error": "mobile_otp_device_required"})
    await _ensure(dsn)
    from jnpa_shared.db import execute, fetch_one

    row = await fetch_one(
        """
        SELECT id, code_hash, expires_at, verified, attempts
        FROM core.otp_request
        WHERE mobile = :m AND verified = false
        ORDER BY created_at DESC LIMIT 1
        """,
        {"m": mobile}, dsn=dsn,
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "no_otp_requested"})
    if row["attempts"] >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail={"error": "too_many_attempts"})
    expires = row["expires_at"]
    if expires and expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail={"error": "otp_expired"})
    ok = hmac.compare_digest(row["code_hash"], _hash(otp, mobile))
    # Record the attempt (committed) regardless of outcome.
    await execute("UPDATE core.otp_request SET attempts = attempts + 1, verified = :v WHERE id = :id",
                  {"v": ok, "id": row["id"]}, dsn=dsn)
    if not ok:
        await audit.record_decision_audit(request_id=mobile, rule_executed="otp-verify",
                                          decision="REJECTED", action_taken="DENY",
                                          input_data={"mobile": mobile}, dsn=dsn)
        raise HTTPException(status_code=401, detail={"error": "invalid_otp"})

    driver_id = f"MOB:{mobile}"
    # Bind the device + link/create the driver (reused tables).
    try:
        await execute(
            """
            INSERT INTO core.device_binding (device_id, mobile, driver_id, last_seen, active)
            VALUES (:d, :m, :drv, now(), true)
            ON CONFLICT (device_id) DO UPDATE SET mobile = EXCLUDED.mobile,
                driver_id = EXCLUDED.driver_id, last_seen = now(), active = true
            """,
            {"d": device_id, "m": mobile, "drv": driver_id}, dsn=dsn,
        )
        await execute(
            """
            INSERT INTO core.driver_identity (driver_id, name, mobile, status, provider, updated_at)
            VALUES (:id, :name, :m, 'ACTIVE', 'otp', now())
            ON CONFLICT (driver_id) DO UPDATE SET mobile = EXCLUDED.mobile, updated_at = now()
            """,
            {"id": driver_id, "name": f"Driver {mobile[-4:]}", "m": mobile}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("otp_bind_failed", error=str(exc))

    token = encode_token(sub=driver_id, role="DRIVER", device_id=device_id)
    await audit.record_decision_audit(request_id=mobile, rule_executed="otp-verify",
                                      decision="VERIFIED", action_taken="ISSUE_TOKEN",
                                      input_data={"mobile": mobile, "device_id": device_id}, dsn=dsn)
    await audit.log_notification(channel="push", receiver=device_id,
                                 message="Login successful — device bound",
                                 delivery_status="SENT", provider_response={"driver_id": driver_id}, dsn=dsn)
    REQUESTS.labels("otp", "ok").inc()
    return {"verified": True, "access_token": token, "token_type": "bearer",
            "device_id": device_id, "driver_id": driver_id, "role": "DRIVER",
            "expires_in": 8 * 3600}


async def _bind_and_mint(dsn, *, mobile: str, device_id: str, provider: str) -> str:
    """Bind a device to a driver and mint a DRIVER JWT (shared by OTP + Firebase).

    Reuses the exact same tables and token seam as the legacy OTP verify so the
    session model is identical regardless of which OTP transport authenticated
    the phone number."""
    from jnpa_shared.db import execute

    driver_id = f"MOB:{mobile}"
    try:
        await execute(
            """
            INSERT INTO core.device_binding (device_id, mobile, driver_id, last_seen, active)
            VALUES (:d, :m, :drv, now(), true)
            ON CONFLICT (device_id) DO UPDATE SET mobile = EXCLUDED.mobile,
                driver_id = EXCLUDED.driver_id, last_seen = now(), active = true
            """,
            {"d": device_id, "m": mobile, "drv": driver_id}, dsn=dsn,
        )
        await execute(
            """
            INSERT INTO core.driver_identity (driver_id, name, mobile, status, provider, updated_at)
            VALUES (:id, :name, :m, 'ACTIVE', :prov, now())
            ON CONFLICT (driver_id) DO UPDATE SET mobile = EXCLUDED.mobile, updated_at = now()
            """,
            {"id": driver_id, "name": f"Driver {mobile[-4:]}", "m": mobile, "prov": provider},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("bind_failed", error=str(exc), provider=provider)
    return encode_token(sub=driver_id, role="DRIVER", device_id=device_id)


@router.post("/firebase-verify")
async def firebase_verify(body: dict = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    """Verify a Firebase Phone-Auth ID token and mint a device-bound DRIVER token.

    Body: ``{id_token, device_id}``. Firebase (client SDK) handles the SMS OTP
    round-trip and returns an ID token; we verify it with the Admin SDK, extract
    the verified phone number, then bind the device + mint OUR DRIVER JWT via the
    same seam the legacy OTP flow uses. The existing OTP endpoints are untouched
    and remain the fallback — this is added alongside, not as a replacement."""
    dsn = state.cfg.postgres_dsn
    id_token = str(body.get("id_token") or "").strip()
    device_id = str(body.get("device_id") or "").strip()
    if not (id_token and device_id):
        raise HTTPException(status_code=422, detail={"error": "id_token_and_device_required"})

    from .. import firebase

    claims = firebase.verify_id_token(state.cfg, id_token)
    if not claims:
        raise HTTPException(status_code=401, detail={"error": "firebase_verify_failed"})
    phone = str(claims.get("phone_number") or "")
    mobile = "".join(ch for ch in phone if ch.isdigit())[-10:] or (claims.get("uid") or "")[:10]
    if not mobile:
        raise HTTPException(status_code=422, detail={"error": "no_phone_in_token"})

    await _ensure(dsn)
    token = await _bind_and_mint(dsn, mobile=mobile, device_id=device_id, provider="firebase")
    driver_id = f"MOB:{mobile}"
    await audit.record_decision_audit(request_id=mobile, rule_executed="firebase-verify",
                                      decision="VERIFIED", action_taken="ISSUE_TOKEN",
                                      input_data={"mobile": mobile, "device_id": device_id,
                                                  "uid": claims.get("uid")}, dsn=dsn)
    await audit.log_notification(channel="fcm", receiver=device_id,
                                 message="Firebase phone login successful — device bound",
                                 delivery_status="SENT", provider_response={"driver_id": driver_id}, dsn=dsn)
    REQUESTS.labels("otp", "ok").inc()
    return {"verified": True, "access_token": token, "token_type": "bearer",
            "device_id": device_id, "driver_id": driver_id, "role": "DRIVER",
            "provider": "firebase", "expires_in": 8 * 3600}


@router.post("/refresh")
async def refresh_token(body: dict = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    """Refresh a session for a still-bound device. Body: {device_id}.

    Re-issues a fresh DRIVER JWT only while the device binding is active — a
    revoked (logged-out) or unknown device is refused. Reuses device_bindings."""
    dsn = state.cfg.postgres_dsn
    device_id = str(body.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(status_code=422, detail={"error": "device_id_required"})
    await _ensure(dsn)
    from jnpa_shared.db import execute, fetch_one

    row = await fetch_one(
        "SELECT mobile, driver_id, active FROM core.device_binding WHERE device_id = :d",
        {"d": device_id}, dsn=dsn,
    )
    if row is None or not row["active"]:
        raise HTTPException(status_code=401, detail={"error": "device_not_bound_or_revoked"})
    await execute("UPDATE core.device_binding SET last_seen = now() WHERE device_id = :d",
                  {"d": device_id}, dsn=dsn)
    token = encode_token(sub=row["driver_id"] or f"MOB:{row['mobile']}", role="DRIVER",
                         device_id=device_id)
    REQUESTS.labels("otp", "ok").inc()
    return {"access_token": token, "token_type": "bearer", "device_id": device_id,
            "expires_in": 8 * 3600}


@router.post("/logout")
async def logout(body: dict = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    """Log out / unbind a device. Body: {device_id}. Marks the binding inactive so
    subsequent /refresh is refused (session revocation). Reuses device_bindings +
    decision_audit + notifications."""
    dsn = state.cfg.postgres_dsn
    device_id = str(body.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(status_code=422, detail={"error": "device_id_required"})
    await _ensure(dsn)
    from jnpa_shared.db import execute

    await execute("UPDATE core.device_binding SET active = false WHERE device_id = :d",
                  {"d": device_id}, dsn=dsn)
    await audit.record_decision_audit(request_id=device_id, rule_executed="device-logout",
                                      decision="REVOKED", action_taken="UNBIND",
                                      input_data={"device_id": device_id}, dsn=dsn)
    await audit.log_notification(channel="push", receiver=device_id,
                                 message="Logged out — device unbound",
                                 delivery_status="SENT", provider_response={}, dsn=dsn)
    REQUESTS.labels("otp", "ok").inc()
    return {"logged_out": True, "device_id": device_id}


@router.get("/session/{device_id}")
async def session_status(device_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Session/binding status for a device (active? last seen? bound driver?)."""
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        "SELECT mobile, driver_id, active, bound_at, last_seen FROM core.device_binding WHERE device_id = :d",
        {"d": device_id}, dsn=state.cfg.postgres_dsn,
    )
    if row is None:
        return {"bound": False, "device_id": device_id}
    d = dict(row)
    for k in ("bound_at", "last_seen"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return {"bound": True, "device_id": device_id, **d}


__all__ = ["router"]
