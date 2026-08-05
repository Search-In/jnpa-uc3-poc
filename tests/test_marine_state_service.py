"""Business-state read surface — additivity, single-source-of-truth, and route shape.

Static: asserts the state service and router contain NO lifecycle rule of their own (every
verdict must come from state_engine) and that nothing pre-existing was modified.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE_SRC = (REPO / "services/marine/state_service.py").read_text(encoding="utf-8")
ROUTER_SRC = (REPO / "gateway/routers/marine_state.py").read_text(encoding="utf-8")


class TestNoDuplicatedBusinessRules:
    """The engine must remain the only place that understands progression."""

    @pytest.mark.parametrize("src,name", [(SERVICE_SRC, "state_service"),
                                          (ROUTER_SRC, "marine_state router")])
    def test_no_event_ladder_is_restated(self, src, name):
        from services.marine.state_engine import EVENT_ORDER
        # An event NAME may appear in a docstring, but never in an ordering structure.
        assert "EVENT_ORDER = " not in src, f"{name} restates the ladder"
        for e in EVENT_ORDER:
            assert f'"{e}"' not in src.split('"""')[-1], \
                f"{name} hardcodes milestone {e} outside a docstring"

    @pytest.mark.parametrize("src,name", [(SERVICE_SRC, "state_service"),
                                          (ROUTER_SRC, "marine_state router")])
    def test_no_status_ladder_is_restated(self, src, name):
        assert "STATUS_ORDER = " not in src
        assert "array_position" not in src

    def test_in_port_predicate_is_imported_not_written(self):
        assert "in_port_sql" in SERVICE_SRC
        assert "ata IS NOT NULL AND" not in SERVICE_SRC, \
            "state_service writes the in-port rule inline; import it from the engine"

    def test_router_derives_nothing_itself(self):
        """The router is a transport shell — no verdicts."""
        for banned in ("derive_state", "if is_at_berth", "Occupied'", 'Occupied"'):
            assert banned not in ROUTER_SRC, f"router computes state itself: {banned}"

    def test_service_delegates_every_verdict_to_the_projection(self):
        """Since the projection layer landed, this service neither queries the event
        ledger nor calls the engine — it asks MarineProjection."""
        assert "MarineProjection" in SERVICE_SRC
        assert SERVICE_SRC.count("self._projection.") >= 3
        assert "derive_state" not in SERVICE_SRC


class TestReadOnlyAndAdditive:
    def test_service_never_writes(self):
        for banned in ("INSERT", "UPDATE", "DELETE", "CREATE ", "ALTER "):
            assert banned not in SERVICE_SRC.upper().replace("UPDATED_AT", ""), \
                f"state_service must stay read-only: found {banned}"

    def test_no_migration_or_ddl_was_added(self):
        from gateway.marine_ext import _DDL
        joined = " ".join(_DDL)
        assert "state" not in joined.lower().replace("estate", "")

    def test_existing_call_contracts_are_untouched(self):
        """The whole point of a separate router: nothing pre-existing changed shape."""
        from gateway.routers.marine_calls import CallOut, EventOut, StatsOut
        # 23 stored columns + the additive optional `lifecycle` = 24. Additive only:
        # a payload without it still parses, so no existing client is affected.
        assert len(CallOut.model_fields) == 24
        assert CallOut(call_id=1).lifecycle is None
        # 8 stored columns + the additive optional `source` = 9. Same rule as `lifecycle`
        # above: a payload without it still parses, so no existing client is affected.
        assert len(EventOut.model_fields) == 9
        assert EventOut().source is None
        assert len(StatsOut.model_fields) == 12

    def test_repository_was_not_given_new_read_methods(self):
        """state_service owns its own SQL precisely so the repository stays as it was."""
        from services.marine.repository import VesselCallRepository as V
        assert not hasattr(V, "call_state")
        assert not hasattr(V, "berth_occupancy")


class TestRoutes:
    def test_both_routes_are_registered_and_read_only(self):
        from gateway.routers import marine_state
        paths = {(tuple(sorted(r.methods)), r.path) for r in marine_state.router.routes}
        assert (("GET",), "/api/marine/state/calls/{call_id}") in paths
        assert (("GET",), "/api/marine/state/berths") in paths
        assert all(m == ("GET",) for m, _ in paths), "state surface must be read-only"

    def test_router_is_registered_in_the_app(self):
        src = (REPO / "gateway/main.py").read_text(encoding="utf-8")
        assert "marine_state," in src
        assert "app.include_router(marine_state.router)" in src

    def test_call_state_model_exposes_every_specified_field(self):
        from gateway.routers.marine_state import CallStateOut
        f = set(CallStateOut.model_fields)
        assert {"status", "arrival_state", "berth_state", "pilot_state",
                "departure_state", "shipping_state", "portcraft_state",
                "is_in_port", "is_at_berth", "latest_event",
                "latest_event_time"} <= f

    def test_berth_states_are_the_three_lifecycle_outcomes(self):
        """Allocation -> occupied -> released, expressed as Free/Allotted/Occupied."""
        assert "Occupied" in SERVICE_SRC and "Allotted" in SERVICE_SRC
        assert "Free" in SERVICE_SRC


