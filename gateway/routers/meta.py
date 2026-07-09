"""/api/meta — platform metadata the dashboard reads as a single source.

    GET /api/assumptions   -> shared/assumptions.json (cross-UC assumptions)
    GET /api/oss-inventory  -> open-source stack inventory (purpose + license)

These are static, read-only reference surfaces (Assumptions & Methodology,
Production Capability, OSS Inventory panels). No auth-sensitive data.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..logging import get_logger
from ..metrics import REQUESTS

log = get_logger("gateway.meta")

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/assumptions")
async def assumptions() -> dict:
    """Serve the cross-UC assumptions single-source (shared/assumptions.json)."""
    try:
        from jnpa_shared.assumptions import load_assumptions
        doc = load_assumptions()
        REQUESTS.labels("meta", "ok").inc()
        return doc
    except FileNotFoundError as exc:
        log.warning("assumptions_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail={"error": "assumptions_unavailable"})


# Open-source inventory (Task 9). Kept here so the dashboard OSS panel and any
# audit tooling read ONE list. Licenses are the upstream project licenses.
OSS_INVENTORY = [
    {"name": "YOLOv8 (Ultralytics)", "purpose": "ANPR licence-plate detection", "license": "AGPL-3.0", "where": "ai/anpr"},
    {"name": "PaddleOCR (PP-OCRv4)", "purpose": "Licence-plate text recognition (OCR)", "license": "Apache-2.0", "where": "ai/anpr"},
    {"name": "ByteTrack", "purpose": "Multi-object tracking for anomaly detection", "license": "MIT", "where": "ai/anomaly"},
    {"name": "GraphSAGE + LSTM", "purpose": "Congestion-onset forecasting (from-scratch impl)", "license": "MIT (project code)", "where": "ai/congestion"},
    {"name": "ArcGIS Maps SDK for JS", "purpose": "Corridor / geofence map visualisation", "license": "Esri EULA (proprietary SDK)", "where": "web"},
    {"name": "Apache Kafka", "purpose": "Event backbone (CloudEvents transport)", "license": "Apache-2.0", "where": "infra"},
    {"name": "FastAPI", "purpose": "Gateway + microservice HTTP framework", "license": "MIT", "where": "gateway / services"},
    {"name": "TimescaleDB / PostgreSQL", "purpose": "Time-series + relational store", "license": "Apache-2.0 / PostgreSQL", "where": "infra"},
    {"name": "Redis", "purpose": "Frame bus + prediction cache", "license": "BSD-3 / RSALv2", "where": "infra"},
    {"name": "Eclipse Mosquitto (MQTT)", "purpose": "Telemetry ingest (RFID / trucks)", "license": "EPL-2.0 / EDL", "where": "infra"},
    {"name": "React + Vite", "purpose": "Operator dashboard + driver PWA", "license": "MIT", "where": "web / mobile-pwa"},
    {"name": "Prometheus + Grafana", "purpose": "Metrics + observability", "license": "Apache-2.0 / AGPL-3.0", "where": "infra"},
]


@router.get("/oss-inventory")
async def oss_inventory() -> dict:
    """Open-source components: name, purpose, license."""
    REQUESTS.labels("meta", "ok").inc()
    return {"components": OSS_INVENTORY, "count": len(OSS_INVENTORY)}
