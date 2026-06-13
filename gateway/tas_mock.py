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


__all__ = ["list_slots", "reschedule_gate", "restore_gate", "Slot"]
