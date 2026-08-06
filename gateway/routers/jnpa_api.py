"""JNPA Port-Data API integration surface.

Routes (all JSON):
    GET  /api/integrations/jnpa/health     sync overview: mode, per-group
                                           watermarks, last run  (poc_1 card)
    POST /api/integrations/jnpa/sync       manual sync — body {group?, dry_run?}
                                           (CONTROL_ROOM/ADMIN; demo trigger)
    POST /api/integrations/jnpa/replay     re-route UNROUTED records — body
                                           {group}  (after a consumer lands)
    GET  /api/integrations/jnpa/runs       ingest-run audit trail
    GET  /api/integrations/jnpa/records    landed API records (filterable)
    GET  /api/integrations/jnpa/defects    runtime defect log (json | md) —
                                           the report JNPA's 31-Jul notice
                                           requires
    GET  /api/jnpa/files                   stored raw files (PoC-2's source)
    GET  /api/jnpa/files/{sha256}          the bytes, original filename

RBAC: mutating routes require CONTROL_ROOM (or ADMIN); reads are open like
the other module list endpoints.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..auth import CONTROL_ROOM, auth_enabled
from ..metrics import REQUESTS

from services.jnpa_sync import JnpaSyncService
from services.jnpa_sync.routing import ALL_GROUPS, INDEXED_GROUPS, REPORT_GROUPS

router = APIRouter(tags=["jnpa-api"])

_API = "jnpa_api"

_SYNC_ROLES = CONTROL_ROOM

_service: Optional[JnpaSyncService] = None


def get_sync_service(request: Request) -> JnpaSyncService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        from integrations.jnpa_portdata import JnpaPortDataClient

        client = JnpaPortDataClient(
            getattr(cfg, "jnpa_portdata_api_url", "") or None,
            client_key=getattr(cfg, "jnpa_portdata_client_key", "") or None)
        _service = JnpaSyncService(
            getattr(cfg, "postgres_dsn", None) or None,
            client=client,
            store_dir=getattr(cfg, "jnpa_store_dir", None) or None,
            api_mode=getattr(cfg, "jnpa_api_mode", "LIVE"))
    return _service


def reset_service_for_tests() -> None:
    global _service
    _service = None


def require_operator(request: Request) -> str:
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _SYNC_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "sync_forbidden",
                    "detail": "JNPA sync requires CONTROL_ROOM or ADMIN"})
    return getattr(principal, "sub", "operator")


class SyncBody(BaseModel):
    group: Optional[str] = None
    dry_run: bool = False


class ReplayBody(BaseModel):
    group: str


@router.get("/api/integrations/jnpa/health",
            summary="JNPA Port-Data API sync overview")
async def jnpa_health(request: Request,
                      svc: JnpaSyncService = Depends(get_sync_service)
                      ) -> Dict[str, Any]:
    REQUESTS.labels(_API, "ok").inc()
    return await svc.health()


@router.post("/api/integrations/jnpa/sync",
             summary="Trigger a sync (one group, or all)")
async def jnpa_sync(request: Request, body: SyncBody,
                    svc: JnpaSyncService = Depends(get_sync_service)
                    ) -> Dict[str, Any]:
    require_operator(request)
    if body.group is not None and body.group not in ALL_GROUPS:
        REQUESTS.labels(_API, "error").inc()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_group", "detail": body.group,
                    "available": list(ALL_GROUPS)})
    try:
        if body.group is None:
            result = await svc.sync_all(trigger="MANUAL", dry_run=body.dry_run)
        elif body.group in REPORT_GROUPS:
            result = await svc.sync_reports(body.group, trigger="MANUAL",
                                            dry_run=body.dry_run)
        elif body.group in INDEXED_GROUPS:
            result = await svc.sync_group(body.group, trigger="MANUAL",
                                          dry_run=body.dry_run)
        else:  # static
            result = {"status": "SKIPPED_STATIC",
                      "reason": "bathymetry is dump-sourced (static delivery)"}
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502
        REQUESTS.labels(_API, "error").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "sync_failed", "detail": str(exc)}) from exc
    REQUESTS.labels(_API, "ok").inc()
    return {"trigger": "MANUAL", "dry_run": body.dry_run, "result": result}


@router.post("/api/integrations/jnpa/replay",
             summary="Re-route UNROUTED records from the raw store")
async def jnpa_replay(request: Request, body: ReplayBody,
                      svc: JnpaSyncService = Depends(get_sync_service)
                      ) -> Dict[str, Any]:
    require_operator(request)
    if body.group not in ALL_GROUPS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_group", "detail": body.group})
    REQUESTS.labels(_API, "ok").inc()
    return await svc.replay_unrouted(body.group)


@router.get("/api/integrations/jnpa/runs",
            summary="Sync-run audit trail")
async def jnpa_runs(request: Request, limit: int = 50,
                    group: Optional[str] = None,
                    svc: JnpaSyncService = Depends(get_sync_service)
                    ) -> Dict[str, Any]:
    runs = await svc._repo.list_runs(limit=min(max(limit, 1), 500),
                                     group=group)
    REQUESTS.labels(_API, "ok").inc()
    return {"items": runs, "count": len(runs)}


@router.get("/api/integrations/jnpa/records",
            summary="Landed API records")
async def jnpa_records(request: Request, group: Optional[str] = None,
                       routed_status: Optional[str] = None, limit: int = 100,
                       svc: JnpaSyncService = Depends(get_sync_service)
                       ) -> Dict[str, Any]:
    records = await svc._repo.list_records(
        group=group, routed_status=routed_status,
        limit=min(max(limit, 1), 1000))
    REQUESTS.labels(_API, "ok").inc()
    return {"items": records, "count": len(records)}


@router.get("/api/integrations/jnpa/report-snapshots",
            summary="Raw report-group snapshots (berthing/daily JSON, verbatim)")
async def jnpa_report_snapshots(request: Request,
                                group: Optional[str] = None,
                                limit: int = 100,
                                svc: JnpaSyncService = Depends(get_sync_service)
                                ) -> Dict[str, Any]:
    """The land-raw half of the report pipeline: exactly what the API answered
    per (group, reportDate, terminal), incl. sections the mappers do not yet
    map (portTotals, discharge/load moves) and each bucket's mapped_status."""
    snapshots = await svc._repo.list_report_snapshots(
        group=group, limit=min(max(limit, 1), 500))
    REQUESTS.labels(_API, "ok").inc()
    return {"items": snapshots, "count": len(snapshots)}


