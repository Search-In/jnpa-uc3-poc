"""Terminal Appointment System (TAS) mock.

JNPA terminals book truck gate-in slots through a TAS. UC-III does not own that
system, so this is a stub the what-if scenarios drive: TFC-1 (gate closure)
marks the slots at the closed gate ``RESCHEDULED`` so the dashboard timeline can
show the appointment knock-on, and "Reset to baseline" restores them.

Demo-scale: an in-process slot book keyed by gate. Slots are minted lazily the
first time a gate is queried so a fresh stack always has something to reschedule.
The mock is process-local to the gateway (the only service that talks to a real
TAS in production), exposed to scenarios via /api/tas/* on the gateway router.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# Default slots minted per gate on first touch (15-min cadence over ~3 h).
_DEFAULT_SLOTS_PER_GATE = 12
_SLOT_CADENCE_MIN = 15


@dataclass
class Slot:
    slot_id: str
    gate_id: str
    start: datetime
    status: str = "BOOKED"          # BOOKED | RESCHEDULED | CANCELLED
    rescheduled_to: Optional[str] = None   # gate_id the slot was moved to


@dataclass
class _Book:
    slots: Dict[str, Slot] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_BOOK = _Book()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_gate_slots(gate_id: str) -> List[Slot]:
    """Lazily mint a deterministic set of BOOKED slots for ``gate_id``."""
    existing = [s for s in _BOOK.slots.values() if s.gate_id == gate_id]
    if existing:
        return existing
    base = _now().replace(second=0, microsecond=0)
    minted: List[Slot] = []
    for i in range(_DEFAULT_SLOTS_PER_GATE):
        sid = f"TAS-{gate_id}-{i:02d}"
        slot = Slot(slot_id=sid, gate_id=gate_id,
                    start=base + timedelta(minutes=_SLOT_CADENCE_MIN * i))
        _BOOK.slots[sid] = slot
        minted.append(slot)
    return minted


def list_slots(gate_id: Optional[str] = None) -> List[dict]:
    with _BOOK.lock:
        if gate_id:
            _ensure_gate_slots(gate_id)
        rows = [s for s in _BOOK.slots.values() if not gate_id or s.gate_id == gate_id]
        return [_to_dict(s) for s in sorted(rows, key=lambda s: s.start)]


def reschedule_gate(gate_id: str, *, to_gate: Optional[str] = None) -> List[dict]:
    """Mark every BOOKED slot at ``gate_id`` RESCHEDULED (TFC-1 step 5).

    Idempotent: slots already RESCHEDULED are left as-is. Returns the affected
    slot rows so the scenario can record them in its timeline.
    """
    with _BOOK.lock:
        _ensure_gate_slots(gate_id)
        affected: List[Slot] = []
        for s in _BOOK.slots.values():
            if s.gate_id == gate_id and s.status == "BOOKED":
                s.status = "RESCHEDULED"
                s.rescheduled_to = to_gate
                affected.append(s)
        return [_to_dict(s) for s in affected]


def restore_gate(gate_id: str) -> int:
    """Restore every RESCHEDULED slot at ``gate_id`` to BOOKED (reset). Count."""
    with _BOOK.lock:
        n = 0
        for s in _BOOK.slots.values():
            if s.gate_id == gate_id and s.status == "RESCHEDULED":
                s.status = "BOOKED"
                s.rescheduled_to = None
                n += 1
        return n


def _to_dict(s: Slot) -> dict:
    return {
        "slot_id": s.slot_id,
        "gate_id": s.gate_id,
        "start": s.start.isoformat(),
        "status": s.status,
        "rescheduled_to": s.rescheduled_to,
    }


# ---------------------------------------------------------------------------
# Deferred-arrival windows (cross-twin contract XT-2).
#
# UC-II publishes DeferredArrivalWindow on `jnpa.crosstwin.deferred-arrival`;
# the gateway's deferred-arrival pump validates and applies it here: slots that
# start inside the window flip to RESCHEDULED, and new bookings inside the
# window are capped at slot_cap (checked by the RMS-TAS /book endpoint).
# In-memory like the rest of this mock (the gateway is the only service that
# would talk to a real TAS in production).
# ---------------------------------------------------------------------------
_MAX_WINDOWS = 32
_WINDOWS: List[dict] = []


def apply_deferred_window(win) -> dict:
    """Apply a validated ``DeferredArrivalWindow`` (jnpa_shared.schemas).

    Returns a summary dict {applied_slots, window}. Idempotent per
    correlation_id: re-applying the same window updates it in place rather
    than double-counting.
    """
    start = win.window_start
    end = start + timedelta(minutes=win.window_min)
    with _BOOK.lock:
        if win.gate_id:
            _ensure_gate_slots(win.gate_id)
        affected: List[Slot] = []
        for s in _BOOK.slots.values():
            if win.gate_id and s.gate_id != win.gate_id:
                continue
            if s.status == "BOOKED" and start <= s.start < end:
                s.status = "RESCHEDULED"
                s.rescheduled_to = None
                affected.append(s)
        entry = next((w for w in _WINDOWS
                      if w["correlation_id"] == win.correlation_id), None)
        if entry is None:
            entry = {"correlation_id": win.correlation_id, "booked": 0}
            _WINDOWS.append(entry)
            del _WINDOWS[:-_MAX_WINDOWS]
        entry.update({
            "gate_id": win.gate_id,
            "window_start": start,
            "window_end": end,
            "window_min": win.window_min,
            "slot_cap": win.slot_cap,
            "source": win.source,
            "received_at": _now(),
            "applied_slots": [s.slot_id for s in affected],
        })
        return {"applied_slots": len(affected), "window": _window_dict(entry)}


def deferred_windows() -> List[dict]:
    with _BOOK.lock:
        return [_window_dict(w) for w in _WINDOWS]


def check_booking_allowed(gate_id: str, slot_start: datetime) -> tuple[bool, Optional[dict]]:
    """Booking guard for the deferred-arrival cap.

    Finds the newest window covering (gate_id, slot_start); if its cap is
    exhausted the booking is refused, otherwise the window's booked counter is
    incremented. (True, window|None) = allowed; (False, window) = refused.
    """
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=timezone.utc)
    with _BOOK.lock:
        for w in reversed(_WINDOWS):
            if w.get("gate_id") not in (None, gate_id):
                continue
            if not (w["window_start"] <= slot_start < w["window_end"]):
                continue
            if w["booked"] >= w["slot_cap"]:
                return False, _window_dict(w)
            w["booked"] += 1
            return True, _window_dict(w)
        return True, None


def _window_dict(w: dict) -> dict:
    return {
        "correlation_id": w["correlation_id"],
        "gate_id": w.get("gate_id"),
        "window_start": w["window_start"].isoformat(),
        "window_end": w["window_end"].isoformat(),
        "window_min": w["window_min"],
        "slot_cap": w["slot_cap"],
        "booked": w["booked"],
        "applied_slots": w.get("applied_slots", []),
        "source": w.get("source"),
        "received_at": w["received_at"].isoformat(),
    }


__all__ = ["list_slots", "reschedule_gate", "restore_gate", "Slot",
           "apply_deferred_window", "deferred_windows", "check_booking_allowed"]
