"""BERALT persistence hardening — event berth, status monotonicity, provenance safety.

Closes the three gaps left open when the BERALT parser slice landed:

  A. the BERTH_ALLOTTED event parsed a berth_code that persistence then DISCARDED, so the
     milestone reached the timeline with no berth on it;
  B. re-importing an EARLIER message rewound the call's lifecycle stage, because both
     upserts took whatever status arrived last and file order is not lifecycle order;
  C. source_file looked like free provenance but its FK targets a different table.

Static: these read the SQL text and the DDL, so they need neither a database nor a
corpus and run in CI everywhere.
"""
from __future__ import annotations

import re

import pytest

from services.marine import repository as R
from services.marine.parsers.pcs_common import (CALL_STATUS_BERTH_ALLOTTED,
                                                CALL_STATUS_BERTH_PLANNED,
                                                CALL_STATUS_PLANNED)

UPSERTS = {"_VESSEL_CALL_UPSERT": R._VESSEL_CALL_UPSERT,
           "_VESSEL_CALL_PREVCN_UPSERT": R._VESSEL_CALL_PREVCN_UPSERT}


# ---------------------------------------------------------------- A. event berth
class TestEventCarriesItsBerth:
    def test_event_insert_writes_berth_id(self):
        # data_origin (migration 0121) rides alongside berth_id on the same insert — the
        # column list is pinned whole so neither can be dropped unnoticed.
        assert ("INSERT INTO core.vessel_call_event "
                "(call_id, event_type, event_ts, berth_id, data_origin)") in R._EVENT_INSERT

    def test_event_berth_is_resolved_through_code_then_alias(self):
        """Same resolve-or-NULL contract as the call: an unknown code stores NULL rather
        than failing the whole import."""
        assert "core.ref_berth " in R._EVENT_INSERT
        assert "core.ref_berth_alias" in R._EVENT_INSERT
        assert ":berth_code" in R._EVENT_INSERT

    def test_event_berth_code_is_passed_at_the_call_site(self):
        """The SQL binding is useless if persist() never supplies the parameter."""
        import inspect
        src = inspect.getsource(R.VesselCallRepository.persist)
        assert '"berth_code": e.get("berth_code")' in src

    def test_event_idempotency_target_is_unchanged(self):
        """Adding a column must not alter the milestone identity."""
        assert "ON CONFLICT (call_id, event_type, event_ts) DO NOTHING" in R._EVENT_INSERT

    def test_event_projection_exposes_berth_code(self):
        assert "AS berth_code" in R._EVENT_SELECT_COLS
        assert "JOIN" not in R._EVENT_SELECT_COLS.upper()  # scalar subquery


# ---------------------------------------------------------------- B. status ordering
class TestStatusNeverMovesBackwards:
    """A re-import must not rewind a call. Replaying a BERMAN file over a call BERALT has
    already advanced used to rewrite 'Berth Allotted' back to 'Berth Planned'."""

    @pytest.mark.parametrize("name", sorted(UPSERTS))
    def test_both_upserts_rank_the_status(self, name):
        sql = UPSERTS[name]
        assert "array_position" in sql, f"{name} still takes the last status unconditionally"
        assert "ELSE core.vessel_call.status END" in sql

    @pytest.mark.parametrize("name", sorted(UPSERTS))
    def test_ranking_covers_the_whole_known_vocabulary(self, name):
        sql = UPSERTS[name]
        for stage in (CALL_STATUS_PLANNED, CALL_STATUS_BERTH_PLANNED,
                      CALL_STATUS_BERTH_ALLOTTED):
            assert f"'{stage}'" in sql, f"{name} does not rank {stage!r}"

    def test_order_is_the_documented_lifecycle_order(self):
        """CALINF -> BERMAN -> BERALT (doc 05 Chain F). A wrong order silently inverts
        the guard, which is worse than not having it."""
        order = R._STATUS_ORDER
        i_planned = order.index(f"'{CALL_STATUS_PLANNED}'")
        i_berth_planned = order.index(f"'{CALL_STATUS_BERTH_PLANNED}'")
        i_allotted = order.index(f"'{CALL_STATUS_BERTH_ALLOTTED}'")
        assert i_planned < i_berth_planned < i_allotted

    def test_unknown_status_ranks_zero_on_both_sides(self):
        """COALESCE(..., 0) on BOTH sides gives the two safe behaviours: an unknown or
        absent incoming status cannot overwrite a known stage, and a known stage may still
        upgrade a row that has no recognised stage yet."""
        assert R._STATUS_RANK_NEW.startswith("COALESCE(array_position(")
        assert R._STATUS_RANK_OLD.startswith("COALESCE(array_position(")
        assert R._STATUS_RANK_NEW.endswith(", 0)")
        assert R._STATUS_RANK_OLD.endswith(", 0)")
        assert "EXCLUDED.status" in R._STATUS_RANK_NEW
        assert "core.vessel_call.status" in R._STATUS_RANK_OLD

    def test_guard_is_gte_so_a_replay_of_the_same_stage_is_still_idempotent(self):
        """Equal ranks must take the branch that writes, or re-importing the SAME file
        would start skipping its own status."""
        for sql in UPSERTS.values():
            assert re.search(r"CASE WHEN .*?\)\s*>=\s*COALESCE", sql, re.S)

    def test_other_columns_still_use_plain_coalesce(self):
        """The guard is scoped to status only — enrichment semantics elsewhere unchanged."""
        for sql in UPSERTS.values():
            assert "vessel_name = COALESCE(EXCLUDED.vessel_name" in sql
            assert "terminal_id = COALESCE(EXCLUDED.terminal_id" in sql
            assert "berth_id    = COALESCE(EXCLUDED.berth_id" in sql


