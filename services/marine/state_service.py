"""UC-I Marine business-state read service — the ONLY consumer-facing state surface.

Orchestration only. Lifecycle comes from :class:`services.marine.projection
.MarineProjection` — this module neither queries the event ledger nor calls the state
engine. It contains NO lifecycle logic: no event ordering, no status ladder, no "in port"
predicate. If a rule appears here it is a bug — it belongs in the engine, and the read that
feeds it belongs in the projection.

Purely additive:
  * adds no table, column, migration or index;
  * modifies no existing repository, response model or endpoint;
  * owns its own read SQL so ``VesselCallRepository`` is untouched.

Deliberately read-only — nothing here writes.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from services.shipping_lines import vessel_progress as vp

from .projection import CallProjection, MarineProjection
from .state_engine import in_port_sql

log = get_logger("services.marine.state_service")

# Only what the engine reads. Kept narrow so this cannot become a second call projection.
_CALL_COLS = "c.call_id, c.vcn, c.via_no, c.imo_no, c.vessel_name, c.status, c.berth_id"

_ONE_CALL = f"SELECT {_CALL_COLS} FROM core.vessel_call c WHERE c.call_id = :call_id"

# Calls that have been allotted a berth. Occupancy is derived by the ENGINE from each
# call's milestones — this query only supplies the rows, never a verdict.
_BERTH_CALLS = f"""
SELECT {_CALL_COLS},
       (SELECT b.code FROM core.ref_berth b WHERE b.berth_id = c.berth_id) AS berth_code
  FROM core.vessel_call c
 WHERE c.berth_id IS NOT NULL
   AND ({in_port_sql('c')} OR c.atd IS NULL)
 ORDER BY c.berth_id, c.call_id
"""

_ALL_BERTHS = ("SELECT berth_id, code, terminal_id FROM core.ref_berth "
               "ORDER BY code")

# Calls that could still require craft: anything not yet departed. SQL supplies the
# CANDIDATES only — whether each one actually engages craft is the engine's verdict
# (`portcraft_state`), never a predicate written here.
_ACTIVE_CALLS = """
SELECT c.call_id
  FROM core.vessel_call c
 WHERE c.atd IS NULL
   AND (c.ata IS NOT NULL OR c.eta IS NOT NULL)
 ORDER BY c.call_id
