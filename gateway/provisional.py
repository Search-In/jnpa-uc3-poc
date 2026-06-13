"""Provisional-vehicle workflow + alert emission (Sub-Criterion 3).

When every Vahan rung above PROVISIONAL has failed (live primary down, sim down,
nothing in the 12 h cache), the gateway must still let the vehicle through the
gate rather than block port operations — but on a leash:

* a row is written to ``jnpa.vehicle_master`` with ``provisional=true`` and
  ``provisional_until = now() + 24h`` (the cure window), and
* an ``Alert(kind="PROVISIONAL_VEHICLE")`` is raised so the control room knows a
  vehicle was admitted on trust and must be reconciled before the window closes.

Alerts are persisted to ``jnpa.alerts`` (best-effort) and, when a Kafka producer
is available, published to the shared ``alerts`` topic so the anomaly/alert
pipeline and the dashboard see them on the same channel as everything else.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jnpa_shared.db import execute
from jnpa_shared.schemas import Alert

from .logging import get_logger

log = get_logger("gateway.provisional")

ALERT_KIND_PROVISIONAL = "PROVISIONAL_VEHICLE"
ALERT_KIND_ELEVATED = "ELEVATED_SCRUTINY"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def admit_provisional(
    plate: str,
    *,
    dsn: Optional[str] = None,
    window_h: int = 24,
    reason: str = "all_vahan_paths_exhausted",
) -> datetime:
    """Write the provisional ``jnpa.vehicle_master`` row; return provisional_until.

    Idempotent on the plate PK: a repeated admission refreshes the cure window.
    Best-effort against the DB — a Postgres outage logs and re-raises so the
    caller can decide (the router treats a write failure as non-fatal so the
    vehicle is still admitted, just without the durable row).
    """
    until = _utcnow() + timedelta(hours=window_h)
    await execute(
        """
        INSERT INTO jnpa.vehicle_master (plate, provisional, provisional_until, updated_at)
        VALUES (:plate, true, :until, now())
        ON CONFLICT (plate) DO UPDATE SET
            provisional       = true,
            provisional_until = EXCLUDED.provisional_until,
            updated_at        = now()
        """,
        {"plate": plate, "until": until},
        dsn=dsn,
    )
    return until


async def persist_alert(alert: Alert, *, dsn: Optional[str] = None) -> None:
    """Insert an Alert into jnpa.alerts (payload JSON-encoded)."""
    await execute(
        """
        INSERT INTO jnpa.alerts (id, ts, kind, severity, gate_id, plate, payload, ack)
        VALUES (:id, :ts, :kind, :severity, :gate_id, :plate, CAST(:payload AS jsonb), :ack)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": str(alert.id),
            "ts": alert.ts,
            "kind": alert.kind,
            "severity": alert.severity,
            "gate_id": alert.gate_id,
            "plate": alert.plate,
            "payload": json.dumps(alert.payload),
            "ack": alert.ack,
        },
        dsn=dsn,
    )


def build_provisional_alert(
    plate: str,
    provisional_until: datetime,
    *,
    reason: str,
    gate_id: Optional[str] = None,
) -> Alert:
    return Alert(
        kind=ALERT_KIND_PROVISIONAL,
        severity="warning",
        gate_id=gate_id,
        plate=plate,
        payload={
            "reason": reason,
            "provisional_until": provisional_until.isoformat(),
            "cure_window_h": round((provisional_until - _utcnow()).total_seconds() / 3600, 2),
            "decision_path": "PROVISIONAL",
        },
    )


def build_elevated_scrutiny_alert(
    *,
    device_id: Optional[str],
    plate: Optional[str],
    decision_path: str,
    gate_boom_delay_s: int,
    detail: Optional[Dict[str, Any]] = None,
) -> Alert:
    payload: Dict[str, Any] = {
        "decision_path": decision_path,
        "gate_boom_delay_s": gate_boom_delay_s,
        "device_id": device_id,
    }
    if detail:
        payload.update(detail)
    return Alert(
        kind=ALERT_KIND_ELEVATED,
        severity="warning",
        plate=plate,
        payload=payload,
    )


__all__ = [
    "ALERT_KIND_PROVISIONAL",
    "ALERT_KIND_ELEVATED",
    "admit_provisional",
    "persist_alert",
    "build_provisional_alert",
    "build_elevated_scrutiny_alert",
]
