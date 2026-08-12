"""/api/email — UC3 Email Processing.

    GET  /api/email/health                  mailbox posture (NEVER the password)
    POST /api/email/sync                    poll the mailbox into the ledger
    GET  /api/email/messages                list tracked "JNPA…" emails
    GET  /api/email/messages/{id}           one email + attachments + errors
    POST /api/email/messages/{id}/preview   classify + dry-run validate (writes nothing)
    POST /api/email/messages/{id}/import    classify + import into the master table
    GET  /api/email/master-tables           tables this page can currently target

SECURITY
    ``EMAIL_PASSWORD`` is read only by services.email_ingest.config and is never
    placed in a response, a log line, or an error body. Mailbox faults surface as
    :class:`MailboxError`, whose message is authored for end users; the original
    exception is logged server-side and never returned.

Follows the gate-document router conventions: module-level cached service,
``require_uploader`` role gate on every state-changing route, REQUESTS metrics.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from services.email_ingest.classifier import known_master_tables
from services.email_ingest.imap_client import MailboxError
from services.email_ingest.service import EmailProcessingService

from ..auth import auth_enabled
from ..logging import get_logger
from ..metrics import REQUESTS

router = APIRouter(prefix="/api/email", tags=["email"])
log = get_logger("gateway.routers.email_processing")

_API = "email"
_UPLOADER_ROLES = {"CONTROL_ROOM", "CUSTOMS", "ADMIN", "DTCCC_ADMIN"}
_service: Optional[EmailProcessingService] = None


def get_service(request: Request) -> EmailProcessingService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = EmailProcessingService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def require_operator(request: Request) -> str:
    """Reading and processing the admin mailbox is an operator action."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "email_forbidden",
                    "detail": "email processing requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "operator")


def _mailbox_http(exc: MailboxError) -> HTTPException:
    """MailboxError -> HTTP. The message is already user-safe; nothing else leaks."""
    code = 503 if exc.code in ("connect_failed", "not_configured") else 502
    if exc.code == "auth_failed":
        code = 502
    return HTTPException(status_code=code,
                         detail={"error": exc.code, "message": str(exc)})


@router.get("/health", summary="Mailbox connection posture (no credentials)")
async def health(request: Request,
                 svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    require_operator(request)
    try:
        out = svc.health()
    except Exception as exc:  # noqa: BLE001 — health must never 500
        log.warning("email_health_failed", error=str(exc))
        return {"connected": False, "message": "Mailbox status could not be determined."}
    REQUESTS.labels(_API, "ok").inc()
    return out


@router.get("/master-tables", summary="Master tables this page can target")
async def master_tables(request: Request) -> Dict[str, Any]:
    require_operator(request)
    return {"tables": known_master_tables()}


@router.post("/sync", summary="Poll the mailbox for new JNPA-prefixed emails")
async def sync(request: Request,
               svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    require_operator(request)
    try:
        out = await svc.sync()
    except MailboxError as exc:
        REQUESTS.labels(_API, "error").inc()
        log.warning("email_sync_mailbox_error", code=exc.code)
        raise _mailbox_http(exc) from exc
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(_API, "error").inc()
        log.warning("email_sync_failed", error=str(exc))
        raise HTTPException(status_code=500,
                            detail={"error": "sync_failed",
                                    "message": "The mailbox could not be synchronised."}) from exc
    REQUESTS.labels(_API, "ok").inc()
    return out


@router.get("/messages", summary="Tracked emails whose subject starts with JNPA")
async def list_messages(request: Request,
                        status_: Optional[str] = Query(default=None, alias="status"),
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    require_operator(request)
    out = await svc.list_messages(status=status_, limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return out


@router.get("/messages/{email_id}", summary="One email with attachments and errors")
async def get_message(email_id: int, request: Request,
                      svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    require_operator(request)
    row = await svc.get_message(email_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "message": "Email not found."})
    REQUESTS.labels(_API, "ok").inc()
    return row


@router.post("/messages/{email_id}/preview",
             summary="Classify + dry-run validate (writes nothing)")
async def preview(email_id: int, request: Request,
                  svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    actor = require_operator(request)
    return await _process(svc, email_id, actor, commit=False, override=False)


@router.post("/messages/{email_id}/import",
             summary="Import the email's data into the mapped master table")
async def import_email(email_id: int, request: Request,
                       override: bool = Query(
                           default=False,
                           description="Re-import an email already marked PROCESSED."),
                       svc: EmailProcessingService = Depends(get_service)) -> Dict[str, Any]:
    actor = require_operator(request)
    return await _process(svc, email_id, actor, commit=True, override=override)


async def _process(svc: EmailProcessingService, email_id: int, actor: str,
                   *, commit: bool, override: bool) -> Dict[str, Any]:
    try:
        out = await svc.process(email_id, actor=actor, commit=commit, override=override)
    except MailboxError as exc:
        REQUESTS.labels(_API, "error").inc()
        log.warning("email_process_mailbox_error", email_id=email_id, code=exc.code)
        raise _mailbox_http(exc) from exc
    except Exception as exc:  # noqa: BLE001 — never surface a stack trace
        REQUESTS.labels(_API, "error").inc()
        log.warning("email_process_failed", email_id=email_id, error=str(exc))
        raise HTTPException(
            status_code=500,
            detail={"error": "process_failed",
                    "message": "The email could not be processed. The technical detail has "
                               "been logged."}) from exc
    REQUESTS.labels(_API, "ok").inc()
    return out


__all__ = ["router"]