"""

# The fleet register. Static particulars from Details_of_Port_Crafts.pdf — it holds no
# operational state and no link to a call, which is why this service reports DEMAND
# against CAPACITY and never claims a specific craft is on a specific job.
_FLEET_BY_TYPE = ("SELECT craft_type, count(*) AS n FROM core.port_craft "
                  "GROUP BY craft_type ORDER BY n DESC, craft_type")

# ---------------------------------------------------------------- KPI roster aggregates
# ROSTER facts, not lifecycle. How many DISTINCT pilots or craft are engaged right now is a
# question about the resource pool, which no CallProjection can answer: a projection
# describes one CALL, and one pilot may hold several movements while one movement may hold
# several craft. The lifecycle rules themselves are still never re-derived here — these
# predicates are the same ones the readers use (manual_pilot._LIVE, manual_craft._LIVE and
# the open-movement test in the pilot register), stated once for aggregation.
_BUSY_PILOTS = """
SELECT count(DISTINCT id) AS n FROM (
    SELECT COALESCE(p.pilot_code, p.extras->>'pilot_name') AS id
      FROM core.pilotage p
     WHERE p.pilot_boarded_at IS NOT NULL AND p.pilot_disembarked_at IS NULL
       AND COALESCE(p.pilot_code, p.extras->>'pilot_name') IS NOT NULL
    UNION
    SELECT m.pilot_code AS id
      FROM core.manual_pilot_assignment m
     WHERE m.active AND m.status <> 'Released'
) x
"""

# Everyone the register knows, from either source — the denominator for utilisation.
_KNOWN_PILOTS = """
SELECT count(DISTINCT id) AS n FROM (
    SELECT COALESCE(p.pilot_code, p.extras->>'pilot_name') AS id
      FROM core.pilotage p
     WHERE COALESCE(p.pilot_code, p.extras->>'pilot_name') IS NOT NULL
    UNION
    SELECT m.pilot_code AS id FROM core.manual_pilot_assignment m
) x
"""

_BUSY_CRAFT = ("SELECT count(DISTINCT craft_id) AS n FROM core.manual_craft_assignment "
               "WHERE active AND status <> 'Released'")

_FLEET_TOTAL = "SELECT count(*) AS n FROM core.port_craft"

# Departures recorded today. History by definition, and the only KPI here that reaches
# outside the active set — which is why it is scoped by date rather than by _ACTIVE_CALLS.
_COMPLETED_TODAY = ("SELECT count(*) AS n FROM core.vessel_call "
                    "WHERE atd IS NOT NULL AND atd >= date_trunc('day', now())")

#: The three demand phases. Names of the buckets port_craft_demand ALREADY sorts into —
#: reported so a consumer can see which one a row landed in without inferring it from the
#: array it arrived in. Not a new classification.
_PHASE_INBOUND = "Inbound"
_PHASE_ALONGSIDE = "Alongside"
_PHASE_OUTBOUND = "Outbound"


def _craft_movement(p: Any, phase: str) -> dict[str, Any]:
    """One demand row: the call requiring craft, described by its OWN lifecycle.

    Every value is copied verbatim off the CallProjection already resolved for this call —
    nothing is derived, recomputed or inferred here. The lifecycle fields exist so an
    operator can see WHY a call counts toward demand ('At Berth', craft 'Busy') rather than
    only that it does.

    Deliberately absent: requires_tug / requires_pilot / requires_launch and any craft
    identity. Nothing in core.vessel_call, core.vessel_call_event or core.port_craft names
    a craft on a job, so those would be invented. This row says which VESSEL needs craft,
    never which craft serves it.
    """
    return {
        # --- identity (unchanged shape) ---
        "call_id": p.call_id,
        "vcn": p.vcn,
        "via_no": p.via_no,
        "vessel_name": p.vessel_name,
        "berth_id": p.berth_id,
        "latest_event": p.latest_event,
        # --- ADDITIVE: the lifecycle that put this call in this phase ---
        "imo_no": p.imo_no,
        "status": p.status,
        "arrival_state": p.arrival_state,
        "pilot_state": p.pilot_state,
        "berth_state": p.berth_state,
        "departure_state": p.departure_state,
        "shipping_state": p.shipping_state,
        "portcraft_state": p.portcraft_state,
        "latest_event_time": p.latest_event_time,
        "movement_phase": phase,
    }

# Distinct (line, vessel_visit) pairs on the advance lists. Supplies the KEYS only —
# every lifecycle verdict comes from the projection.
#
# The optional filter is CAST for the same reason the berthing one is: asyncpg types every
# parameter at PREPARE, before any value is bound, and a bare parameter in an IS NULL test
# gives PostgreSQL nothing to infer from. Reproduced against the live database as
# `AmbiguousParameterError` (SQLSTATE 42P08) with line=None AND line='MAERSK' — the failure
# precedes binding, which is why this endpoint returned HTTP 500 unconditionally. CAST
# supplies the type; the predicate's meaning is unchanged (NULL line = all lines).
_SL_VISITS = """
SELECT a.line_code AS shipping_line, a.vessel_visit, a.voyage,
       count(*) AS containers
  FROM core.advance_list_container a
 WHERE a.vessel_visit IS NOT NULL AND a.vessel_visit <> ''
   AND (CAST(:line AS text) IS NULL OR a.line_code = CAST(:line AS text))
 GROUP BY a.line_code, a.vessel_visit, a.voyage
 ORDER BY a.line_code, a.vessel_visit
 LIMIT :limit
