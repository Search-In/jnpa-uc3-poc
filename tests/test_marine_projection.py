"""Marine Projection Layer — the single lifecycle source, and the proof it stays single.

The projection exists because consumers had begun re-implementing the same three things:
the events lookup, the VIA-resolution LATERAL, and the derive_state call. Two modules had
already diverged into near-copies before this layer landed.

These tests are the guard: NO module outside projection.py may call derive_state or query
core.vessel_call_event for lifecycle purposes.

Pure: no DB, no corpus.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from services.marine.projection import CallProjection, MarineProjection, project

REPO = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ev(t: str, hour: int = 12):
    return {"event_type": t, "event_ts": dt.datetime(2026, 7, 29, hour, 0, tzinfo=IST)}


def proj(*events, **call):
    call.setdefault("call_id", 1)
    return project(call, [ev(*e) if isinstance(e, tuple) else ev(e) for e in events])


# ---------------------------------------------------------------- exposed surface
class TestProjectionExposesTheRequiredView:
    def test_every_required_field_is_present(self):
        d = proj("BERTHED", status="Berth Allotted").to_dict()
        for f in ("status", "arrival_state", "berth_state", "pilot_state",
                  "departure_state", "shipping_state", "portcraft_state",
                  "is_in_port", "is_at_berth", "latest_event", "latest_event_time"):
            assert f in d, f"projection is missing {f}"

    def test_effective_timestamps_cover_every_milestone(self):
        d = proj("BERTH_ALLOTTED", "ANCHORED", "PILOT_BOARDED", "BERTHED",
                 "ARRIVED", "SAILED", "DEPARTED").to_dict()
        for f in ("berth_allotted_at", "anchored_at", "pilot_boarded_at", "berthed_at",
                  "arrived_at", "sailed_at", "departed_at"):
            assert d[f] is not None, f"{f} not projected"

    def test_planned_times_come_from_the_call_row(self):
        now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=IST)
        p = project({"call_id": 1, "eta": now, "etb": now, "etd": now}, [])
        assert (p.eta, p.etb, p.etd) == (now, now, now)

    def test_berthed_at_is_exposed_although_no_column_holds_it(self):
        """core.vessel_call has etb but NO atb. The projection surfaces the berthing
        actual from the ledger, so consumers get it without a migration."""
        p = proj(("BERTHED", 21))
        assert p.berthed_at is not None
        assert not hasattr(p, "atb")

    def test_identity_is_carried_for_joins(self):
        p = project({"call_id": 7, "vcn": "V", "via_no": "S0001", "imo_no": "9",
                     "vessel_name": "X", "voyage_no": "1"}, [])
        assert (p.call_id, p.vcn, p.via_no, p.imo_no) == (7, "V", "S0001", "9")


class TestEffectiveTimestamps:
    def test_earliest_wins_so_a_re_emitted_message_cannot_move_an_actual(self):
        """VESDEP is emitted twice for one call in the corpus."""
        p = project({"call_id": 1}, [ev("DEPARTED", 10), ev("DEPARTED", 14)])
        assert p.departed_at.hour == 10

    def test_absent_milestone_is_none_never_a_default_time(self):
        p = proj("ANCHORED")
        assert p.berthed_at is None and p.departed_at is None

    def test_untimed_event_does_not_produce_a_timestamp(self):
        p = project({"call_id": 1}, [{"event_type": "BERTHED", "event_ts": None}])
        assert p.berthed_at is None

    def test_events_tuple_lists_what_was_seen(self):
        p = proj("ANCHORED", "BERTHED")
        assert set(p.events) == {"ANCHORED", "BERTHED"}


class TestDerivationDelegates:
    def test_state_matches_the_engine(self):
        from services.marine.state_engine import derive_state
        call, events = {"call_id": 1, "status": "Berth Allotted"}, [ev("BERTHED")]
        p, s = project(call, events), derive_state(call, events)
        assert (p.status, p.berth_state, p.is_at_berth) == (
            s.status, s.berth_state, s.is_at_berth)

    def test_projection_is_immutable(self):
        p = proj("BERTHED")
        with pytest.raises(Exception):
            p.status = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------- the anti-duplication guard
class TestSingleSourceOfTruth:
    """The whole point of the layer. If these fail, duplication has returned."""

    ALLOWED = {"services/marine/projection.py", "services/marine/state_engine.py"}

    def _py(self):
        for base in ("services", "gateway"):
            for f in (REPO / base).rglob("*.py"):
                if "__pycache__" in str(f):
                    continue
                yield f.relative_to(REPO).as_posix(), f.read_text(encoding="utf-8")

    def test_only_the_projection_calls_derive_state(self):
        offenders = [n for n, s in self._py()
                     if "derive_state(" in s and n not in self.ALLOWED]
        assert offenders == [], (
            f"{offenders} call derive_state directly — consume MarineProjection instead")

    #: Legitimate owners of an event-ledger query that is NOT lifecycle derivation:
    #:   * repository.py   — the module's own event read/write API (/events, /timeline,
    #:                       the import path and the ata/atd projection);
    #:   * marine_ext.py   — DDL and the unique-index repair (schema, not business state);
    #:   * manual_craft.py — WRITES the CRAFT_ASSIGNED / CRAFT_RELEASED milestones, the
    #:                       operator-side counterpart to the import path. It writes and
    #:                       never reads: craft_state is derived by state_engine from
    #:                       these rows like any imported milestone, which is precisely
    #:                       what stops craft being a second state machine.
    LEDGER_OWNERS = ALLOWED | {"services/marine/repository.py", "gateway/marine_ext.py",
                               "services/marine/manual_craft.py",
                               "services/marine/manual_pilot.py"}

    def test_only_the_projection_queries_the_event_ledger(self):
        """SQL only — a docstring naming the table is documentation, not a query."""
        import re
        pat = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+core\.vessel_call_event", re.I)
        offenders = [n for n, s in self._py()
                     if pat.search(s) and n not in self.LEDGER_OWNERS]
        assert offenders == [], (
            f"{offenders} query the event ledger — consume MarineProjection instead")

    def test_via_resolution_lateral_is_not_re_implemented(self):
        """The recycled-VIA tiebreak must be stated once."""
        offenders = [n for n, s in self._py()
                     if "ORDER BY eta DESC NULLS LAST, call_id DESC" in s
                     and n not in self.ALLOWED
                     and n not in ("services/marine/repository.py",
                                   "services/marine/state_service.py")]
        assert offenders == [], f"{offenders} re-implement VIA resolution"

    def test_berthing_no_longer_owns_a_lifecycle_query(self):
        """The duplication this layer was created to remove."""
        src = (REPO / "services/berthing/repository.py").read_text(encoding="utf-8")
        assert "vessel_call" not in src
        assert "derive_state" not in src
        assert "LATERAL" not in src

    def test_berthing_service_consumes_the_projection(self):
        src = (REPO / "services/berthing/service.py").read_text(encoding="utf-8")
        assert "MarineProjection" in src
        assert "by_vias(" in src

    def test_state_service_consumes_the_projection(self):
        src = (REPO / "services/marine/state_service.py").read_text(encoding="utf-8")
        assert "self._projection." in src
        assert "derive_state" not in src


class TestLookupKeys:
    """A consumer holds one of three keys; all three must be served."""

    def test_every_key_shape_is_available(self):
        for m in ("by_call_ids", "by_vcns", "by_vias", "one"):
            assert callable(getattr(MarineProjection, m))

    def test_projection_is_read_only(self):
        src = (REPO / "services/marine/projection.py").read_text(encoding="utf-8")
        for banned in ("INSERT", "UPDATE ", "DELETE", "CREATE ", "ALTER "):
            assert banned not in src.upper(), f"projection must not write: {banned}"

    def test_no_schema_or_contract_change(self):
        from gateway.marine_ext import _DDL
        assert not any("projection" in s.lower() for s in _DDL)
        from gateway.routers.berthing import ReportOut
        from gateway.routers.marine_calls import CallOut
        assert len(ReportOut.model_fields) == 17
        # 24 since the additive optional `lifecycle` landed: 23 stored columns + the
        # derived block. The stored 23 are unchanged — asserted below.
        assert len(CallOut.model_fields) == 24
        assert "lifecycle" in CallOut.model_fields
        assert CallOut(call_id=1).lifecycle is None


# ------------------------------------------------ timeline: lifecycle without a 2nd trip
class TestTimelineCarriesLifecycle:
    """`/api/marine/calls/{id}/timeline` derives the lifecycle from the rows it ALREADY
    loaded, so the detail pane needs one request instead of two.

    The invariant under guard is not "the field exists" — it is that adding it cost no
    extra query. A repository that is asked twice would silently double the pane's DB
    work, which is exactly the regression this class catches.
    """

    class _Repo:
        """Stands in for VesselCallRepository, counting how often it is consulted."""

        def __init__(self, row):
            self.row, self.calls = row, 0

        async def timeline(self, call_id, *, data_origin=None):
            # `data_origin` is the LIVE/DEMO narrowing the service forwards verbatim; it
            # changes no lifecycle rule, so the double accepts and ignores it.
            self.calls += 1
            return self.row

    class _Manual:
        """Stands in for ManualPilotService — no DB, and by default no assignment.

        Injected because pilot state is `imported OR manual OR pending`: the service must
        consult the manual reader, and the real one opens a connection. Returning None
        keeps every assertion below about the ENGINE's verdict, unchanged.
        """

        def __init__(self, assignment=None):
            self.assignment, self.calls = assignment, 0

        async def resolve_effective_pilot(self, call_id):
            self.calls += 1
            return self.assignment

    class _Milestones:
        """Stands in for PilotMilestoneService — no DB, and by default no milestones.

        Injected for the same reason `_Manual` is: the timeline now merges the pilotage
        milestones core.pilotage records, and the real reader opens a connection.
        Returning nothing keeps every assertion below about the LEDGER's own events.
        """

        def __init__(self, events=None):
            self.events, self.calls = events or [], 0

        async def by_call_id(self, call_id):
            self.calls += 1
            return list(self.events)

    def _service(self, row, assignment=None, milestones=None):
        from services.marine.service import VesselCallService
        repo = self._Repo(row)
        return VesselCallService(repository=repo,
                                 manual=self._Manual(assignment),
                                 milestones=self._Milestones(milestones)), repo

    @pytest.mark.asyncio
    async def test_lifecycle_is_attached_from_the_same_rows(self):
        row = {"call_id": 7, "status": "Berth Allotted",
               "ata": dt.datetime(2026, 7, 29, 6, 0, tzinfo=IST), "atd": None,
               "events": [ev("ARRIVED", 6), ev("BERTHED", 9)]}
        svc, repo = self._service(row)
        out = await svc.timeline(7)

        assert repo.calls == 1, "the lifecycle must not cost a second repository read"
        life = out["lifecycle"]
        assert life["status"] == "At Berth"
        assert life["is_in_port"] is True and life["is_at_berth"] is True
        # Highest RANK, not latest clock: BERTHED and ARRIVED routinely share a timestamp,
        # so EVENT_ORDER — not event_ts — decides which one the pane shows.
        assert life["latest_event"] == "ARRIVED"
        # The stored facts are untouched — the field is purely additive.
        assert out["call_id"] == 7 and len(out["events"]) == 2

    @pytest.mark.asyncio
    async def test_matches_the_dedicated_state_endpoint_exactly(self):
        """Two entry points, ONE rule set. If these ever disagree the lifecycle has been
        re-implemented somewhere, which is the failure this whole module exists to stop."""
        row = {"call_id": 7, "status": "Planned", "ata": None, "atd": None,
               "events": [ev("PILOT_BOARDED", 4)]}
        svc, _ = self._service(row)
        assert (await svc.timeline(7))["lifecycle"] == project(row, row["events"]).to_dict()

    @pytest.mark.asyncio
    async def test_the_manual_assignment_is_supplied_as_the_third_input(self):
        """Pilot state is `imported OR manual OR pending`, so the timeline must consult
        the manual reader. It once did not, and reported Pending for a vessel that had a
        manually assigned pilot while the LIST endpoint — which goes through
        MarineProjection — reported it correctly. Two paths, one of them under-fed."""
        row = {"call_id": 7, "status": "Berth Allotted", "ata": None, "atd": None,
               "events": []}
        svc, _ = self._service(row)
        out = await svc.timeline(7)
        assert svc._manual.calls == 1, "timeline must ask for the effective pilot"
        # No assignment in this stub, so the engine's own verdict still stands.
        assert out["lifecycle"]["pilot_state"] == "Pending"

    @pytest.mark.asyncio
    async def test_a_call_with_no_events_still_projects(self):
        svc, _ = self._service({"call_id": 7, "status": "Planned"})
        assert (await svc.timeline(7))["lifecycle"]["status"] == "Planned"

    @pytest.mark.asyncio
    async def test_missing_call_stays_none(self):
        svc, _ = self._service(None)
        assert await svc.timeline(999) is None

    def test_response_model_keeps_the_call_contract(self):
        """TimelineOut = CallOut + events. Nothing removed, nothing renamed.

        `lifecycle` used to be the other difference; it now lives on CallOut itself, so
        the LIST endpoint carries it too and TimelineOut simply inherits it. Both still
        expose it — the assertion moved, the guarantee did not.
        """
        from gateway.routers.marine_calls import CallOut, TimelineOut
        assert set(CallOut.model_fields) <= set(TimelineOut.model_fields)
        assert set(TimelineOut.model_fields) - set(CallOut.model_fields) == {"events"}
        assert "lifecycle" in CallOut.model_fields
        # Optional on BOTH: a payload without it must still parse, so the field can never
        # break an older gateway or a cached response.
        assert TimelineOut(call_id=1).lifecycle is None
        assert CallOut(call_id=1).lifecycle is None

    def test_service_derives_rather_than_re_queries(self):
        import re
        src = (REPO / "services/marine/service.py").read_text(encoding="utf-8")
        assert not re.search(r"(?:FROM|JOIN|INTO|UPDATE)\s+core\.", src, re.I), \
            "the read service must own no SQL of its own"
        assert "project(" in src


# ---------------------------------------- list endpoint: derived state beside stored stage
class TestCallListCarriesLifecycle:
    """`/api/marine/calls` returns the PARSER stage and the DERIVED state side by side.

    Before this, the list showed only `status` — the message stage — while the detail pane
    showed the operational state, so one call read 'Berth Allotted' in the table and
    'At Berth' in the timeline. Both facts are now returned; neither overwrites the other.
    """

    class _Repo:
        def __init__(self, rows):
            self.rows, self.calls = rows, 0

        async def list_calls(self, filters, *, sort, direction, limit, offset):
            self.calls += 1
            return [dict(r) for r in self.rows]

        async def count(self, filters):
            return len(self.rows)

    class _Projection:
        """Stands in for MarineProjection, counting batched reads."""

        def __init__(self, states):
            self.states, self.batches, self.last_ids = states, 0, None

        async def by_call_ids(self, ids):
            self.batches += 1
            self.last_ids = list(ids)
            return {k: v for k, v in self.states.items() if k in set(ids)}

    def _service(self, rows, states):
        from services.marine.service import VesselCallService
        repo, proj_ = self._Repo(rows), self._Projection(states)
        return VesselCallService(repository=repo, projection=proj_), proj_

    @staticmethod
    def _state(call_id, **kw):
        call = {"call_id": call_id, **{k: v for k, v in kw.items() if k != "events"}}
        return project(call, kw.get("events", ()))

    @pytest.mark.asyncio
    async def test_derived_state_is_attached_to_every_row(self):
        rows = [{"call_id": 48, "status": "Berth Allotted"},
                {"call_id": 51, "status": "Berth Allotted"}]
        states = {48: self._state(48, status="Berth Allotted",
                                  ata=dt.datetime(2026, 7, 29, 21, 24, tzinfo=IST),
                                  events=[ev("BERTHED", 21), ev("ARRIVED", 21)]),
                  51: self._state(51, status="Berth Allotted")}
        svc, _ = self._service(rows, states)
        out = await svc.list_calls({}, sort="updated_at", direction="desc",
                                   limit=50, offset=0)
        assert out["items"][0]["lifecycle"]["status"] == "At Berth"
        assert out["items"][0]["lifecycle"]["is_at_berth"] is True

    @pytest.mark.asyncio
    async def test_stored_parser_status_is_never_overwritten(self):
        """The two are different facts and the source-vs-derived comparison must survive."""
        rows = [{"call_id": 48, "status": "Berth Allotted"}]
        states = {48: self._state(48, status="Berth Allotted",
                                  ata=dt.datetime(2026, 7, 29, 21, 24, tzinfo=IST),
                                  events=[ev("BERTHED", 21), ev("ARRIVED", 21)])}
        svc, _ = self._service(rows, states)
        row = (await svc.list_calls({}, sort="updated_at", direction="desc",
                                    limit=50, offset=0))["items"][0]
        assert row["status"] == "Berth Allotted"          # parser stage, untouched
        assert row["lifecycle"]["status"] == "At Berth"   # operational state

    @pytest.mark.asyncio
    async def test_one_batched_projection_read_per_page_not_one_per_row(self):
        rows = [{"call_id": i, "status": None} for i in range(1, 26)]
        states = {i: self._state(i) for i in range(1, 26)}
        svc, proj_ = self._service(rows, states)
        await svc.list_calls({}, sort="updated_at", direction="desc", limit=50, offset=0)
        assert proj_.batches == 1, "the list must not issue one projection read per row"
        assert len(proj_.last_ids) == 25

    @pytest.mark.asyncio
    async def test_a_call_the_projection_cannot_answer_for_gets_null(self):
        """A real state — nothing ingested for it yet — not an error."""
        rows = [{"call_id": 48, "status": "Planned"}, {"call_id": 99, "status": None}]
        svc, _ = self._service(rows, {48: self._state(48, status="Planned")})
        items = (await svc.list_calls({}, sort="updated_at", direction="desc",
                                      limit=50, offset=0))["items"]
        assert items[0]["lifecycle"] is not None
        assert items[1]["lifecycle"] is None

    @pytest.mark.asyncio
    async def test_an_empty_page_issues_no_projection_read(self):
        svc, proj_ = self._service([], {})
        out = await svc.list_calls({}, sort="updated_at", direction="desc",
                                   limit=50, offset=0)
        assert out["items"] == [] and proj_.batches == 0

    @pytest.mark.asyncio
    async def test_envelope_keys_are_unchanged(self):
        svc, _ = self._service([{"call_id": 1, "status": None}], {})
        out = await svc.list_calls({}, sort="updated_at", direction="desc",
                                   limit=10, offset=0)
        assert set(out) == {"items", "total", "limit", "offset", "count"}

    def test_the_list_derives_nothing_itself(self):
        """It asks the projection; it must not restate a rule."""
        src = (REPO / "services/marine/service.py").read_text(encoding="utf-8")
        assert "derive_state" not in src
        assert "self._projection.by_call_ids" in src


class TestIdentityResolutionIsDeterministic:
    """Which call an event attaches to must never depend on write order.

    The resolver is VCN -> (imo, voyage) -> VIA. Every tier that can match more than one
    row must break the tie on properties OF THE CALL, so re-running the same import
    reaches the same call. Static: reads the SQL, no database needed.
    """

    #: The one tiebreak every consumer shares — repository, projection and state service.
    TIEBREAK = "ORDER BY eta DESC NULLS LAST, call_id DESC"

    def test_no_resolver_orders_by_a_mutable_column(self):
        """`updated_at` moves on every touch, so ordering by it makes identity depend on
        import order — the bug this class exists to prevent."""
        from services.marine import repository as R
        for name in ("_RESOLVE_BY_VCN", "_RESOLVE_BY_IMO_VOYAGE", "_RESOLVE_BY_VIA"):
            sql = getattr(R, name)
            assert "updated_at" not in sql, f"{name} ties identity to write order"

    def test_every_ambiguous_tier_uses_the_same_tiebreak(self):
        from services.marine import repository as R
        for name in ("_RESOLVE_BY_IMO_VOYAGE", "_RESOLVE_BY_VIA"):
            assert self.TIEBREAK in " ".join(getattr(R, name).split()), \
                f"{name} must share the canonical tiebreak"

    def test_vcn_tier_needs_no_tiebreak(self):
        """vcn is UNIQUE, so it can only ever match one row."""
        from services.marine import repository as R
        assert "ORDER BY" not in R._RESOLVE_BY_VCN

    def test_resolution_order_is_vcn_then_imo_voyage_then_via(self):
        """Strongest key first. Reordering would silently re-point events."""
        import inspect
        from services.marine.repository import VesselCallRepository
        body = inspect.getsource(VesselCallRepository._resolve_call_id)
        assert body.index("_RESOLVE_BY_VCN") < body.index("_RESOLVE_BY_IMO_VOYAGE") \
            < body.index("_RESOLVE_BY_VIA")

    def test_vessel_name_is_never_an_identity_key(self):
        """Names collide ('KMTC MUMBAI' vs 'kmtc Mumbai' are different visits) and are
        absent on CALINV/BERALT, so they must never resolve a call."""
        import re
        from pathlib import Path
        src = (REPO / "services/marine/repository.py").read_text(encoding="utf-8")
        # No WHERE / ON predicate may key on vessel_name.
        assert not re.search(r"(?:WHERE|AND|ON)\s+\w*\.?vessel_name\s*=", src, re.I), \
            "vessel_name used as a lookup key"
        assert Path is not None  # keep the import meaningful for linters

    def test_pilotage_binds_through_the_same_tiebreak(self):
        """core.pilotage resolves its call at INSERT time; it must not invent its own rule."""
        from services.marine import repository as R
        assert self.TIEBREAK in " ".join(R._PILOTAGE_INSERT.split())