@router.get("/api/integrations/jnpa/defects",
            summary="Runtime API defect log (json or md)")
async def jnpa_defects(request: Request, format: str = "json",
                       limit: int = 200, severity: Optional[str] = None,
                       svc: JnpaSyncService = Depends(get_sync_service)):
    defects = await svc._repo.list_defects(limit=min(max(limit, 1), 1000),
                                           severity=severity)
    REQUESTS.labels(_API, "ok").inc()
    if format != "md":
        return {"items": defects, "count": len(defects)}
    lines = ["# JNPA Port-Data API — runtime defect observations", "",
             "| Observed (UTC) | Code | Severity | Endpoint | Detail |",
             "|---|---|---|---|---|"]
    for d in defects:
        detail = str(d.get("description") or "").replace("|", "\\|")
        lines.append(
            f"| {d.get('observed_at')} | {d.get('defect_code')} "
            f"| {d.get('severity')} | {d.get('endpoint') or ''} | {detail} |")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@router.get("/api/jnpa/files", summary="Stored raw API files")
async def jnpa_files(request: Request, group: Optional[str] = None,
                     limit: int = 200,
                     svc: JnpaSyncService = Depends(get_sync_service)
                     ) -> Dict[str, Any]:
    records = await svc._repo.list_records(group=group,
                                           limit=min(max(limit, 1), 1000))
    items = [
        {"sha256": r.get("checksum_sha256"),
         "filename": (r.get("stored_path") or "").rpartition("__")[2] or None,
         "group": r.get("group_slug"),
         "message_type": r.get("message_type"),
         "published_at": r.get("published_at"),
         "size_bytes": r.get("size_bytes"),
         "routed_status": r.get("routed_status")}
        for r in records if r.get("stored_path")
    ]
    REQUESTS.labels(_API, "ok").inc()
    return {"items": items, "count": len(items)}


@router.get("/api/jnpa/files/{sha256}",
            summary="Download a stored raw API file by content hash")
async def jnpa_file_bytes(request: Request, sha256: str,
                          svc: JnpaSyncService = Depends(get_sync_service)):
    found = svc._store.open_bytes(sha256.lower())
    if found is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "file_not_found",
                                    "detail": sha256})
    content, filename = found
    REQUESTS.labels(_API, "ok").inc()
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "ETag": f'"{sha256.lower()}"'})