"""

# Berthing-report rows reconciled against the PCS call spine.
#
# The two are INDEPENDENT sources describing the same physical call: berthing_record comes
# from the terminals' daily PDFs, vessel_call from the PCS message stream. They are joined
# on the VIA, which both carry (doc 01 §1.6 verified 46 VIAs present in both the outbound
# journal and the berthing sheets).
#
# LATERAL, not a plain LEFT JOIN: a short VIA RECYCLES across years, so `via_no` is not
# unique and a naive join would fan a single report row into several. The ordering here is
# the same tiebreak VesselCallRepository._RESOLVE_BY_VIA already uses — newest call wins —
# so the two never disagree about which call a VIA means.
#
# EXACT match only. Some terminals emit a composite VIA ('CGKS0504' = 3-char vessel code +
# VIA); those deliberately do NOT join rather than be silently truncated to force a match.
# An unmatched row is reported with call_id NULL, never dropped.
#
# THE OPTIONAL FILTER IS CAST. asyncpg uses the extended query protocol, so every parameter
# must be typed at PREPARE, before any value is bound. A bare `:terminal IS NULL` gives
# PostgreSQL no type to infer, so the statement was rejected with
# `AmbiguousParameterError: could not determine data type of parameter $1` on EVERY call —
# with and without a terminal, since the failure happens before values matter. That is why
# this endpoint returned HTTP 500 unconditionally. CAST supplies the type; the predicate's
# logic is unchanged (NULL terminal still means "all terminals").
_BERTHING_RECONCILE = """
SELECT b.id            AS record_id,
       b.terminal, b.vessel_name, b.voyage_number, b.berth_number,
       b.status        AS report_status,
       b.eta, b.ata, b.berthing_time, b.departure_time,
       c.call_id, c.vcn, c.via_no, c.status, c.berth_id
  FROM core.berthing_record b
  LEFT JOIN LATERAL (
       SELECT call_id, vcn, via_no, status, berth_id
         FROM core.vessel_call
        WHERE via_no = b.voyage_number
        ORDER BY eta DESC NULLS LAST, call_id DESC
        LIMIT 1) c ON TRUE
 WHERE (CAST(:terminal AS text) IS NULL OR b.terminal = CAST(:terminal AS text))
 ORDER BY b.id
 LIMIT :limit OFFSET :offset
