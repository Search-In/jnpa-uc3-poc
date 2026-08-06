"""Berthing ← marine lifecycle translation. The ONLY place the two vocabularies meet.

``core.berthing_record.status`` is a CHECK-constrained column with its own seven values,
sourced from the terminals' daily PDFs. The marine State Engine speaks a different
vocabulary (``Berth Planned`` / ``Berth Allotted`` / ``At Berth`` / ``Departed``). This
module translates ONE into the OTHER and does nothing else:

  * it derives no state — every verdict comes from ``state_engine.derive_state``;
  * it introduces no new status value — the output is always one of berthing's existing
    seven, so the CHECK constraint holds and no migration is needed;
  * it never writes — the stored column is untouched; the merge happens on READ.

MERGE RULE — advance only, never regress
----------------------------------------
The effective status is the MORE ADVANCED of (stored PDF status, lifecycle-derived
status), ranked by berthing's OWN ``_LIFECYCLE`` ladder. This is not a new rule: the
berthing repository already documents "consecutive daily snapshots / re-imports advance the
lifecycle status (never regress)", and this applies the same rule to a second source.

It matters because the two sources see different things. Only the PDFs report cargo
operations (``CARGO_OPERATION`` / ``COMPLETED``) — no NLP Marine message carries them — so
a naive "lifecycle wins" would silently downgrade a vessel working cargo back to
``BERTHING_STARTED``. Equally, only the PCS stream carries VESDEP, so a report still saying
``BERTHING_STARTED`` is advanced to ``DEPARTED`` once the vessel has actually sailed.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.marine.parsers.pcs_common import CALL_STATUS_BERTH_PLANNED
from services.marine.projection import CallProjection
from services.marine.state_engine import status_rank

#: A call at or beyond this stage has had a berth assigned, even with no milestone yet —
#: BERMAN lodges the berth application and emits NO event, so the stage is the only signal.
#: Ranked through the engine's ladder rather than compared by string, so the two cannot
#: drift apart.
_BERTH_ASSIGNED_FROM = status_rank(CALL_STATUS_BERTH_PLANNED)

#: Berthing's own ladder, lowest first. Mirrors core.berthing_record's CHECK constraint.
#: Imported by the repository so the order is stated once for this module.
LIFECYCLE: tuple[str, ...] = ("EXPECTED", "ARRIVED", "BERTH_ASSIGNED", "BERTHING_STARTED",
                              "CARGO_OPERATION", "COMPLETED", "DEPARTED")

_RANK = {s: i for i, s in enumerate(LIFECYCLE)}


def rank(status: Optional[str]) -> int:
    """Ladder position, or -1 for an unknown/absent value (so it never wins a merge)."""
    if not status:
        return -1
    return _RANK.get(str(status).strip().upper(), -1)


def from_call_state(state: CallProjection) -> Optional[str]:
    """Marine lifecycle -> berthing vocabulary. None when the lifecycle says nothing yet.

    Mapped against the documented business flow:

        CALINF  -> ETA available          -> EXPECTED
        BERMAN  -> berth assigned         -> BERTH_ASSIGNED
        BERALT  -> berth allotted         -> BERTH_ASSIGNED   (berthing has no separate
                                                               'allotted' value; the berth
                                                               is assigned either way)
        VESARR  -> arrived / berthed      -> ARRIVED / BERTHING_STARTED
        VESDEP  -> departed / released    -> DEPARTED

    CARGO_OPERATION and COMPLETED are deliberately unreachable here: no NLP Marine message
    reports cargo work, so claiming them from the lifecycle would be an invention. They
    survive the merge because the stored PDF value outranks whatever the lifecycle derives.
    """
    if state.departure_state == "Completed":
        return "DEPARTED"
    if state.is_at_berth:
        return "BERTHING_STARTED"
    if state.arrival_state in ("Completed", "Anchored"):
        return "ARRIVED"
    if state.berth_state in ("Allotted", "Occupied", "Released"):
        return "BERTH_ASSIGNED"
    # BERMAN emits no event, so its stage is the only evidence a berth was applied for.
    if status_rank(state.status) >= _BERTH_ASSIGNED_FROM:
        return "BERTH_ASSIGNED"
    if state.status:                      # a call exists but has not reached a berth yet
        return "EXPECTED"
    return None


def effective_status(stored: Optional[str],
                     state: Optional[CallProjection]) -> Optional[str]:
    """The more advanced of the stored PDF status and the lifecycle-derived one.

    Returns ``stored`` unchanged when no call matched the report's VIA — a PDF-only row
    must behave exactly as it did before this module existed.
    """
    if state is None:
        return stored
    derived = from_call_state(state)
    if derived is None:
        return stored
    return derived if rank(derived) > rank(stored) else stored


def apply(row: Mapping[str, Any],
          state: Optional[CallProjection]) -> dict[str, Any]:
    """Return the report row with its status advanced by the lifecycle, if applicable.

    The row keeps EVERY key it arrived with and gains none, so the API response shape is
    byte-identical whether or not a call matched.
    """
    out = dict(row)
    out["status"] = effective_status(out.get("status"), state)
    return out