class TestBerthOccupancyShape:
    def test_every_berth_is_reported_not_only_the_busy_ones(self):
        """A berth missing from the list would read as 'no such berth', not 'free'."""
        assert "_ALL_BERTHS" in SERVICE_SRC
        assert "FROM core.ref_berth" in SERVICE_SRC

    def test_occupancy_verdict_comes_from_engine_flags(self):
        assert "is_at_berth" in SERVICE_SRC
        assert 'berth_state" ] == "Allotted"' not in SERVICE_SRC  # no string re-derivation


class TestBerthingReconciliation:
    """Berthing propagation: reports reconciled against the PCS lifecycle, never merged.

    core.berthing_record.status is a CHECK-constrained column with its OWN seven-value
    vocabulary sourced from terminal PDFs. Writing engine values into it would violate the
    constraint (a schema change) and destroy the source-vs-source comparison, so the
    lifecycle is added ALONGSIDE it.
    """

    def test_report_status_is_preserved_verbatim(self):
        assert '"report_status": r["report_status"]' in SERVICE_SRC
        assert "report_status" in ROUTER_SRC

    def test_lifecycle_is_a_separate_field_not_a_merge(self):
        assert '"lifecycle": lifecycle' in SERVICE_SRC
        # The report's own status must never be reassigned from engine output.
        assert 'report_status": st.' not in SERVICE_SRC
        assert 'report_status"] = ' not in SERVICE_SRC

    def test_berthing_table_and_its_status_column_are_not_written(self):
        for banned in ("UPDATE core.berthing_record", "INSERT INTO core.berthing_record"):
            assert banned not in SERVICE_SRC

    def test_join_is_lateral_because_via_recycles(self):
        """A short VIA is not unique, so a plain LEFT JOIN would fan one report row into
        several calls."""
        assert "LEFT JOIN LATERAL" in SERVICE_SRC
        assert "ORDER BY eta DESC NULLS LAST, call_id DESC" in SERVICE_SRC
        assert "LIMIT 1" in SERVICE_SRC

    def test_join_key_is_the_via(self):
        assert "via_no = b.voyage_number" in SERVICE_SRC

    def test_unmatched_rows_are_reported_not_dropped(self):
        """A report with no matching call is a real finding (composite VIA, or no PCS
        message ingested) and must stay visible."""
        assert '"unmatched"' in SERVICE_SRC
        assert "lifecycle: Optional[dict[str, Any]] = None" in SERVICE_SRC

    def test_projection_supplies_the_verdict(self):
        """call state, berth occupancy and berthing reconciliation — all three."""
        assert SERVICE_SRC.count("self._projection.") >= 3

    def test_no_berthing_vocabulary_is_restated(self):
        """EXPECTED/BERTH_ASSIGNED/CARGO_OPERATION belong to the berthing module."""
        for v in ("BERTH_ASSIGNED", "BERTHING_STARTED", "CARGO_OPERATION"):
            assert v not in SERVICE_SRC, f"state_service restates berthing vocabulary: {v}"

    def test_route_is_registered_and_read_only(self):
        from gateway.routers import marine_state
        paths = {(tuple(sorted(r.methods)), r.path) for r in marine_state.router.routes}
        assert (("GET",), "/api/marine/state/berthing") in paths

    def test_existing_berthing_contracts_are_untouched(self):
        from gateway.routers.berthing import ReportOut, StatsOut
        assert len(ReportOut.model_fields) == 17
        assert len(StatsOut.model_fields) == 9


