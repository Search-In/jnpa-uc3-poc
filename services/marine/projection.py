"""Marine Projection Layer — the ONLY source of lifecycle information.

Every UC-I module that needs to know where a vessel call has got to reads it from here.
Nothing else may query ``core.vessel_call`` / ``core.vessel_call_event`` for lifecycle
purposes, and nothing else may call ``state_engine.derive_state``.

    state_engine.py   pure rules — ladders, derive_state (no I/O)
          ^
          |
    projection.py     THIS — the single DB read + the single engine call
          ^
          +-- berthing/lifecycle.py   translation to berthing's vocabulary (no DB)
          +-- marine/state_service.py the read API surface
          +-- future: pilotage, port craft, shipping lines, dashboard, timeline, KPI

WHY IT EXISTS
-------------
Before this layer, every consumer wrote the same three things for itself: the events
lookup, the VIA-resolution LATERAL, and the derive_state call. Two modules had already
diverged into near-copies. Six consumers doing that is six places to fix when a milestone
is added, and six chances for two screens to disagree about the same vessel.

A consumer now supplies a KEY (call_id / VIA / VCN) and receives a finished
:class:`CallProjection`. It adds no lifecycle logic of its own — at most it translates the
projection into its own vocabulary, which is presentation, not business rules.

EFFECTIVE TIMESTAMPS
--------------------
The projection exposes the actual time of each milestone (``anchored_at``, ``berthed_at``,
``departed_at`` …) taken from the event ledger. That deliberately includes ``berthed_at``,
which has NO column on core.vessel_call — the schema has ``etb`` but no ``atb``. Consumers
therefore get the berthing actual without a migration, and without anyone inventing a
column.

Read-only. Adds no table, column, migration or endpoint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .manual_pilot import (STATUS_ASSIGNED, STATUS_ONBOARD, STATUS_RELEASED,
                            ManualPilotAssignment, ManualPilotService)
from .manual_craft import STATUS_RELEASED as _CRAFT_RELEASED
from .manual_craft import ManualCraftAssignment, ManualCraftService
from .pilot_milestones import BY_CALL_IDS as _MILESTONES_SQL
from .pilot_milestones import merge_events, synthesize
from .state_engine import (EVENT_ANCHORED, EVENT_ARRIVED, EVENT_BERTH_ALLOTTED,
                           EVENT_BERTHED, EVENT_DEPARTED, EVENT_PILOT_BOARDED,
                           EVENT_SAILED, CallState, derive_state)

#: Manual assignment status -> the pilot_state it projects as. Distinct words from the
#: engine's own Pending/Active/Completed on purpose: an operator-entered state must be
#: readable AS an operator state wherever it surfaces.
_MANUAL_PILOT_STATE = {
    STATUS_ASSIGNED: "Assigned",
    STATUS_ONBOARD: "Onboard",
    STATUS_RELEASED: "Released",
}

log = get_logger("services.marine.projection")

# Identity + planned times. The lifecycle itself is derived, never selected.
_CALL_COLS = ("c.call_id, c.vcn, c.via_no, c.imo_no, c.vessel_name, c.voyage_no, "
              "c.status, c.terminal_id, c.berth_id, c.eta, c.etb, c.etd, c.ata, c.atd, c.atc")

_BY_CALL_IDS = f"SELECT {_CALL_COLS} FROM core.vessel_call c WHERE c.call_id = ANY(:keys)"

_BY_VCNS = f"SELECT {_CALL_COLS} FROM core.vessel_call c WHERE c.vcn = ANY(:keys)"

# A short VIA RECYCLES across years, so via_no is not unique: LATERAL picks the newest
# call per VIA. This tiebreak is stated ONCE, here — it used to be copied into every
# consumer, and it must agree with VesselCallRepository._RESOLVE_BY_VIA.
_BY_VIAS = f"""
SELECT k.via AS _key, {_CALL_COLS}
  FROM (SELECT DISTINCT unnest(CAST(:keys AS text[])) AS via) k
  JOIN LATERAL (
       SELECT * FROM core.vessel_call
        WHERE via_no = k.via
        ORDER BY eta DESC NULLS LAST, call_id DESC
        LIMIT 1) c ON TRUE
