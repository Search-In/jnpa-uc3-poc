"""/api/pdp — Port Data Platform adapter (Feature 12).

A thin, auditable read surface over the Port Data Platform (PDP). Every lookup
goes through the shared integration seam (:mod:`gateway.integrations`) so the
LIVE-vs-MOCK posture is explicit and each call is logged to
core.integration_lookup with its source + latency — never a silent hardcode.

  * If ``PDP_BASE_URL`` is configured the adapter performs a REAL HTTP call.
  * Otherwise a deterministic MOCK payload (built by the module-level
    ``_mock_*`` builders below) is returned, tagged ``source="MOCK"``.

    GET /api/pdp/vehicle/{plate}  -> vehicle registry / permit lookup
    GET /api/pdp/event/{ref}      -> a single port event by ref
    GET /api/pdp/traffic          -> live traffic segments / jam factors
    GET /api/pdp/health           -> configured / mode flag
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import integrations
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.pdp")

router = APIRouter(prefix="/api/pdp", tags=["pdp"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------- mock builders
def _mock_vehicle(plate: str) -> Dict[str, Any]:
    """Deterministic PDP vehicle/permit record keyed off the plate."""
    return {
        "plate": plate,
        "owner": "Bhagwati Logistics Pvt Ltd",
        "vehicle_class": "HCV",
        "permit_valid": True,
        "permit_no": f"MH-PMT-{abs(hash(plate)) % 900000 + 100000}",
        "last_seen_gate": "G-NSICT",
        "last_seen_at": _now_iso(),
        "fastag_status": "ACTIVE",
    }


def _mock_event(ref: str) -> Dict[str, Any]:
    """Deterministic PDP port event keyed off the ref."""
    return {
        "ref": ref,
        "event_type": "GATE_IN",
        "ts": _now_iso(),
        "gate_id": "G-NSICT",
        "plate": f"MH04AB{abs(hash(ref)) % 9000 + 1000}",
        "lane": "IN-2",
    }


def _mock_traffic() -> Dict[str, Any]:
    """Deterministic PDP traffic snapshot (a few fixed segments)."""
    return {
        "ts": _now_iso(),
        "segments": [
            {"segment_id": "SEG-Y-JUNCTION", "jam_factor": 0.8, "speed_kmh": 14},
            {"segment_id": "SEG-NH348-APPROACH", "jam_factor": 0.4, "speed_kmh": 38},
            {"segment_id": "SEG-NSICT-GATE", "jam_factor": 0.6, "speed_kmh": 22},
            {"segment_id": "SEG-DPWORLD-GATE", "jam_factor": 0.2, "speed_kmh": 46},
        ],
    }


# --------------------------------------------------------------------- routes
@router.get("/vehicle/{plate}")
async def pdp_vehicle(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """Vehicle / permit lookup from the Port Data Platform."""
    result = await integrations.call(
        system="PDP", op="vehicle", ref=plate,
        request={"plate": plate},
        live_path=f"/vehicle/{plate}",
        mock_fn=lambda: _mock_vehicle(plate),
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("pdp", "ok").inc()
    return {"source": result["source"], **result["data"]}


@router.get("/event/{ref}")
async def pdp_event(ref: str, state: GatewayState = Depends(get_state)) -> dict:
    """Single port event by ref."""
    result = await integrations.call(
        system="PDP", op="event", ref=ref,
        request={"ref": ref},
        live_path=f"/event/{ref}",
        mock_fn=lambda: _mock_event(ref),
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("pdp", "ok").inc()
    return {"source": result["source"], **result["data"]}


@router.get("/traffic")
async def pdp_traffic(state: GatewayState = Depends(get_state)) -> dict:
    """Live traffic segments / jam factors around the port."""
    result = await integrations.call(
        system="PDP", op="traffic", ref=None,
        request={},
        live_path="/traffic",
        mock_fn=_mock_traffic,
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("pdp", "ok").inc()
    return {"source": result["source"], **result["data"]}


@router.get("/health")
async def pdp_health() -> dict:
    """LIVE-vs-MOCK posture for the PDP dependency."""
    return integrations.health("PDP")