class TestOptionalFiltersAreTyped:
    """asyncpg types every parameter at PREPARE, before any value is bound.

    A bare `:param IS NULL` gives PostgreSQL nothing to infer from, so the statement is
    rejected with `AmbiguousParameterError: could not determine data type of parameter $1`
    — on EVERY call, with and without a value, because the failure precedes binding.
    That is what made /api/marine/state/berthing return HTTP 500 unconditionally.

    This guards the CLASS of defect, not the single instance: any future optional filter
    written the same way fails here instead of in production.
    """

    #: `:name IS NULL` with no CAST around the parameter.
    _UNTYPED = re.compile(r":(\w+)\s+IS\s+(?:NOT\s+)?NULL", re.I)

    @staticmethod
    def _sql_only(src: str) -> str:
        """Source with `#` comment lines removed.

        The comments in state_service.py QUOTE the broken form to explain it, so scanning
        raw source reports the prose as a defect. Only executable SQL may be matched.
        """
        return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    #: Empty, and it must stay that way. `_SL_VISITS` held the last entry (`line`) until
    #: /api/marine/state/shipping-lines was repaired; both optional filters in this module
    #: are now CAST. An addition here means a new 500 is being accepted rather than fixed.
    _KNOWN_UNTYPED: set[str] = set()

    def test_no_new_sql_compares_a_bare_parameter_to_null(self):
        offenders = set(self._UNTYPED.findall(self._sql_only(SERVICE_SRC))) - self._KNOWN_UNTYPED
        assert not offenders, (
            "optional-filter parameters must be CAST so asyncpg can type them at PREPARE; "
            f"untyped: {sorted(offenders)}")

    def test_the_debt_list_is_empty(self):
        """Every optional filter in this module is typed. The allowlist exists only so a
        deliberate, reviewed exception is visible — it must not become a parking space."""
        assert self._KNOWN_UNTYPED == set(), (
            "an untyped optional filter is being accepted rather than fixed: "
            f"{sorted(self._KNOWN_UNTYPED)}")

    def test_the_shipping_line_filter_is_cast_on_both_sides(self):
        sql = " ".join(SERVICE_SRC.split())
        assert "CAST(:line AS text) IS NULL" in sql
        assert "a.line_code = CAST(:line AS text)" in sql

    def test_the_line_filter_still_means_all_lines_when_absent(self):
        sql = " ".join(SERVICE_SRC.split())
        assert "AND (CAST(:line AS text) IS NULL OR a.line_code = CAST(:line AS text))" in sql

    def test_the_visit_key_query_is_otherwise_unchanged(self):
        """The fix touched the WHERE predicate only — grouping and keys are untouched."""
        sql = " ".join(SERVICE_SRC.split())
        assert "FROM core.advance_list_container a" in sql
        assert "GROUP BY a.line_code, a.vessel_visit, a.voyage" in sql
        assert "a.vessel_visit IS NOT NULL AND a.vessel_visit <> ''" in sql

    def test_the_berthing_filter_is_cast_on_both_sides(self):
        sql = " ".join(SERVICE_SRC.split())
        assert "CAST(:terminal AS text) IS NULL" in sql
        assert "b.terminal = CAST(:terminal AS text)" in sql

    def test_the_filter_still_means_all_terminals_when_absent(self):
        """CAST changes the TYPE, never the logic: NULL must still disable the filter."""
        sql = " ".join(SERVICE_SRC.split())
        assert "WHERE (CAST(:terminal AS text) IS NULL OR b.terminal = CAST(:terminal AS text))" in sql

    def test_reconciliation_still_joins_on_the_via(self):
        """The fix touched the WHERE clause only — the LATERAL join is unchanged."""
        sql = " ".join(SERVICE_SRC.split())
        assert "WHERE via_no = b.voyage_number" in sql


class TestPortCraftPhaseSplit:
    """The three-phase demand split — the only Port-Craft-specific logic in this service.

    `portcraft_state` itself is the engine's (covered in test_marine_state_engine.py).
    What lives HERE is which phase a Busy call is counted in, and that precedence had no
    test. Static: reads the source, so no database is needed.
    """

    def test_only_busy_calls_are_counted(self):
        """A call the engine says engages no craft must not appear in any phase."""
        sql = " ".join(SERVICE_SRC.split())
        assert 'if p.portcraft_state != "Busy": continue' in sql

    def test_sailing_outranks_alongside(self):
        """A vessel that has SAILED is still at its berth until it clears. It must be
        counted OUTBOUND, not alongside — so the Sailing test has to come first."""
        body = self._demand_body()
        i_sail = body.index('p.departure_state == "Sailing"')
        i_berth = body.index("p.is_at_berth")
        # Matches the pilot branch whatever states it accepts — it now also admits the
        # projection's 'Onboard' (a MANUAL boarding) alongside the engine's 'Active'.
        # The test is about branch ORDER, so it must not be coupled to that list.
        i_pilot = body.index("p.pilot_state")
        assert i_sail < i_berth < i_pilot, (
            "phase precedence must be Sailing -> at berth -> pilot active; "
            "reordering silently reclassifies every sailing vessel")

    def test_the_three_phases_are_mutually_exclusive(self):
        """elif, not three ifs — one call must never be counted twice."""
        body = self._demand_body()
        assert body.count("elif") == 2 and body.count("if p.") >= 1

    def test_counts_are_derived_from_the_arrays_not_tracked_separately(self):
        """Counts and rows cannot drift if the count IS len(rows)."""
        sql = " ".join(SERVICE_SRC.split())
        assert '"total": len(inbound) + len(alongside) + len(outbound)' in sql
        assert '"inbound_movement": len(inbound)' in sql
        assert '"alongside": len(alongside)' in sql
        assert '"outbound_movement": len(outbound)' in sql

    def test_no_utilisation_ratio_is_published(self):
        """Nothing links a craft to a call, so demand/capacity would be invented.

        Scans the CODE only — the method's docstring explains at length that it publishes
        no utilisation percentage, so matching raw source would flag the explanation.
        """
        body = self._demand_body().lower()
        for banned in ("utilisation", "utilization", "/ fleet", "pct", "percent"):
            assert banned not in body, f"port craft must not publish {banned}"

    def test_fleet_capacity_comes_from_the_register_only(self):
        assert "FROM core.port_craft" in SERVICE_SRC
        assert "_FLEET_BY_TYPE" in SERVICE_SRC

    def test_phase_is_read_off_the_engine_never_recomputed(self):
        """No event names, no ladder — the phase reads projection fields only."""
        body = self._demand_body()
        for banned in ("BERTHED", "SAILED", "DEPARTED", "PILOT_BOARDED", "event_ts"):
            assert banned not in body, f"demand re-derives from events: {banned}"

    @staticmethod
    def _demand_body() -> str:
        """Source of port_craft_demand with comments and docstring stripped.

        The method's prose quotes the field names it uses, so scanning raw source would
        match the explanation rather than the code — the trap this helper avoids.
        """
        start = SERVICE_SRC.index("async def port_craft_demand")
        end = SERVICE_SRC.index("async def shipping_line_progress")
        body = SERVICE_SRC[start:end]
        body = re.sub(r'""".*?"""', "", body, flags=re.S)        # docstring
        return "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))