"""

_EVENTS = ("SELECT call_id, event_type, event_ts, berth_id "
           "FROM core.vessel_call_event WHERE call_id = ANY(:call_ids)")

#: Milestone -> the projection attribute carrying its actual time.
_ACTUALS: tuple[tuple[str, str], ...] = (
    (EVENT_BERTH_ALLOTTED, "berth_allotted_at"),
    (EVENT_ANCHORED, "anchored_at"),
    (EVENT_PILOT_BOARDED, "pilot_boarded_at"),
    (EVENT_BERTHED, "berthed_at"),
    (EVENT_ARRIVED, "arrived_at"),
    (EVENT_SAILED, "sailed_at"),
    (EVENT_DEPARTED, "departed_at"),
)


@dataclass(frozen=True)
class CallProjection:
    """Business-ready lifecycle view of one vessel call."""
    # --- identity -------------------------------------------------------------
    call_id: int
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_no: Optional[str] = None
    terminal_id: Optional[int] = None
    berth_id: Optional[int] = None
    # --- derived state (state_engine) ----------------------------------------
    status: Optional[str] = None
    arrival_state: str = "Pending"
    berth_state: str = "Pending"
    pilot_state: str = "Pending"
    departure_state: str = "Pending"
    shipping_state: str = "Expected"
    portcraft_state: str = "Idle"
    is_in_port: bool = False
    is_at_berth: bool = False
    latest_event: Optional[str] = None
    latest_event_time: Optional[datetime] = None
    #: Craft committed to this call, DERIVED BY state_engine from the CRAFT_ASSIGNED /
    #: CRAFT_RELEASED ledger milestones — not counted here. Distinct from
    #: `portcraft_state`, which is the engine's verdict on whether the movement REQUIRES
    #: craft: supply versus demand, deliberately not conflated.
    craft_state: str = "Idle"
    #: HOW MANY craft are committed right now.
    #:
    #: This is the one craft fact the ledger cannot answer. An event ledger records that
    #: something HAPPENED; a reached-set has no cardinality, so "three tugs are out" is not
    #: derivable from it. The count therefore comes from the assignment rows. That is not a
    #: second lifecycle calculation — `craft_state` is the engine's and only the engine's;
    #: this is an inventory question the engine was never asked.
    craft_committed: int = 0
    #: Where pilot_state came from: 'imported' when the event ledger supplied it,
    #: 'manual' when an operator assignment did, None when there is no pilot at all.
    #: Additive; consumers that do not know the field are unaffected.
    pilot_source: Optional[str] = None
    # --- effective timestamps -------------------------------------------------
    # planned, from the call row
    eta: Optional[datetime] = None
    etb: Optional[datetime] = None
    etd: Optional[datetime] = None
    # projected onto the call by the import path
    ata: Optional[datetime] = None
    atd: Optional[datetime] = None
    atc: Optional[datetime] = None
    # actual, from the event ledger. `berthed_at` has NO column on core.vessel_call.
    berth_allotted_at: Optional[datetime] = None
    anchored_at: Optional[datetime] = None
    pilot_boarded_at: Optional[datetime] = None
    berthed_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    sailed_at: Optional[datetime] = None
    departed_at: Optional[datetime] = None
    #: Milestones seen, highest rank last.
    events: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _actuals(events: Iterable[Mapping[str, Any]]) -> dict[str, Optional[datetime]]:
    """Earliest timestamp per milestone. Earliest, not latest: a re-emitted message (VESDEP
    appears twice for one call in the corpus) must not move an actual."""
    out: dict[str, Optional[datetime]] = {attr: None for _, attr in _ACTUALS}
    for e in events:
        et = str(e.get("event_type") or "").strip().upper()
        ts = e.get("event_ts")
        if ts is None:
            continue
        for name, attr in _ACTUALS:
            if et == name and (out[attr] is None or ts < out[attr]):
                out[attr] = ts
    return out


def _merge_manual(d: dict[str, Any], actuals: dict[str, Optional[datetime]],
                  manual: Optional[ManualPilotAssignment]) -> Optional[str]:
    """Fold a manual assignment into the engine's verdict. Returns the pilot_source.

    PRECEDENCE. The engine's own verdict is authoritative whenever it has one: a
    pilot_state other than 'Pending' means the event ledger recorded PILOT_BOARDED, which
    can only come from imported VESARR/VESDEP. In that case the manual row is ignored
    outright — imported data wins, and this is the last of the three places that enforce
    it (the other two are the SQL reader and the partial unique index).

    Mutates `d` (the engine's dict) in place; the engine itself is never modified.
    """
    # THE ENGINE'S VERDICT WINS WHENEVER IT HAS ONE.
    #
    # A manual assignment now writes its own rungs into the shared ledger, so after a
    # boarding the engine already says 'Active' — derived from an event this feature
    # created, through exactly the pipeline an imported VESARR boarding uses. Overriding
    # that with the operator vocabulary would need a heuristic to tell our own ledger rows
    # from imported ones, and any such test (timestamp matching was the obvious one) fails
    # the moment an imported milestone lands at the same instant. Precedence must not rest
    # on a coincidence of clocks.
    #
    # So the manual record supplies a state only where the engine HAS none — the gap
    # between assignment and boarding, which no milestone describes. Everything after that
    # is the ledger's to answer, which is what "manual behaves exactly like imported"
    # means. Operator-facing wording is a presentation concern and stays in the UI.
    # `pilot_source` reports where the STATE came from, not whether an operator record
    # happens to exist. Once the ledger has a pilot milestone the engine's verdict stands
    # and the source is the ledger — including for a rung this feature wrote, which is
    # indistinguishable from an imported one by design.
    if d.get("pilot_state") not in (None, "Pending"):
        return "imported"
    if manual is None:
        return None

    d["pilot_state"] = _MANUAL_PILOT_STATE.get(manual.status, d.get("pilot_state"))

    # Craft are engaged from pilot boarding until the vessel is fast — the same rule the
    # engine applies to an IMPORTED boarding (state_engine: `piloted and not berthed`).
    # Applying it to a manual boarding keeps Port Craft consistent between the two
    # sources instead of showing craft demand only for imported movements.
    if manual.status == STATUS_ONBOARD and not d.get("is_at_berth")             and d.get("departure_state") != "Completed":
        d["portcraft_state"] = "Busy"

    # The operator's boarding time is the only actual this call has; the ledger has none.
    # Filled ONLY when empty, so an imported milestone can never be overwritten.
    if manual.boarded_at is not None and actuals.get("pilot_boarded_at") is None:
        actuals["pilot_boarded_at"] = manual.boarded_at
    return "manual"


def project(call: Mapping[str, Any],
            events: Sequence[Mapping[str, Any]] = (),
            manual: Optional[ManualPilotAssignment] = None,
            craft: Sequence[ManualCraftAssignment] = ()) -> CallProjection:
    """Build the projection for one call. Pure — the only place derive_state is called.

    `manual` is the live operator assignment for this call, when one exists and the call
    has no imported pilotage. It is merged AFTER the engine has spoken, never before, so
    the engine's rules are untouched.
    """
    state: CallState = derive_state(call, events)
    d = state.to_dict()
    acts = _actuals(events)
    pilot_source = _merge_manual(d, acts, manual)
    # COUNT only. `craft_state` comes from the engine via `d` below and is never
    # recomputed here — that duplication was removed when the craft milestones entered the
    # shared ledger.
    live_craft = [c for c in (craft or ()) if c.status != _CRAFT_RELEASED]
    return CallProjection(
        call_id=int(call["call_id"]),
        pilot_source=pilot_source,
        craft_committed=len(live_craft),
        vcn=call.get("vcn"), via_no=call.get("via_no"), imo_no=call.get("imo_no"),
        vessel_name=call.get("vessel_name"), voyage_no=call.get("voyage_no"),
        terminal_id=call.get("terminal_id"), berth_id=call.get("berth_id"),
        eta=call.get("eta"), etb=call.get("etb"), etd=call.get("etd"),
        ata=call.get("ata"), atd=call.get("atd"), atc=call.get("atc"),
        events=tuple(sorted({str(e.get("event_type")) for e in events
                             if e.get("event_type")})),
        **acts,
        **d,
    )


class MarineProjection:
    """Lifecycle lookups by every key a consumer might hold. Read-only."""

    def __init__(self, dsn: Optional[str] = None,
                 manual: Optional[ManualPilotService] = None) -> None:
        self._dsn = dsn
        # Injected for tests; defaults to the real service so every existing caller —
        # none of which knows this parameter exists — picks the merge up unchanged.
        self._manual = manual or ManualPilotService(dsn)
        self._craft = ManualCraftService(dsn)

    async def _fetch(self, sql: str, keys: Sequence[Any]) -> list[dict]:
        if not keys:
            return []
        async with get_engine(self._dsn).connect() as conn:
            calls = (await conn.execute(text(sql), {"keys": list(keys)})).mappings().all()
            if not calls:
                return []
            ids = [int(c["call_id"]) for c in calls]
            evs = (await conn.execute(text(_EVENTS),
                                      {"call_ids": ids})).mappings().all()
            pilot_rows = (await conn.execute(text(_MILESTONES_SQL),
                                             {"call_ids": ids})).mappings().all()
        by_call: dict[int, list[dict]] = {}
        for e in evs:
            by_call.setdefault(int(e["call_id"]), []).append(dict(e))
        # ONE batched read for the whole page, mirroring how events are fetched — a
        # per-call query here would turn every list endpoint into an N+1.
        manual = await self._manual.live_by_call_ids(ids)
        craft = await self._craft.live_by_call_ids(ids)

        # Milestones core.pilotage recorded but the ledger never carried (first line, all
        # fast, pilot away, berth cleared). Synthesised read-only; nothing is written.
        by_pilotage: dict[int, list[dict]] = {}
        for e in synthesize(pilot_rows):
            by_pilotage.setdefault(int(e["call_id"]), []).append(e)

        out = []
        for c in calls:
            row = dict(c)
            cid = int(c["call_id"])
            # Ledger first: an imported milestone always wins a same-instant duplicate.
            row["_events"] = merge_events(by_call.get(cid, []), by_pilotage.get(cid, []))
            row["_manual"] = manual.get(cid)
            row["_craft"] = craft.get(cid, [])
            out.append(row)
        return out

    async def by_call_ids(self, call_ids: Sequence[int]) -> dict[int, CallProjection]:
        ids = sorted({int(i) for i in call_ids if i is not None})
        rows = await self._fetch(_BY_CALL_IDS, ids)
        return {int(r["call_id"]): project(r, r["_events"], r.get("_manual"), r.get("_craft") or ()) for r in rows}

    async def by_vcns(self, vcns: Sequence[str]) -> dict[str, CallProjection]:
        keys = sorted({str(v).strip() for v in vcns if v and str(v).strip()})
        rows = await self._fetch(_BY_VCNS, keys)
        return {str(r["vcn"]): project(r, r["_events"], r.get("_manual"), r.get("_craft") or ())
                for r in rows if r.get("vcn")}

    async def by_vias(self, vias: Sequence[str]) -> dict[str, CallProjection]:
        """Newest call per VIA. A VIA that resolves to no call is simply absent."""
        keys = sorted({str(v).strip() for v in vias if v and str(v).strip()})
        rows = await self._fetch(_BY_VIAS, keys)
        return {str(r["_key"]): project(r, r["_events"], r.get("_manual"), r.get("_craft") or ()) for r in rows}

    async def one(self, call_id: int) -> Optional[CallProjection]:
        return (await self.by_call_ids([call_id])).get(int(call_id))


__all__ = ["CallProjection", "MarineProjection", "project"]
