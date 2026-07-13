"""services/congestion_alert.py — automatic corridor-congestion alerting (UC-3 R4/R7).

Turns the traffic forecaster's per-segment congestion probabilities into durable,
driver-facing alerts when they cross a configurable threshold:

    Traffic prediction
        -> detect_congested()          (pure: which segments are congested)
        -> raise_congestion_alerts()   (persist + dispatch)
             -> jnpa.alerts + jnpa.notifications      (durable trail)
             -> WebSocket + WebPush/FCM               (injected transports)

Design (Clean Architecture): this module owns the detection + persistence logic
and receives the transport fan-out as *injected async callables*, so it never
imports the gateway — mirroring how ``services.cargo`` stays free of gateway
internals. It talks to Postgres through the shared ``jnpa_shared.db`` helpers only.

Persistence is best-effort and **deterministically de-duplicated** (one alert per
segment per time-bucket, via a uuid5 id + ``ON CONFLICT DO NOTHING``) so polling
``/api/traffic/predict`` on a timer cannot spam the alert feed — an alert fires
once when a segment first crosses the threshold in a bucket and stays quiet after.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

# Stable namespace for deterministic congestion-alert ids (segment + bucket).
_ALERT_NS = uuid.UUID("6f1d1e2a-0c3b-4a5e-9c7d-2b8a1f0e4c31")

# Async transport callables the caller injects.
BroadcastFn = Callable[[str, Dict[str, Any]], Awaitable[Any]]
DispatchFn = Callable[[str, Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class CongestionAlert:
    """One congested corridor segment above the alert threshold."""

    segment_id: str
    score: float
    severity: str
    route: str
    recommended_action: str
    gate: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        return {
            "type": "TRAFFIC_CONGESTION",
            "segment_id": self.segment_id,
            "route": self.route,
            "gate": self.gate,
            "severity": self.severity,
            "score": self.score,
            "recommended_action": self.recommended_action,
            "source": "congestion-alert-service",
        }


def _severity(score: float) -> str:
    if score >= 0.90:
        return "CRITICAL"
    if score >= 0.80:
        return "HIGH"
    return "MEDIUM"


def _route_label(segment_id: str, meta: Dict[str, Any]) -> str:
    return str(meta.get("route") or f"NH-348 {segment_id}")


def _action(route: str, gate: Optional[str]) -> str:
    where = f"near {gate}" if gate else f"on {route}"
    return f"Heavy congestion detected {where}. Recommended route changed."


def detect_congested(
    predictions: Dict[str, Any],
    threshold: float,
    *,
    segment_meta: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[CongestionAlert]:
    """Pure: the congested segments (score >= threshold), most-congested first.

    ``predictions`` maps ``segment_id -> probability``. ``segment_meta`` optionally
    supplies ``{"route", "gate"}`` per segment so the advisory can name a real gate
    (e.g. "near Gate 2"); absent, a corridor-segment label is used.
    """
    meta = segment_meta or {}
    out: List[CongestionAlert] = []
    for segment_id, raw in (predictions or {}).items():
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        # Skip NaN/inf: ``nan < threshold`` is False, so a NaN would otherwise slip
        # through the threshold gate and raise a bogus alert.
        if not math.isfinite(score) or score < threshold:
            continue
        m = meta.get(segment_id, {})
        route = _route_label(segment_id, m)
        gate = m.get("gate")
        out.append(
            CongestionAlert(
                segment_id=str(segment_id),
                score=round(score, 3),
                severity=_severity(score),
                route=route,
                recommended_action=_action(route, gate),
                gate=gate,
            )
        )
    return sorted(out, key=lambda a: (-a.score, a.segment_id))


def _bucket_key(bucket: Optional[str]) -> str:
    """Time-bucket string for dedup (defaults to the current UTC hour)."""
    if bucket:
        return bucket
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _alert_id(segment_id: str, bucket: str) -> str:
    return str(uuid.uuid5(_ALERT_NS, f"congestion|{segment_id}|{bucket}"))


async def _persist_alert(dsn: str, alert_id: str, alert: CongestionAlert) -> bool:
    """Insert one jnpa.alerts row (dedup by id). Returns True if newly inserted."""
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        """
        INSERT INTO jnpa.alerts (id, kind, severity, gate_id, plate, payload)
        VALUES (CAST(:id AS uuid), :kind, :sev, :gate, NULL, CAST(:p AS jsonb))
        ON CONFLICT (id) DO NOTHING
        RETURNING id
        """,
        {"id": alert_id, "kind": "TRAFFIC_CONGESTION", "sev": alert.severity,
         "gate": alert.gate, "p": json.dumps(alert.payload())},
        dsn=dsn,
    )
    return row is not None


async def _persist_notification(dsn: str, alert_id: str, alert: CongestionAlert,
                                receiver: Optional[str], status: str) -> None:
    from jnpa_shared.db import execute

    await execute(
        """
        INSERT INTO jnpa.notifications (event_id, channel, receiver, message, delivery_status, provider_response)
        VALUES (:e, 'push', :r, :m, :st, CAST(:p AS jsonb))
        """,
        {"e": alert_id, "r": receiver, "m": alert.recommended_action, "st": status,
         "p": json.dumps({"kind": "TRAFFIC_CONGESTION", "segment_id": alert.segment_id,
                          "severity": alert.severity})},
        dsn=dsn,
    )


async def raise_congestion_alerts(
    *,
    predictions: Dict[str, Any],
    threshold: float,
    dsn: Optional[str],
    broadcast: Optional[BroadcastFn] = None,
    dispatch: Optional[DispatchFn] = None,
    device_targets: Optional[Sequence[str]] = None,
    segment_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    bucket: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Detect congestion and, for each newly-congested segment, create an alert.

    For each segment above ``threshold`` (and not already alerted this bucket):
      1. write a ``jnpa.alerts`` row (dedup by deterministic id),
      2. broadcast a ``type=alert`` WS frame (dashboard + foregrounded PWAs),
      3. push a per-driver advisory over ``dispatch`` to each ``device_targets``
         entry (WebPush/FCM), if provided, and
      4. record a ``jnpa.notifications`` delivery row with a real status.

    Returns the list of alerts newly created this call (empty if none crossed the
    threshold, or all were already alerted this bucket). Every step is best-effort;
    a failing transport/DB never raises into the caller.
    """
    detected = detect_congested(predictions, threshold, segment_meta=segment_meta)
    if not detected:
        return []
    key = _bucket_key(bucket)
    created: List[Dict[str, Any]] = []

    for alert in detected:
        alert_id = _alert_id(alert.segment_id, key)
        payload = {"id": alert_id, "kind": "TRAFFIC_CONGESTION",
                   "severity": alert.severity, "payload": alert.payload()}

        # 1) persist alert (dedup) — only continue the fan-out when NEWLY created.
        is_new = True
        if dsn:
            try:
                is_new = await _persist_alert(dsn, alert_id, alert)
            except Exception:  # noqa: BLE001 — best-effort; still fan out on WS
                is_new = True
        if not is_new:
            continue

        # 2) WebSocket broadcast (reaches dashboard + any foregrounded PWA).
        if broadcast is not None:
            try:
                await broadcast("alert", payload)
            except Exception:  # noqa: BLE001
                pass

        # 3) per-driver device push + 4) notification delivery row per target.
        targets = list(device_targets or [])
        if dispatch is not None and targets:
            for device_id in targets:
                advisory = {
                    **alert.payload(),
                    "truck_id": device_id,
                    "title": "Traffic congestion",
                    "body": alert.recommended_action,
                    "category": "traffic",
                    "href": "#/reroute",
                    "alert_id": alert_id,
                }
                status = "FAILED"
                try:
                    res = await dispatch(device_id, advisory)
                    # dispatch may return a DispatchResult-like or truthy value.
                    sent = bool(getattr(res, "ws", False) or getattr(res, "webpush", False)
                                or getattr(res, "fcm", False) or res)
                    status = "SENT" if sent else "FAILED"
                except Exception:  # noqa: BLE001
                    status = "FAILED"
                if dsn:
                    try:
                        await _persist_notification(dsn, alert_id, alert, device_id, status)
                    except Exception:  # noqa: BLE001
                        pass
        elif dsn:
            # Corridor-wide alert with no specific driver bound: record the WS
            # broadcast as the delivery (SENT when a broadcaster was wired).
            try:
                await _persist_notification(
                    dsn, alert_id, alert, None,
                    "SENT" if broadcast is not None else "PENDING",
                )
            except Exception:  # noqa: BLE001
                pass

        created.append({"alert_id": alert_id, **alert.payload()})

    return created


__all__ = ["CongestionAlert", "detect_congested", "raise_congestion_alerts"]