class TestCraftMovementCarriesLifecycle:
    """A demand row explains WHY the call counts, using values already in hand.

    The projection was resolved for the phase split anyway; before this the row kept 6 of
    its 16 fields and discarded the rest one line before serialisation.
    """

    def test_row_is_built_from_the_projection_verbatim(self):
        body = TestPortCraftPhaseSplit._demand_body()
        assert "_craft_movement(p, phase)" in body, \
            "the row must be built from the projection already in scope"

    def test_no_lifecycle_is_recomputed_for_the_row(self):
        src = " ".join(SERVICE_SRC.split())
        assert "derive_state" not in src
        # Every approved field is a straight read off `p`.
        for f in ("imo_no", "status", "arrival_state", "pilot_state", "berth_state",
                  "departure_state", "shipping_state", "portcraft_state",
                  "latest_event_time"):
            assert f'"{f}": p.{f}' in src, f"{f} must be copied from the projection"

    def test_movement_phase_names_the_existing_bucket(self):
        """Not a second classification — the branch already chosen, made explicit."""
        src = " ".join(SERVICE_SRC.split())
        assert '_PHASE_INBOUND = "Inbound"' in src
        assert '_PHASE_ALONGSIDE = "Alongside"' in src
        assert '_PHASE_OUTBOUND = "Outbound"' in src
        assert '"movement_phase": phase' in src

    def test_phase_constant_matches_the_bucket_it_is_appended_to(self):
        body = TestPortCraftPhaseSplit._demand_body()
        assert "bucket, phase = outbound, _PHASE_OUTBOUND" in body
        assert "bucket, phase = alongside, _PHASE_ALONGSIDE" in body
        assert "bucket, phase = inbound, _PHASE_INBOUND" in body

    def test_no_craft_identity_or_requires_flag_is_exposed(self):
        """Nothing links a craft to a call, so either would be invented."""
        from gateway.routers.marine_state import CraftMovementOut
        banned = {"requires_tug", "requires_pilot", "requires_launch", "assigned_tug",
                  "assigned_launch", "assigned_craft", "craft_name", "craft_assignment"}
        assert banned.isdisjoint(CraftMovementOut.model_fields)
        assert banned.isdisjoint(set(re.findall(r'"(\w+)":', SERVICE_SRC)))

    def test_every_new_field_is_optional(self):
        """Additive: a payload without them must still parse."""
        from gateway.routers.marine_state import CraftMovementOut
        m = CraftMovementOut(call_id=48)
        for f in ("imo_no", "status", "arrival_state", "pilot_state", "berth_state",
                  "departure_state", "shipping_state", "portcraft_state",
                  "latest_event_time", "movement_phase"):
            assert getattr(m, f) is None, f"{f} must default to None"

    def test_the_envelope_shape_is_unchanged(self):
        from gateway.routers.marine_state import PortCraftDemandOut
        assert set(PortCraftDemandOut.model_fields) == {
            "fleet", "demand", "inbound_movement", "alongside", "outbound_movement",
            "active_calls"}

    def test_a_busy_call_in_no_phase_is_still_not_reported(self):
        """The `else: continue` must not widen what counts as demand."""
        body = TestPortCraftPhaseSplit._demand_body()
        assert 'if p.portcraft_state != "Busy": continue' in " ".join(body.split())