# ---------------------------------------------------------------- C. provenance safety
class TestSourceFileIsNotWritten:
    """fk_vessel_call_event_source_file targets core.ingest_file(file_id), NOT the
    core.marine_import_files(id) this import path creates. Binding the ledger id there
    would violate the FK on every event — the same cross-object mistake that made
    ON CONFLICT ON CONSTRAINT fail in production."""

    def test_event_insert_does_not_bind_source_file(self):
        assert ":source_file" not in R._EVENT_INSERT
        assert "source_file" not in R._EVENT_INSERT.split("ON CONFLICT")[0]

    def test_the_fk_still_points_at_a_different_table(self):
        """If this ever changes, source_file becomes safe to write — and this test is the
        prompt to revisit it."""
        from gateway.marine_ext import _DDL
        fk = [s for s in _DDL if "fk_vessel_call_event_source_file" in s]
        assert fk, "FK definition not found"
        assert "REFERENCES core.ingest_file (file_id)" in fk[0]


# ------------------------------------------------- D. promote never steals a taken VCN
class TestPromoteDoesNotCollide:
    """`vcn` is UNIQUE. The promote UPDATE assumed it was the only writer — that a pre-VCN
    seed for an (imo, voyage) must be the row a VCN belongs to. When a SECOND row already
    holds that VCN the UPDATE raised 23505, and because persist() is ONE transaction an
    893-row journal import wrote ZERO rows.

    Static: reads the SQL text, so no database is needed.
    """

    def test_promote_guards_against_an_already_assigned_vcn(self):
        sql = " ".join(R._VESSEL_CALL_PROMOTE.split())
        assert re.search(
            r"NOT\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+core\.vessel_call\s+\w+\s+"
            r"WHERE\s+\w+\.vcn\s*=\s*:vcn\s*\)", sql, re.I), \
            "promote must not assign a VCN another row already holds"

    def test_the_original_match_predicates_are_intact(self):
        """The guard is ADDITIVE — the CALINF+BERMAN merge must still fire unchanged."""
        sql = " ".join(R._VESSEL_CALL_PROMOTE.split())
        assert "SET vcn = :vcn" in sql
        assert "vcn IS NULL" in sql
        assert "voyage_no = :voyage_no" in sql
        assert "imo_no IS NOT DISTINCT FROM" in sql

    def test_promote_is_still_a_single_update_on_the_call_spine(self):
        sql = R._VESSEL_CALL_PROMOTE.strip().upper()
        assert sql.startswith("UPDATE CORE.VESSEL_CALL")
        assert "INSERT" not in sql and "DELETE" not in sql

    def test_promote_still_takes_exactly_its_three_bind_params(self):
        """persist() binds {vcn, imo_no, voyage_no}; a new param would raise at execute."""
        params = set(re.findall(r":(\w+)", R._VESSEL_CALL_PROMOTE))
        assert params == {"vcn", "imo_no", "voyage_no"}
