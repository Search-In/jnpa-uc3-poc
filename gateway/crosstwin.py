"""Cross-twin XT-2 applier: one path for every DeferredArrivalWindow.

Before this module the Kafka pump applied a window straight to the in-memory
``tas_mock`` slot book and did nothing else. Three consequences, all reported
from the live demo:

  * the window vanished on gateway restart (in-memory only, cap 32);
  * there was no way to fire XT-2 without the UC-II stack publishing on Kafka —
    no producer exists anywhere in this repository;
  * no driver was ever told their slot moved, although the whole point of the
    contract is to meter arrivals.

``apply`` is now the single entry point used by BOTH transports (the Kafka pump
in ``gateway.main`` and ``POST /api/tas/deferred-windows``). It applies the
window, persists it, tells the dashboard, and pushes the affected drivers.
Every step after the apply is best-effort: metering must not depend on RDS, the
WS hub, or push being available.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine

from .logging import get_logger

log = get_logger("gateway.crosstwin")

_REPO: Any = None


def _repo(gw) -> Any:
    global _REPO
    if _REPO is None:
        from services.crosstwin import DeferredArrivalRepository

        _REPO = DeferredArrivalRepository(gw.cfg.postgres_dsn)
    return _REPO


def reset_for_tests() -> None:
    global _REPO
    _REPO = None


async def _affected_devices(gw, gate_id: Optional[str], slot_codes: list[str]) -> list[dict]:
    """Drivers holding a TAS booking on one of the rescheduled slots.

    Returns ``[{vehicle_id, driver_id, slot_code}]``. A gate-wide window with no
    named slots still matches every open booking at that gate, which is what the
    metering means operationally.
    """
    dsn = gw.cfg.postgres_dsn
    if not dsn:
        return []
    sql = ("SELECT vehicle_id, driver_id, slot_code FROM core.tas_booking "
           "WHERE status = 'BOOKED'")
    params: dict[str, Any] = {}
    if slot_codes:
        sql += " AND slot_code = ANY(:slots)"
        params["slots"] = slot_codes
    elif gate_id:
        sql += " AND slot_code LIKE :prefix"
        params["prefix"] = f"{gate_id}%"
    else:
        return []
    sql += " LIMIT 200"
    try:
        async with get_engine(dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("crosstwin.affected_lookup_failed", error=str(exc))
        return []


async def apply(gw, win, *, transport: str = "KAFKA") -> dict:
    """Apply one validated ``DeferredArrivalWindow`` end to end.

    Returns ``{applied_slots, window, persisted, notified}`` so the HTTP caller
    and the tests can assert what actually happened rather than inferring it.
    """
    from . import tas_mock

    result = tas_mock.apply_deferred_window(win)
    window = result["window"]

    # Persistence is BEST-EFFORT, like the WS and push legs below. Without this
    # guard an unreachable RDS raised straight out of apply(), so a UC-II
    # metering window that had ALREADY been applied to the slot book was lost:
    # the Kafka pump saw an exception and the HTTP inject route 500'd, even
    # though the meter was in force. The window is re-persisted on the next
    # delivery (correlation_id is the idempotency key), and `persisted: False`
    # tells the caller the durability leg did not complete rather than implying
    # it did.
    try:
        persisted = await _repo(gw).upsert(window, transport=transport)
    except Exception as exc:  # noqa: BLE001 — metering must not depend on RDS
        persisted = False
        log.warning("crosstwin.persist_failed",
                    correlation_id=window.get("correlation_id"),
                    transport=transport, error=str(exc))

    # Dashboard: an addressed-to-nobody frame, i.e. the control-room view. The
    # PWA ignores type=tas, so this leaks nothing to drivers.
    try:
        await gw.ws.broadcast("tas", {
            "type": "deferred_arrival_applied",
            "correlation_id": window.get("correlation_id"),
            "gate_id": window.get("gate_id"),
            "window_start": window.get("window_start"),
            "window_end": window.get("window_end"),
            "slot_cap": window.get("slot_cap"),
            "applied_slots": window.get("applied_slots", []),
            "transport": transport,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("crosstwin.ws_failed", error=str(exc))

    # Drivers whose slot just moved get a real advisory on their own device only.
    notified = 0
    try:
        from . import notifications
        from .routers import push as push_router

        affected = await _affected_devices(gw, window.get("gate_id"),
                                           list(window.get("applied_slots") or []))
        for row in affected:
            device_id = await push_router.resolve_device(
                gw, driver_id=row.get("driver_id") or None,
                vehicle_id=row.get("vehicle_id") or None)
            if not device_id:
                continue
            res = await notifications.dispatch_alert(
                gw, device_id, kind="TAS_RESLOT",
                title="Gate appointment rescheduled",
                body=(f"Arrivals at {window.get('gate_id') or 'the gate'} are being "
                      f"metered for {window.get('window_min')} min. Your slot "
                      f"{row.get('slot_code')} has been rescheduled."),
                category="gate", href="#/trip",
                extra={"correlation_id": window.get("correlation_id"),
                       "slot_code": row.get("slot_code"),
                       "gate_id": window.get("gate_id"),
                       "source": "UC-II"},
            )
            if res is not None:
                notified += 1
    except Exception as exc:  # noqa: BLE001 — never fail metering on a push
        log.warning("crosstwin.notify_failed", error=str(exc))

    log.info("crosstwin.deferred_applied",
             correlation_id=window.get("correlation_id"),
             gate_id=window.get("gate_id"),
             applied_slots=result["applied_slots"],
             transport=transport, persisted=persisted, notified=notified)
    return {**result, "persisted": persisted, "notified": notified,
            "transport": transport}


async def restore(gw) -> int:
    """Replay persisted windows into the in-memory slot book at startup.

    Without this a gateway restart silently dropped every consumed UC-II window,
    so the TAS booking cap stopped applying and the proof surface came back
    empty — the "loaded data disappears" report, cross-twin edition.
    """
    from datetime import datetime

    from . import tas_mock

    rows = await _repo(gw).recent(limit=32)
    restored = 0
    for r in reversed(rows):  # oldest first so newest ends up last in the ring
        try:
            tas_mock.restore_deferred_window({
                "correlation_id": r["correlation_id"],
                "gate_id": r["gate_id"],
                "window_start": datetime.fromisoformat(r["window_start"]),
                "window_end": datetime.fromisoformat(r["window_end"]),
                "window_min": r["window_min"],
                "slot_cap": r["slot_cap"],
                "booked": r["booked"],
                "applied_slots": r["applied_slots"],
                "source": r["source"],
                "received_at": (datetime.fromisoformat(r["received_at"])
                                if r.get("received_at") else None),
            })
            restored += 1
        except Exception as exc:  # noqa: BLE001 — one bad row never blocks boot
            log.warning("crosstwin.restore_row_failed",
                        correlation_id=r.get("correlation_id"), error=str(exc))
    if restored:
        log.info("crosstwin.restored", windows=restored)
    return restored


__all__ = ["apply", "restore", "reset_for_tests"]
