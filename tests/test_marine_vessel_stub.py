"""RC2 - minimal vessel stub for an IMO no VESPRO profiles.

core.vessel_call.imo_no -> core.vessel(imo_no) is attached NOT VALID by migration 0044,
which skips the back-scan but ENFORCES every new write - 0044 says so, and expects it to
"become load-bearing once marine ingestion starts". The PCS corpus reaches that point: it
carries calls for 107 IMOs whose VESPRO is in no file of the extract, and one such call
aborted an entire 880-record journal with integrity_error.

The fix is a MINIMAL stub - IMO, plus the name only when the message supplies one. These
tests pin both halves:

  * the SELECTION rules (pure, _stub_candidates)
  * the SQL CONTRACT that makes a stub safe (never overwrites, always enrichable)

Static: no database, no corpus, runs in CI everywhere - matching
test_marine_beralt_persistence.py.
"""
from __future__ import annotations

import re

from services.marine import repository as R
from services.marine.repository import VesselCallRepository as V


def _call(imo=None, name=None, **kw):
    d = {"_target": "vessel_call", "imo_no": imo, "vessel_name": name}
    d.update(kw)
    return d


def _vessel(imo, name=None):
    return {"_target": "vessel", "imo_no": imo, "vessel_name": name}


# ---------------------------------------------------------------- selection rules
class TestStubSelection:
    def test_unknown_imo_becomes_a_stub(self):
        """The whole point: a call for an unprofiled vessel must still land."""
        assert V._stub_candidates([], [_call("9535137")]) == {"9535137": None}

    def test_known_imo_is_not_stubbed(self):
        """Step 1 upserts the real vessel moments later; a stub would be redundant."""
        assert V._stub_candidates([_vessel("9535137", "HELLA")], [_call("9535137")]) == {}

    def test_name_is_preserved_when_the_message_carries_one(self):
        assert V._stub_candidates([], [_call("9535137", "HELLA")]) == {"9535137": "HELLA"}

    def test_name_is_null_when_the_message_carries_none(self):
        assert V._stub_candidates([], [_call("9535137")])["9535137"] is None
        # blank / whitespace is not a name
        assert V._stub_candidates([], [_call("9535137", "   ")])["9535137"] is None

    def test_a_nameless_message_never_erases_a_name_already_seen(self):
        got = V._stub_candidates([], [_call("9535137", "HELLA"), _call("9535137")])
        assert got == {"9535137": "HELLA"}
        # ...in either arrival order
        got = V._stub_candidates([], [_call("9535137"), _call("9535137", "HELLA")])
        assert got == {"9535137": "HELLA"}

    def test_no_imo_means_no_stub(self):
        """A call with no IMO constrains nothing - inventing a vessel for it would be
        fabricating data."""
        assert V._stub_candidates([], [_call(None), _call(""), _call("   ")]) == {}

    def test_both_call_groups_are_considered(self):
        """pre-VCN seeds (CALINF) and VCN calls (BERMAN/BERALT) both carry the FK."""
        got = V._stub_candidates([], [_call("111")], [_call("222", "B")])
        assert got == {"111": None, "222": "B"}

    def test_imos_are_whitespace_normalised(self):
        assert V._stub_candidates([], [_call(" 9535137 ")]) == {"9535137": None}


# ---------------------------------------------------------------- SQL contract
class TestStubSqlContract:
    def test_stub_writes_only_imo_and_name(self):
        """No LOA/GRT/call sign/flag/vespro_ref - a stub must stay self-evidently
        'seen but not profiled', never a fabricated vessel."""
        cols = re.search(r"INSERT INTO core\.vessel\s*\(([^)]*)\)",
                         R._VESSEL_STUB_INSERT, re.S).group(1)
        assert {c.strip() for c in cols.split(",")} == {"imo_no", "vessel_name"}
        for forbidden in ("loa_m", "grt", "call_sign", "flag", "vespro_ref", "mmsi", "dwt"):
            assert forbidden not in R._VESSEL_STUB_INSERT

    def test_stub_never_overwrites_an_existing_row(self):
        """DO NOTHING, not DO UPDATE: an existing vessel - stub or fully profiled - must
        be left alone, so a stub can never dilute a real profile."""
        assert "ON CONFLICT (imo_no) DO NOTHING" in R._VESSEL_STUB_INSERT
        assert "DO UPDATE" not in R._VESSEL_STUB_INSERT

    def test_stub_is_idempotent_and_reports_only_real_inserts(self):
        """RETURNING with DO NOTHING yields no row when the vessel already existed, which
        is how persist() counts stubs without double-counting a re-run."""
        assert "RETURNING imo_no" in R._VESSEL_STUB_INSERT

    def test_a_later_vespro_enriches_the_stub_in_place(self):
        """_VESSEL_UPSERT COALESCEs every particular onto the existing row and keys on
        imo_no, so the VESPRO fills the stub rather than creating a second vessel."""
        assert "ON CONFLICT (imo_no) DO UPDATE" in R._VESSEL_UPSERT
        for col in ("vessel_name", "loa_m", "grt", "call_sign", "flag", "vespro_ref"):
            assert re.search(rf"{col}\s*=\s*COALESCE\(EXCLUDED\.{col}", R._VESSEL_UPSERT), col

    def test_persist_stubs_before_it_writes_calls(self):
        """Ordering is the fix: the stub must exist before the FK is checked."""
        src = re.search(r"async def persist\(.*?\n    async def ",
                        open(R.__file__, encoding="utf-8").read(), re.S).group(0)
        assert src.index("_VESSEL_STUB_INSERT") < src.index("_VESSEL_CALL_PREVCN_UPSERT")
        assert src.index("_VESSEL_STUB_INSERT") < src.index("_VESSEL_CALL_UPSERT")

    def test_stubs_are_not_counted_as_imported_rows(self):
        """A stub is referential scaffolding, not an imported business record; counting it
        would overstate the import and break the run-1/run-2 comparison."""
        src = open(R.__file__, encoding="utf-8").read()
        block = src[src.index("stub_names = "):src.index("# 2. CALINF pre-VCN seed")]
        assert "ins +=" not in block and "upd +=" not in block