"""


class MarineStateService:
    def __init__(self, dsn: Optional[str] = None,
                 projection: Optional[MarineProjection] = None) -> None:
        self._dsn = dsn
        # Lifecycle comes from the shared projection layer; this service derives nothing.
        self._projection = projection or MarineProjection(dsn)

    # ------------------------------------------------------------- module 1: timeline
    async def call_state(self, call_id: int) -> Optional[dict[str, Any]]:
        """Business state of ONE call — the Vessel Timeline surface.

        Returns None when the call does not exist, so the router can 404 rather than
        inventing an 'unknown' state for a call that was never there.
        """
        p: Optional[CallProjection] = await self._projection.one(call_id)
        if p is None:
            return None
        d = p.to_dict()
        d["event_count"] = len(p.events)
        return d

    # ------------------------------------------------------------- module 7: operational KPIs
    async def kpis(self) -> dict[str, Any]:
        """Operational KPIs, derived from the projection and nothing else.

        WHY THIS IS NOT /api/marine/calls/stats
        ---------------------------------------
        That endpoint reports FACTUAL aggregates over stored columns — how many calls
        exist, how many have an ATA, average turnaround. Those are database facts and are
        correct as they are; it is deliberately left untouched.

        This endpoint reports OPERATIONAL state: who is working, what needs a pilot, which
        movements have craft committed. Every one of those is a lifecycle question, so
        every one is read off a CallProjection. No status is inferred here and no rule is
        restated — the counters below are tallies of the engine's own verdicts.

        SCOPE. Active calls only (`_ACTIVE_CALLS`: not departed, and expected or arrived).
        A KPI about current operations must not be diluted by a year of finished calls.
        `completed_today` is the single deliberate exception and says so.
        """
        async with get_engine(self._dsn).connect() as conn:
            ids = [int(r["call_id"]) for r in
                   (await conn.execute(text(_ACTIVE_CALLS))).mappings().all()]
            busy_pilots = int((await conn.execute(text(_BUSY_PILOTS))).scalar() or 0)
            known_pilots = int((await conn.execute(text(_KNOWN_PILOTS))).scalar() or 0)
            busy_craft = int((await conn.execute(text(_BUSY_CRAFT))).scalar() or 0)
            fleet_total = int((await conn.execute(text(_FLEET_TOTAL))).scalar() or 0)
            completed_today = int((await conn.execute(text(_COMPLETED_TODAY))).scalar() or 0)

        states = await self._projection.by_call_ids(ids)

        # Every tally below reads a projection field. None re-derives one.
        pilot_pending = pilot_engaged = pilot_done = 0
        craft_demand = craft_committed_calls = 0
        awaiting_berthing = at_berth = preparing_departure = sailing = 0
        for p in states.values():
            if p.pilot_state == "Pending":
                pilot_pending += 1
            elif p.pilot_state in ("Active", "Onboard", "Assigned"):
                pilot_engaged += 1
            elif p.pilot_state in ("Completed", "Released"):
                pilot_done += 1

            if p.portcraft_state == "Busy":
                craft_demand += 1
            if p.craft_state != "Idle":
                craft_committed_calls += 1

            # The same three phases port_craft_demand sorts into, counted rather than
            # listed. Precedence is identical, so the two can never disagree.
            if p.departure_state == "Sailing":
                sailing += 1
                preparing_departure += 1
            elif p.is_at_berth:
                at_berth += 1
            elif p.pilot_state in ("Active", "Onboard"):
                awaiting_berthing += 1

        def pct(n: int, d: int) -> float:
            return round(100.0 * n / d, 1) if d else 0.0

        return {
            "scope": {"active_calls": len(ids), "basis": "projection"},
            "pilot": {
                "busy": busy_pilots,
                "available": max(0, known_pilots - busy_pilots),
                "known": known_pilots,
                "utilisation_pct": pct(busy_pilots, known_pilots),
                # Calls whose pilot job is not finished — pilotage still required.
                "demand": pilot_pending + pilot_engaged,
                "waiting_assignment": pilot_pending,
                "under_pilotage": pilot_engaged,
                "completed": pilot_done,
            },
            "craft": {
                "busy": busy_craft,
                "available": max(0, fleet_total - busy_craft),
                "fleet_total": fleet_total,
                "utilisation_pct": pct(busy_craft, fleet_total),
                # The ENGINE's verdict that a movement needs craft.
                "demand": craft_demand,
                "committed_calls": craft_committed_calls,
                # Needs craft, has none committed — the number an operator chases.
                "waiting_assignment": max(0, craft_demand - craft_committed_calls),
                # Busy by the engine's verdict but in no reportable phase — the gap
                # between this endpoint's `demand` and the Port Craft board's total.
                "demand_unphased": max(
                    0, craft_demand - (awaiting_berthing + at_berth + preparing_departure)),
            },
            "operations": {
                # The count the Port Craft board actually LISTS, not the raw Busy tally.
                # `port_craft_demand` drops a Busy call that lands in no reportable phase
                # (Busy but neither sailing, at berth, nor under pilotage), so reporting
                # the raw figure here would put two different numbers for the same idea on
                # two screens. `craft.demand` above keeps the engine's unfiltered verdict
                # and `craft.demand_unphased` accounts for the difference, so nothing is
                # silently lost.
                "marine_support_required": awaiting_berthing + at_berth + preparing_departure,
                "awaiting_berthing": awaiting_berthing,
                "at_berth": at_berth,
                "under_pilotage": pilot_engaged,
                "preparing_departure": preparing_departure,
                "sailing": sailing,
                "completed_today": completed_today,
            },
        }

    # ------------------------------------------------------------- module 3: berthing
    async def berth_occupancy(self) -> dict[str, Any]:
        """Berth occupancy derived from the vessel lifecycle: allotted -> occupied ->
        released.

        Every berth in ``core.ref_berth`` is reported, including the ones with no call
        against them — a berth absent from the list would read as "no such berth" rather
        than "free". The occupied/allotted verdict comes from the ENGINE
        (``is_at_berth`` / ``berth_state``), never from SQL, so the rule lives in one place.
        """
        async with get_engine(self._dsn).connect() as conn:
            berths = (await conn.execute(text(_ALL_BERTHS))).mappings().all()
            calls = (await conn.execute(text(_BERTH_CALLS))).mappings().all()
        states = await self._projection.by_call_ids([int(c["call_id"]) for c in calls])

        by_berth: dict[int, list[dict[str, Any]]] = {}
        for c in calls:
            st = states[int(c["call_id"])]
            by_berth.setdefault(int(c["berth_id"]), []).append({
                "call_id": int(c["call_id"]),
                "vcn": c["vcn"],
                "via_no": c["via_no"],
                "vessel_name": c["vessel_name"],
                "berth_state": st.berth_state,
                "is_at_berth": st.is_at_berth,
                "latest_event": st.latest_event,
            })

        rows: list[dict[str, Any]] = []
        occupied = allotted = 0
        for b in berths:
            bid = int(b["berth_id"])
            here = by_berth.get(bid, [])
            at_berth = [x for x in here if x["is_at_berth"]]
            waiting = [x for x in here if not x["is_at_berth"]
                       and x["berth_state"] == "Allotted"]
            if at_berth:
                state = "Occupied"
                occupied += 1
            elif waiting:
                state = "Allotted"
                allotted += 1
            else:
                state = "Free"
            rows.append({
                "berth_id": bid,
                "code": b["code"],
                "terminal_id": b["terminal_id"],
                "state": state,
                # The call currently alongside, when there is one.
                "occupied_by": (at_berth[0] if at_berth else None),
                "inbound": waiting,
            })

        return {
            "berths": rows,
            "total": len(rows),
            "occupied": occupied,
            "allotted": allotted,
            "free": len(rows) - occupied - allotted,
        }

    # ------------------------------------------------------------- berthing reconciliation
    async def berthing_reconciliation(self, *, terminal: Optional[str] = None,
                                      limit: int = 100,
                                      offset: int = 0) -> dict[str, Any]:
        """Berthing-report rows with the PCS lifecycle state alongside.

        Reports and calls are two INDEPENDENT sources for the same physical call, so the
        report's own ``status`` is preserved verbatim and the lifecycle state is added
        beside it. Nothing is overwritten: ``core.berthing_record.status`` is a CHECK-
        constrained column with its own seven-value vocabulary, and the engine's vocabulary
        is different — merging them would need a schema change AND would destroy the
        source-vs-source comparison that makes a mismatch visible.

        ``lifecycle`` is None when the VIA does not resolve to a call. That is a REAL
        finding (report present, no PCS message ingested, or a composite VIA), not an
        error, so the row is still returned.
        """
        params = {"terminal": terminal, "limit": limit, "offset": offset}
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(_BERTHING_RECONCILE), params)).mappings().all()
        states = await self._projection.by_call_ids(
            [int(r["call_id"]) for r in rows if r["call_id"] is not None])

        items: list[dict[str, Any]] = []
        matched = 0
        for r in rows:
            lifecycle: Optional[dict[str, Any]] = None
            if r["call_id"] is not None:
                matched += 1
                st = states[int(r["call_id"])]
                lifecycle = {
                    "call_id": int(r["call_id"]),
                    "vcn": r["vcn"],
                    "via_no": r["via_no"],
                    **st.to_dict(),
                }
            items.append({
                "record_id": int(r["record_id"]),
                "terminal": r["terminal"],
                "vessel_name": r["vessel_name"],
                "voyage_number": r["voyage_number"],
                "berth_number": r["berth_number"],
                # The PDF-sourced status, untouched.
                "report_status": r["report_status"],
                "eta": r["eta"],
                "ata": r["ata"],
                "berthing_time": r["berthing_time"],
                "departure_time": r["departure_time"],
                "lifecycle": lifecycle,
            })

        return {"items": items, "count": len(items), "matched": matched,
                "unmatched": len(items) - matched, "limit": limit, "offset": offset}

    # ------------------------------------------------------------- port craft
    async def port_craft_demand(self) -> dict[str, Any]:
        """Craft DEMAND derived from the vessel lifecycle, against real fleet CAPACITY.

        WHAT THIS DELIBERATELY DOES NOT DO
        ----------------------------------
        It does not say which tug is on which job, and it reports no utilisation
        percentage. ``core.port_craft`` is a static fleet REGISTER — name, type, owner,
        LOA, bollard pull — with no operational state column, and NOTHING in the schema
        links a craft to a call, a movement or a pilotage row. Converting "movements
        needing craft" into "craft engaged" would require an assumed craft-per-movement
        ratio, and that assumption is not in the data. Demand and capacity are both real;
        their ratio is not, so it is not published.

        Every per-call verdict is the engine's ``portcraft_state`` / ``pilot_state`` /
        ``departure_state``. This method aggregates them and adds no rule of its own.
        """
        async with get_engine(self._dsn).connect() as conn:
            ids = [int(r["call_id"]) for r in
                   (await conn.execute(text(_ACTIVE_CALLS))).mappings().all()]
            fleet = (await conn.execute(text(_FLEET_BY_TYPE))).mappings().all()

        states = await self._projection.by_call_ids(ids)

        inbound: list[dict[str, Any]] = []
        alongside: list[dict[str, Any]] = []
        outbound: list[dict[str, Any]] = []
        for p in states.values():
            if p.portcraft_state != "Busy":
                continue                      # engine says this call engages no craft
            # The phase is read off the engine's own fields, not re-derived from events.
            if p.departure_state == "Sailing":
                bucket, phase = outbound, _PHASE_OUTBOUND
            elif p.is_at_berth:
                bucket, phase = alongside, _PHASE_ALONGSIDE
            # 'Active' is the engine's word for an imported boarding; 'Onboard' is the
            # projection's word for a manual one. Same operational fact, same bucket —
            # imported calls never produce 'Onboard', so this adds no behaviour for them.
            elif p.pilot_state in ("Active", "Onboard"):
                bucket, phase = inbound, _PHASE_INBOUND
            else:
                continue                      # Busy but in no reportable phase
            bucket.append(_craft_movement(p, phase))

        by_type = [{"craft_type": f["craft_type"], "count": int(f["n"])} for f in fleet]
        return {
            "fleet": {"total": sum(t["count"] for t in by_type), "by_type": by_type},
            "demand": {
                "total": len(inbound) + len(alongside) + len(outbound),
                "inbound_movement": len(inbound),
                "alongside": len(alongside),
                "outbound_movement": len(outbound),
            },
            "inbound_movement": inbound,
            "alongside": alongside,
            "outbound_movement": outbound,
            "active_calls": len(states),
        }

    # ------------------------------------------------------------- shipping lines
    async def shipping_line_progress(self, *, line: Optional[str] = None,
                                     limit: int = 500) -> dict[str, Any]:
        """Vessel progress per shipping-line visit, from the shared projection.

        Each advance-list visit is resolved to its call and reported with the lifecycle
        stage, the effective times and whether the call is active or historical. Active vs
        historical is the ENGINE's ``is_in_port`` — not a date comparison written here.

        NOT REPORTED: current position. Neither core.vessel_call nor
        core.vessel_call_event carries a coordinate, and the projection exposes none, so a
        position would have to be invented. Live position belongs to the AIS layer.
        """
        async with get_engine(self._dsn).connect() as conn:
            visits = (await conn.execute(
                text(_SL_VISITS), {"line": line, "limit": limit})).mappings().all()

        states = await self._projection.by_vias(vp.all_candidates(visits))

        items: list[dict[str, Any]] = []
        by_line: dict[str, dict[str, int]] = {}
        matched = exact = composite = 0
        for v in visits:
            p, how = vp.resolve(v["vessel_visit"], states)
            code = str(v["shipping_line"] or "")
            agg = by_line.setdefault(code, {"visits": 0, "active": 0, "historical": 0,
                                            "unmatched": 0, "containers": 0})
            agg["visits"] += 1
            agg["containers"] += int(v["containers"] or 0)
            row: dict[str, Any] = {
                "shipping_line": code,
                "vessel_visit": v["vessel_visit"],
                "voyage": v["voyage"],
                "containers": int(v["containers"] or 0),
                "match": how,
                "lifecycle": None,
            }
            if p is not None:
                matched += 1
                exact += (how == vp.EXACT)
                composite += (how == vp.COMPOSITE)
                # Active vs historical is the engine's verdict, not a local rule.
                active = p.is_in_port
                agg["active" if active else "historical"] += 1
                row["lifecycle"] = {
                    "call_id": p.call_id, "vcn": p.vcn, "via_no": p.via_no,
                    "vessel_name": p.vessel_name,
                    "status": p.status,
                    "arrival_state": p.arrival_state,
                    "berth_state": p.berth_state,
                    "departure_state": p.departure_state,
                    "shipping_state": p.shipping_state,
                    "is_in_port": p.is_in_port, "is_at_berth": p.is_at_berth,
                    "berth_id": p.berth_id,
                    "eta": p.eta, "etd": p.etd,
                    "arrived_at": p.arrived_at, "berthed_at": p.berthed_at,
                    "departed_at": p.departed_at,
                    "latest_event": p.latest_event,
                }
            else:
                agg["unmatched"] += 1
            items.append(row)

        return {
            "items": items,
            "count": len(items),
            "matched": matched,
            "unmatched": len(items) - matched,
            "matched_exact": exact,
            "matched_composite": composite,
            "by_line": [{"shipping_line": k, **v} for k, v in sorted(by_line.items())],
        }
