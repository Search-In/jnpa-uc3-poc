"""Regression guard — marine upserts must not bind to a constraint NAME.

WHY THIS FILE EXISTS
--------------------
core.vessel_call / core.vessel_call_event are created by TWO different DDLs that name the
same uniqueness differently:

  * gateway/marine_ext.py  -> CONSTRAINT uq_vessel_call_vcn UNIQUE (vcn)
  * schema.sql (the DDL that built the deployed database)
                           -> vcn text UNIQUE   -- PostgreSQL auto-names this
                                                -- 'vessel_call_vcn_key'

``ON CONFLICT ON CONSTRAINT uq_vessel_call_vcn`` therefore raised "constraint
uq_vessel_call_vcn for table vessel_call does not exist" on the deployed database, the
transaction aborted, and persist() reported the failure as status=FAILED with
inserted=0 / updated=0 — every BERMAN and BERALT upload, silently.

Column inference (``ON CONFLICT (vcn)``) matches ANY unique index over those columns
whatever it is called, so it survives both DDLs and any future rename. These tests are
static — they read the SQL text and need neither a database nor a corpus, so they run
everywhere and fail fast if the coupling is ever reintroduced.
"""
from __future__ import annotations

import re

import pytest

from services.marine import repository as R

#: Every ON CONFLICT-bearing statement this module executes against core.vessel_call*.
CALL_STATEMENTS = {
    "_VESSEL_CALL_UPSERT": R._VESSEL_CALL_UPSERT,
    "_VESSEL_CALL_PREVCN_UPSERT": R._VESSEL_CALL_PREVCN_UPSERT,
    "_EVENT_INSERT": R._EVENT_INSERT,
}


class TestNoConstraintNameCoupling:
    """The defect itself: a hardcoded constraint name in a vessel_call* upsert."""

    @pytest.mark.parametrize("name", sorted(CALL_STATEMENTS))
    def test_statement_does_not_name_a_constraint(self, name):
        sql = CALL_STATEMENTS[name]
        assert "ON CONFLICT ON CONSTRAINT" not in sql.upper(), (
            f"{name} binds to a constraint NAME. core.vessel_call* uniqueness is named "
            "differently by marine_ext.py and schema.sql, so a named target fails on one "
            "of them — use column inference, e.g. ON CONFLICT (vcn)."
        )

    def test_the_exact_broken_name_is_gone_from_executable_sql(self):
        """`uq_vessel_call_vcn` may still appear in DDL and prose — never in an upsert."""
        for name, sql in CALL_STATEMENTS.items():
            assert "uq_vessel_call_vcn" not in sql, f"{name} still references it"

    def test_no_vessel_call_upsert_anywhere_in_the_module_names_a_constraint(self):
        """Catches a NEW statement added later that reintroduces the coupling."""
        import inspect
        src = inspect.getsource(R)
        for m in re.finditer(r"ON CONFLICT ON CONSTRAINT\s+(\w+)", src, re.I):
            assert not m.group(1).startswith("uq_vessel_call"), (
                f"a statement still binds to {m.group(1)} — use column inference")


class TestInferredTargetsAreCorrect:
    """Inference must name the columns the uniqueness is actually defined over."""

    def test_vcn_upsert_infers_on_vcn(self):
        assert "ON CONFLICT (vcn) DO UPDATE SET" in R._VESSEL_CALL_UPSERT

    def test_event_insert_infers_on_the_milestone_triple(self):
        assert ("ON CONFLICT (call_id, event_type, event_ts) DO NOTHING"
                in R._EVENT_INSERT)

    def test_prevcn_upsert_keeps_its_partial_predicate(self):
        """A partial index is only inferable when the predicate is restated — dropping
        `WHERE vcn IS NULL` would silently target the wrong index."""
        sql = R._VESSEL_CALL_PREVCN_UPSERT
        assert "ON CONFLICT (imo_no, voyage_no) WHERE vcn IS NULL DO UPDATE SET" in sql

    def test_upserts_still_return_the_inserted_discriminator(self):
        """persist() counts updates from `(xmax = 0) AS inserted`; losing it would make a
        successful update look like a no-op — the symptom that masked this bug."""
        for name in ("_VESSEL_CALL_UPSERT", "_VESSEL_CALL_PREVCN_UPSERT"):
            assert "RETURNING call_id, (xmax = 0) AS inserted" in CALL_STATEMENTS[name]


class TestUniquenessIsProvisionedIndependentlyOfCreateTable:
    """The root cause: uniqueness declared only INSIDE `CREATE TABLE IF NOT EXISTS` never
    materialises on a database whose table already exists. Every uniqueness an upsert
    infers must therefore also be created by a standalone statement."""

    def _ddl(self) -> list[str]:
        from gateway.marine_ext import _DDL
        return list(_DDL)

    def test_prevcn_index_is_created_standalone(self):
        assert any("CREATE UNIQUE INDEX IF NOT EXISTS uq_vessel_call_imo_voyage_pre_vcn" in s
                   for s in self._ddl())

    def test_event_uniqueness_is_repaired_standalone(self):
        """Without this, ON CONFLICT (call_id, event_type, event_ts) has nothing to infer
        on the deployed database and every event write fails."""
        ddl = self._ddl()
        repair = [s for s in ddl if "uq_vessel_call_event_row" in s]
        assert repair, "no standalone repair for core.vessel_call_event uniqueness"
        body = repair[0]
        assert "CREATE UNIQUE INDEX uq_vessel_call_event_row" in body
        assert "(call_id, event_type, event_ts)" in body

    def test_event_repair_cannot_abort_boot(self):
        """ensure_marine_schema runs all DDL in ONE transaction with no per-statement
        guard, so this repair must NOTICE-and-skip on duplicates, never RAISE."""
        body = [s for s in self._ddl() if "uq_vessel_call_event_row" in s][0]
        assert "RAISE NOTICE" in body
        assert "RAISE EXCEPTION" not in body

    def test_event_repair_is_idempotent(self):
        """Must no-op when the fresh-DB constraint or a previous run already satisfied it."""
        body = [s for s in self._ddl() if "uq_vessel_call_event_row" in s][0]
        assert "to_regclass('core.uq_vessel_call_event')" in body
        assert "to_regclass('core.uq_vessel_call_event_row')" in body
        assert "to_regclass('core.vessel_call_event')" in body  # table-absent guard


class TestFailureEnvelopeIsDistinguishable:
    """Secondary finding from the same incident: the FAILED envelope omits `failed`,
    `duplicate` and `invalid`, so upload_service's `.get(..., 0)` renders a hard error as
    `failed=0` — indistinguishable from a clean no-op. Pinned so the shape is a
    deliberate choice rather than an accident; widening it is a response-contract change.
    """

    def test_failure_dict_shape_is_pinned(self):
        import inspect
        src = inspect.getsource(R.VesselCallRepository._record_failure)
        assert '"status": "FAILED"' in src
        for absent in ('"failed"', '"duplicate"', '"invalid"'):
            assert absent not in src, (
                f"_record_failure now returns {absent}; update the upload-response "
                "contract and this test together")
