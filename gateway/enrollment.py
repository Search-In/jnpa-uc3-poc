"""Driver-enrolment store + lifecycle (Identity / face-recognition, Appendix C #2).

Backs the PWA submit -> admin approve/reject workflow that sits in front of the
identity service. Records live in ``jnpa.driver_enrollments`` (+ an append-only
``jnpa.enrollment_audit``); when Postgres is absent (the mock/test/local profile)
every operation degrades to an in-process dict so the demo still works end to end.

The identity service itself is unchanged: on approval the gateway calls its
existing ``/enrol`` to mint + store the face template. This module only owns the
*request queue*, the *approval state machine*, and the *DPDP audit*.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .logging import get_logger
from .mode import ProductionSafetyError, allow_memory_store, production_mode

log = get_logger("gateway.enrollment")

# Allowed lifecycle states (mirrors the CHECK constraint in infra/postgres/init.sql).
PENDING = "PENDING"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"
REENROLL = "REENROLL"

# --- schema (idempotent; also applied here so an existing volume gains the tables
# without an init.sql re-run) ------------------------------------------------
_DDL = """
CREATE SCHEMA IF NOT EXISTS jnpa;
CREATE TABLE IF NOT EXISTS jnpa.driver_enrollments (
    driver_id         text PRIMARY KEY,
    name              text NOT NULL,
    license_no        text,
    mobile            text,
    vehicle_no        text,
    aadhaar_masked    text,
    emergency_contact text,
    status            text NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING', 'ACTIVE', 'REJECTED', 'REENROLL')),
    consent           boolean NOT NULL DEFAULT false,
    consent_at        timestamptz,
    face_images       jsonb NOT NULL DEFAULT '[]'::jsonb,
    reference_image   text,
    photo_url         text,
    documents         jsonb NOT NULL DEFAULT '[]'::jsonb,
    template_dim      int,
    provider          text,
    submitted_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at       timestamptz,
    reviewed_by       text,
    rejection_reason  text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_driver_enrol_status
    ON jnpa.driver_enrollments (status, submitted_at DESC);
CREATE TABLE IF NOT EXISTS jnpa.enrollment_audit (
    id        bigserial PRIMARY KEY,
    driver_id text NOT NULL,
    event     text NOT NULL,
    actor     text,
    detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_enrollment_audit_driver
    ON jnpa.enrollment_audit (driver_id, ts DESC);
CREATE TABLE IF NOT EXISTS jnpa.drivers (
    driver_id         text PRIMARY KEY,
    name              text NOT NULL,
    license_no        text,
    mobile            text,
    vehicle_no        text,
    aadhaar_masked    text,
    emergency_contact text,
    status            text NOT NULL DEFAULT 'ACTIVE'
                      CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    photo_url         text,
    reference_image   text,
    template_dim      int,
    provider          text,
    enrolled_at       timestamptz NOT NULL DEFAULT now(),
    approved_by       text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jnpa.driver_faces (
    driver_id     text PRIMARY KEY,
    embedding     jsonb NOT NULL,
    dim           int NOT NULL,
    provider      text,
    model_version text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jnpa.verification_logs (
    id            bigserial PRIMARY KEY,
    driver_id     text NOT NULL,
    decision      text NOT NULL,
    score         double precision,
    matched       boolean,
    provider      text,
    decision_path text,
    actor         text,
    purpose       text,
    reason        text,
    ts            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_verification_logs_driver
    ON jnpa.verification_logs (driver_id, ts DESC);
"""

# in-memory fallback store (DEV ONLY — used when no Postgres DSN is reachable)
_MEM: Dict[str, dict] = {}          # driver_enrollments (workflow)
_MEM_AUDIT: List[dict] = []         # enrollment_audit
_MEM_DRIVERS: Dict[str, dict] = {}  # drivers (master)
_MEM_VLOGS: List[dict] = []         # verification_logs
_MEM_FACES: Dict[str, dict] = {}    # driver_faces (1:N embedding store)
# Resolved backend per DSN: None (undetermined) | "db" | "mem".
_BACKEND: Dict[str, str] = {}

# Fields exposed in list/detail views (raw base64 face frames are only returned
# from the detail/get path, never the list, to keep payloads small).
_SUMMARY_COLS = (
    "driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, "
    "emergency_contact, status, consent, consent_at, photo_url, template_dim, "
    "provider, submitted_at, reviewed_at, reviewed_by, rejection_reason, updated_at"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else [])


def _loads(val: Any) -> Any:
    """jsonb may come back from asyncpg as a str (no type info via text()); parse it."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:  # noqa: BLE001
            return val
    return val


def decode_data_url(data: Optional[str]) -> Optional[bytes]:
    """Decode a base64 / data-URL image string to raw bytes (None if absent/bad)."""
    if not data:
        return None
    try:
        payload = data.split(",", 1)[1] if data.strip().startswith("data:") else data
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return None


async def _backend(dsn: str) -> str:
    """Resolve (and memoise) whether to use Postgres or the in-memory fallback.

    Applies the idempotent schema once. In DEV any failure (no DSN, DB down) pins
    the in-memory backend so the demo runs without infra. In PRODUCTION an
    unavailable Postgres is fatal-per-request: it raises ProductionSafetyError so
    the route returns a structured 503 instead of silently losing data to memory.
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

        for stmt in (s.strip() for s in _DDL.split(";")):
            if stmt:
                await execute(stmt, dsn=dsn)
        _BACKEND[key] = "db"
        log.info("enrollment_store_backend", backend="db")
        return "db"
    except Exception as exc:  # noqa: BLE001
        if not allow_memory_store():
            # Production: do not fall back to memory — fail loud and safe.
            log.error("enrollment_store_db_unavailable_production", error=str(exc))
            raise ProductionSafetyError("postgres", str(exc)) from exc
        _BACKEND[key] = "mem"
        log.warning("enrollment_store_db_unavailable_using_memory", error=str(exc))
        return "mem"


async def ensure_backend(dsn: str) -> str:
    """Resolve the persistence backend, surfacing a production failure.

    Public entry point for the startup gate / healthz: in production a missing or
    unreachable Postgres raises :class:`ProductionSafetyError` (the route/boot turns
    it into a 503 / fail-fast); in dev it pins the in-memory store."""
    return await _backend(dsn)


# --------------------------------------------------------------------------- audit
async def audit(dsn: str, driver_id: str, event: str, *, actor: str,
                detail: Optional[Dict[str, Any]] = None) -> None:
    detail = detail or {}
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import execute

            await execute(
                "INSERT INTO jnpa.enrollment_audit (driver_id, event, actor, detail) "
                "VALUES (:d, :e, :a, CAST(:det AS jsonb))",
                {"d": driver_id, "e": event, "a": actor, "det": _dumps(detail)},
                dsn=dsn,
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_audit_failed", error=str(exc))
    _MEM_AUDIT.append(
        {"driver_id": driver_id, "event": event, "actor": actor,
         "detail": detail, "ts": _now().isoformat()}
    )


# --------------------------------------------------------------------------- writes
async def submit(dsn: str, *, driver_id: str, name: str, license_no: str = "",
                 mobile: str = "", vehicle_no: str = "", aadhaar_masked: str = "",
                 emergency_contact: str = "", consent: bool = False,
                 face_images: Optional[List[str]] = None,
                 documents: Optional[List[Any]] = None) -> dict:
    """Create/refresh a PENDING enrolment request. Re-submitting overwrites a
    prior PENDING/REJECTED/REENROLL record (a driver may re-enrol after rejection)."""
    face_images = face_images or []
    documents = documents or []
    now = _now()
    consent_at = now if consent else None
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import execute

            await execute(
                """
                INSERT INTO jnpa.driver_enrollments
                    (driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked,
                     emergency_contact, status, consent, consent_at, face_images,
                     documents, submitted_at, updated_at)
                VALUES
                    (:driver_id, :name, :license_no, :mobile, :vehicle_no, :aadhaar,
                     :emergency, 'PENDING', :consent, :consent_at,
                     CAST(:faces AS jsonb), CAST(:docs AS jsonb), :now, :now)
                ON CONFLICT (driver_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    license_no = EXCLUDED.license_no,
                    mobile = EXCLUDED.mobile,
                    vehicle_no = EXCLUDED.vehicle_no,
                    aadhaar_masked = EXCLUDED.aadhaar_masked,
                    emergency_contact = EXCLUDED.emergency_contact,
                    status = 'PENDING',
                    consent = EXCLUDED.consent,
                    consent_at = EXCLUDED.consent_at,
                    face_images = EXCLUDED.face_images,
                    documents = EXCLUDED.documents,
                    reference_image = NULL,
                    photo_url = NULL,
                    template_dim = NULL,
                    provider = NULL,
                    reviewed_at = NULL,
                    reviewed_by = NULL,
                    rejection_reason = NULL,
                    submitted_at = EXCLUDED.submitted_at,
                    updated_at = EXCLUDED.updated_at
                """,
                {"driver_id": driver_id, "name": name, "license_no": license_no,
                 "mobile": mobile, "vehicle_no": vehicle_no, "aadhaar": aadhaar_masked,
                 "emergency": emergency_contact, "consent": consent,
                 "consent_at": consent_at, "faces": _dumps(face_images),
                 "docs": _dumps(documents), "now": now},
                dsn=dsn,
            )
            await audit(dsn, driver_id, "SUBMITTED", actor=f"driver:{driver_id}",
                        detail={"images": len(face_images), "consent": consent})
            return await get(dsn, driver_id) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_submit_db_failed_using_memory", error=str(exc))
    rec = {
        "driver_id": driver_id, "name": name, "license_no": license_no,
        "mobile": mobile, "vehicle_no": vehicle_no, "aadhaar_masked": aadhaar_masked,
        "emergency_contact": emergency_contact, "status": PENDING, "consent": consent,
        "consent_at": consent_at.isoformat() if consent_at else None,
        "face_images": face_images, "reference_image": None, "photo_url": None,
        "documents": documents, "template_dim": None, "provider": None,
        "submitted_at": now.isoformat(), "reviewed_at": None, "reviewed_by": None,
        "rejection_reason": None, "updated_at": now.isoformat(),
    }
    _MEM[driver_id] = rec
    await audit(dsn, driver_id, "SUBMITTED", actor=f"driver:{driver_id}",
                detail={"images": len(face_images), "consent": consent})
    return _public(rec, include_faces=False)


async def mark_active(dsn: str, driver_id: str, *, actor: str, photo_url: Optional[str],
                      reference_image: Optional[str], template_dim: Optional[int],
                      provider: Optional[str]) -> dict:
    """Approve: move PENDING -> ACTIVE, persist the template metadata + reference
    photo pointer, and clear the pending review frames (keep one canonical frame)."""
    now = _now()
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import execute

            await execute(
                """
                UPDATE jnpa.driver_enrollments SET
                    status = 'ACTIVE',
                    photo_url = :photo_url,
                    reference_image = :ref,
                    template_dim = :dim,
                    provider = :provider,
                    face_images = '[]'::jsonb,
                    reviewed_at = :now,
                    reviewed_by = :actor,
                    rejection_reason = NULL,
                    updated_at = :now
                WHERE driver_id = :driver_id
                """,
                {"photo_url": photo_url, "ref": reference_image, "dim": template_dim,
                 "provider": provider, "now": now, "actor": actor,
                 "driver_id": driver_id},
                dsn=dsn,
            )
            await audit(dsn, driver_id, "APPROVED", actor=actor,
                        detail={"provider": provider, "dim": template_dim,
                                "photo_url": photo_url})
            return await get(dsn, driver_id) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_approve_db_failed_using_memory", error=str(exc))
    rec = _MEM.get(driver_id)
    if rec is not None:
        rec.update(status=ACTIVE, photo_url=photo_url, reference_image=reference_image,
                   template_dim=template_dim, provider=provider, face_images=[],
                   reviewed_at=now.isoformat(), reviewed_by=actor,
                   rejection_reason=None, updated_at=now.isoformat())
    await audit(dsn, driver_id, "APPROVED", actor=actor,
                detail={"provider": provider, "dim": template_dim})
    return _public(rec or {}, include_faces=False)


async def set_status(dsn: str, driver_id: str, status: str, *, actor: str,
                     reason: str = "") -> dict:
    """Reject or request re-enrolment. Keeps the record so the driver can re-submit."""
    event = "REJECTED" if status == REJECTED else "REENROLL_REQUESTED"
    now = _now()
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import execute

            await execute(
                """
                UPDATE jnpa.driver_enrollments SET
                    status = :status,
                    rejection_reason = :reason,
                    reviewed_at = :now,
                    reviewed_by = :actor,
                    updated_at = :now
                WHERE driver_id = :driver_id
                """,
                {"status": status, "reason": reason, "now": now, "actor": actor,
                 "driver_id": driver_id},
                dsn=dsn,
            )
            await audit(dsn, driver_id, event, actor=actor, detail={"reason": reason})
            return await get(dsn, driver_id) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_set_status_db_failed_using_memory", error=str(exc))
    rec = _MEM.get(driver_id)
    if rec is not None:
        rec.update(status=status, rejection_reason=reason, reviewed_at=now.isoformat(),
                   reviewed_by=actor, updated_at=now.isoformat())
    await audit(dsn, driver_id, event, actor=actor, detail={"reason": reason})
    return _public(rec or {}, include_faces=False)


# --------------------------------------------------------------------------- reads
async def get(dsn: str, driver_id: str, *, include_faces: bool = True) -> Optional[dict]:
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import fetch_one

            cols = _SUMMARY_COLS + (", face_images, reference_image, documents"
                                    if include_faces else "")
            row = await fetch_one(
                f"SELECT {cols} FROM jnpa.driver_enrollments WHERE driver_id = :d",
                {"d": driver_id}, dsn=dsn,
            )
            return _row(row, include_faces=include_faces) if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_get_db_failed_using_memory", error=str(exc))
    rec = _MEM.get(driver_id)
    return _public(rec, include_faces=include_faces) if rec else None


async def list_requests(dsn: str, *, status: Optional[str] = None) -> List[dict]:
    """List enrolment requests (summary only — no raw frames) newest-first."""
    if await _backend(dsn) == "db":
        try:
            from jnpa_shared.db import fetch_all

            where = "WHERE status = :s" if status else ""
            rows = await fetch_all(
                f"SELECT {_SUMMARY_COLS}, (face_images->>0) AS thumb "
                f"FROM jnpa.driver_enrollments {where} ORDER BY submitted_at DESC",
                {"s": status} if status else {}, dsn=dsn,
            )
            return [_row(r, include_faces=False) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("enrollment_list_db_failed_using_memory", error=str(exc))
    items = [v for v in _MEM.values() if status is None or v.get("status") == status]
    items.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return [_public(r, include_faces=False) for r in items]


# --------------------------------------------------------------------------- drivers master
async def promote_to_driver(dsn: str, rec: Mapping[str, Any], *, actor: str,
                            photo_url: Optional[str], reference_image: Optional[str],
                            template_dim: Optional[int], provider: Optional[str]) -> None:
    """On approval, upsert the canonical master identity in jnpa.drivers. Existing
    embeddings are NOT clobbered blindly — the identity service guards its template;
    here we refresh the durable profile + the reference-photo pointer + metadata."""
    now = _now()
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        await execute(
            """
            INSERT INTO jnpa.drivers
                (driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked,
                 emergency_contact, status, photo_url, reference_image, template_dim,
                 provider, enrolled_at, approved_by, updated_at)
            VALUES
                (:driver_id, :name, :license_no, :mobile, :vehicle_no, :aadhaar,
                 :emergency, 'ACTIVE', :photo_url, :ref, :dim, :provider, :now, :actor, :now)
            ON CONFLICT (driver_id) DO UPDATE SET
                name = EXCLUDED.name,
                license_no = EXCLUDED.license_no,
                mobile = EXCLUDED.mobile,
                vehicle_no = EXCLUDED.vehicle_no,
                aadhaar_masked = EXCLUDED.aadhaar_masked,
                emergency_contact = EXCLUDED.emergency_contact,
                status = 'ACTIVE',
                photo_url = EXCLUDED.photo_url,
                reference_image = EXCLUDED.reference_image,
                template_dim = EXCLUDED.template_dim,
                provider = EXCLUDED.provider,
                approved_by = EXCLUDED.approved_by,
                updated_at = EXCLUDED.updated_at
            """,
            {"driver_id": rec.get("driver_id"), "name": rec.get("name") or rec.get("driver_id"),
             "license_no": rec.get("license_no"), "mobile": rec.get("mobile"),
             "vehicle_no": rec.get("vehicle_no"), "aadhaar": rec.get("aadhaar_masked"),
             "emergency": rec.get("emergency_contact"), "photo_url": photo_url,
             "ref": reference_image, "dim": template_dim, "provider": provider,
             "now": now, "actor": actor},
            dsn=dsn,
        )
        return
    _MEM_DRIVERS[rec.get("driver_id")] = {
        "driver_id": rec.get("driver_id"), "name": rec.get("name"),
        "license_no": rec.get("license_no"), "mobile": rec.get("mobile"),
        "vehicle_no": rec.get("vehicle_no"), "aadhaar_masked": rec.get("aadhaar_masked"),
        "emergency_contact": rec.get("emergency_contact"), "status": "ACTIVE",
        "photo_url": photo_url, "reference_image": reference_image,
        "template_dim": template_dim, "provider": provider,
        "enrolled_at": now.isoformat(), "approved_by": actor, "updated_at": now.isoformat(),
    }


async def get_driver(dsn: str, driver_id: str) -> Optional[dict]:
    """Read the master driver record (durable identity)."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_one

        row = await fetch_one(
            "SELECT driver_id, name, license_no, mobile, vehicle_no, aadhaar_masked, "
            "emergency_contact, status, photo_url, reference_image, template_dim, "
            "provider, enrolled_at, approved_by, updated_at "
            "FROM jnpa.drivers WHERE driver_id = :d", {"d": driver_id}, dsn=dsn)
        return _iso_row(dict(row)) if row else None
    return _MEM_DRIVERS.get(driver_id)


async def list_active_drivers(dsn: str) -> List[dict]:
    """Active master drivers (for the verification gallery)."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        rows = await fetch_all(
            "SELECT driver_id, name, license_no, photo_url FROM jnpa.drivers "
            "WHERE status = 'ACTIVE' ORDER BY name", dsn=dsn)
        return [dict(r) for r in rows]
    return [{"driver_id": d["driver_id"], "name": d.get("name"),
             "license_no": d.get("license_no"), "photo_url": d.get("photo_url")}
            for d in _MEM_DRIVERS.values() if d.get("status") == "ACTIVE"]


# --------------------------------------------------------------------------- biometric templates (1:N)
async def store_face(dsn: str, driver_id: str, embedding: List[float], *,
                     dim: int, provider: Optional[str], model_version: str = "") -> None:
    """Upsert a driver's biometric template into jnpa.driver_faces (1:N store)."""
    now = _now()
    if await _backend(dsn) == "db":
        from jnpa_shared.db import execute

        await execute(
            """
            INSERT INTO jnpa.driver_faces (driver_id, embedding, dim, provider, model_version, created_at, updated_at)
            VALUES (:d, CAST(:emb AS jsonb), :dim, :provider, :mv, :now, :now)
            ON CONFLICT (driver_id) DO UPDATE SET
                embedding = EXCLUDED.embedding, dim = EXCLUDED.dim,
                provider = EXCLUDED.provider, model_version = EXCLUDED.model_version,
                updated_at = EXCLUDED.updated_at
            """,
            {"d": driver_id, "emb": _dumps(embedding), "dim": dim,
             "provider": provider, "mv": model_version, "now": now}, dsn=dsn)
        return
    _MEM_FACES[driver_id] = {"driver_id": driver_id, "embedding": list(embedding),
                             "dim": dim, "provider": provider, "model_version": model_version}


async def load_faces(dsn: str) -> List[dict]:
    """All stored biometric templates for the 1:N nearest-neighbour search."""
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        rows = await fetch_all(
            "SELECT driver_id, embedding, dim, provider FROM jnpa.driver_faces", dsn=dsn)
        return [{"driver_id": r["driver_id"], "embedding": _loads(r["embedding"]),
                 "dim": r["dim"], "provider": r["provider"]} for r in rows]
    return [dict(v) for v in _MEM_FACES.values()]


# --------------------------------------------------------------------------- verification audit
async def log_verification(dsn: str, *, driver_id: str, decision: str,
                           score: Optional[float], matched: Optional[bool],
                           provider: Optional[str], decision_path: Optional[str],
                           actor: str, purpose: str, reason: Optional[str]) -> None:
    """Append a verification decision to the audit trail. Best-effort: a logging
    failure must never break the verification response."""
    try:
        if await _backend(dsn) == "db":
            from jnpa_shared.db import execute

            await execute(
                "INSERT INTO jnpa.verification_logs "
                "(driver_id, decision, score, matched, provider, decision_path, actor, purpose, reason) "
                "VALUES (:d, :dec, :score, :matched, :provider, :path, :actor, :purpose, :reason)",
                {"d": driver_id, "dec": decision, "score": score, "matched": matched,
                 "provider": provider, "path": decision_path, "actor": actor,
                 "purpose": purpose, "reason": reason}, dsn=dsn)
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("verification_log_failed", error=str(exc))
        return
    _MEM_VLOGS.append({"driver_id": driver_id, "decision": decision, "score": score,
                       "matched": matched, "provider": provider, "decision_path": decision_path,
                       "actor": actor, "purpose": purpose, "reason": reason,
                       "ts": _now().isoformat()})


async def recent_verifications(dsn: str, driver_id: Optional[str] = None,
                               limit: int = 50) -> List[dict]:
    if await _backend(dsn) == "db":
        from jnpa_shared.db import fetch_all

        where = "WHERE driver_id = :d " if driver_id else ""
        rows = await fetch_all(
            f"SELECT driver_id, decision, score, matched, provider, decision_path, "
            f"actor, purpose, reason, ts FROM jnpa.verification_logs {where}"
            f"ORDER BY ts DESC LIMIT :lim",
            {"d": driver_id, "lim": limit} if driver_id else {"lim": limit}, dsn=dsn)
        return [_iso_row(dict(r)) for r in rows]
    items = [v for v in _MEM_VLOGS if not driver_id or v["driver_id"] == driver_id]
    return list(reversed(items))[:limit]


# --------------------------------------------------------------------------- shaping
def _iso(val: Any) -> Any:
    return val.isoformat() if isinstance(val, datetime) else val


def _iso_row(d: dict) -> dict:
    """ISO-format any datetime values in a row dict (for JSON responses)."""
    return {k: _iso(v) for k, v in d.items()}


def _with_browser_photo(d: dict, fallback: Optional[str]) -> dict:
    """Common photo mapping: turn the stored ``photo_url`` (which may be an
    ``s3://`` pointer) into a browser-loadable presigned URL, and mirror it onto
    ``photo``. Applies to every read path (demo + real enrolment) so the UI never
    receives an ``s3://`` URL it cannot fetch. Falls back to a captured frame when
    no object URL exists yet."""
    from . import objectstore

    resolved = objectstore.resolve_photo_url(d.get("photo_url"))
    if resolved is not None:
        d["photo_url"] = resolved
    d["photo"] = resolved or d.get("photo") or fallback
    return d


def _row(row: Mapping[str, Any], *, include_faces: bool) -> dict:
    d = dict(row)
    for k in ("consent_at", "submitted_at", "reviewed_at", "updated_at"):
        if k in d:
            d[k] = _iso(d[k])
    for k in ("face_images", "documents"):
        if k in d:
            d[k] = _loads(d[k])
    # `thumb` (first frame) becomes the list photo when no MinIO url exists yet.
    thumb = d.pop("thumb", None)
    if not include_faces:
        d.pop("reference_image", None)
    return _with_browser_photo(d, thumb)


def _public(rec: dict, *, include_faces: bool) -> dict:
    d = dict(rec)
    if not include_faces:
        d.pop("face_images", None)
        d.pop("reference_image", None)
        d.pop("documents", None)
    fallback = (rec.get("face_images") or [None])[0]
    return _with_browser_photo(d, fallback)


__all__ = [
    "PENDING", "ACTIVE", "REJECTED", "REENROLL",
    "ensure_backend",
    "submit", "mark_active", "set_status", "get", "list_requests", "audit",
    "decode_data_url", "promote_to_driver", "get_driver", "list_active_drivers",
    "log_verification", "recent_verifications", "store_face", "load_faces",
]
