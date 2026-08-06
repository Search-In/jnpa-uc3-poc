"""Marine Vessel-master slice tests — parser mapping (pure) + read layer (pure SQL build).

Tier 1 asserts the VESPRO → core.vessel field mapping against the OFFICIAL client files
in client-data/1-NLP Marine/VESPRO, same posture as tests/test_marine_parsers.py: the
customer samples ARE the fixtures. It is regression cover for three tags that were
previously unmapped — TEU, MMSINumber and the stern-thruster fit.

Tier 2 covers VesselRepository's pure query building (filters / sort whitelisting), which
needs no database: the SQL text is assembled before any connection is opened.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.marine.parsers import parse_marine
from services.marine.parsers.documents import safe_fromstring
from services.marine.parsers.vespro import _thruster, parse_vespro
from services.marine.vessel import VesselRepository

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))
VESPRO_DIR = DATA_DIR / "VESPRO"

pytestmark = pytest.mark.skipif(
    not VESPRO_DIR.is_dir(), reason=f"VESPRO client data absent: {VESPRO_DIR}")


def _vessel_records() -> list[dict]:
    """Every VESPRO file in the corpus → its single vessel record."""
    out = []
    for f in sorted(VESPRO_DIR.glob("*.xml")):
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected, f"{f.name} rejected"
        vessels = [r for r in res.records if r["_target"] == "vessel"]
        assert len(vessels) == 1, f"{f.name}: expected exactly one vessel record"
        out.append(vessels[0])
    return out


# ---------------------------------------------------------------- Tier 1 — mapping
class TestVesproMapping:
    def test_corpus_parses_and_keys_on_imo(self):
        recs = _vessel_records()
        assert len(recs) == 9, "expected the 9 corpus VESPRO documents"
        for v in recs:
            assert v["_message"] == "VESPRO"
            assert v["imo_no"] and v["imo_no"].isdigit()

    def test_teu_capacity_is_mapped(self):
        """REGRESSION: TEU was hardcoded None with the comment 'not carried by VESPRO'.

        It IS carried, in 6 of the 9 corpus documents. Asserting ">= 1 populated" rather
        than an exact count keeps the test honest if the corpus grows, while still failing
        hard if the mapping is ever dropped again.
        """
        recs = _vessel_records()
        populated = [v["teu_capacity"] for v in recs if v["teu_capacity"] is not None]
        assert populated, "TEU is present in the corpus but nothing was mapped"
        assert all(isinstance(t, int) and t > 0 for t in populated)

    def test_mmsi_is_mapped(self):
        """REGRESSION: mmsi was hardcoded None with the comment 'VESPRO carries no MMSI'."""
        recs = _vessel_records()
        populated = [v["mmsi"] for v in recs if v["mmsi"] is not None]
        assert populated, "MMSINumber is present in the corpus but nothing was mapped"
        # core.vessel.mmsi is text — keep it a string, never coerced to a number.
        assert all(isinstance(m, str) and m.isdigit() for m in populated)

    def test_stern_thruster_reads_the_count_tag(self):
        """REGRESSION: the parser read a 'SternThruster' tag that does not exist, so the
        column was permanently NULL. The corpus states the fit as TotalNoOfSternThrusters."""
        recs = _vessel_records()
        assert any(v["stern_thruster"] is not None for v in recs), \
            "no stern-thruster fit resolved from TotalNoOfSternThrusters"

    def test_bow_thruster_flag_still_wins(self):
        """The Y/N flag is present in every corpus file — the count fallback must not
        change the existing bow-thruster result."""
        recs = _vessel_records()
        assert all(v["bow_thruster"] is not None for v in recs)

    def test_absent_particular_stays_none_never_zero(self):
        """Sparse tags are normal for VESPRO. An absent TEU must be None, not 0 — a
        fabricated zero would read as 'a vessel with no capacity'."""
        recs = _vessel_records()
        assert any(v["teu_capacity"] is None for v in recs), "expected a sparse TEU"
        assert 0 not in [v["teu_capacity"] for v in recs]

    def test_insurance_block_is_a_list(self):
        for v in _vessel_records():
            assert isinstance(v["_insurance"], list)


class TestThrusterHelper:
    """Pure tri-state rules for :func:`_thruster`, without any XML."""

    @staticmethod
    def _el(**tags):
        body = "".join(f"<{k}>{v}</{k}>" for k, v in tags.items())
        return safe_fromstring(f"<VesselProfileDetails>{body}</VesselProfileDetails>")

    def test_flag_wins_over_count(self):
        el = self._el(BowThruster="N", TotalNoOfBowThrusters="2")
        assert _thruster(el, "BowThruster", "TotalNoOfBowThrusters") is False

    def test_count_used_when_flag_absent(self):
        el = self._el(TotalNoOfSternThrusters="1")
        assert _thruster(el, "SternThruster", "TotalNoOfSternThrusters") is True

    def test_zero_count_is_no_fit(self):
        el = self._el(TotalNoOfSternThrusters="0")
        assert _thruster(el, "SternThruster", "TotalNoOfSternThrusters") is False

    def test_both_absent_stays_none(self):
        """Tri-state: unknown is NOT 'no fit'."""
        el = self._el(VesselName="X")
        assert _thruster(el, "SternThruster", "TotalNoOfSternThrusters") is None


# ---------------------------------------------------------------- Tier 2 — read layer
class TestVesselRepositoryQueryBuilding:
    """`_where` is pure — it builds SQL text + bind params with no connection."""

    def _repo(self) -> VesselRepository:
        return VesselRepository(dsn=None)

    def test_no_filters_yields_no_where_clause(self):
        clause, params = self._repo()._where({})
        assert clause == "" and params == {}

    def test_equality_filter_is_bound_not_interpolated(self):
        clause, params = self._repo()._where({"flag": "HK"})
        assert clause == "WHERE v.flag = :flag"
        assert params == {"flag": "HK"}

    def test_like_filter_wraps_wildcards(self):
        clause, params = self._repo()._where({"name": " fortune "})
        assert "v.vessel_name ILIKE :name" in clause
        assert params["name"] == "%fortune%"

    def test_filters_combine_with_and(self):
        clause, _ = self._repo()._where({"flag": "HK", "name": "XT"})
        assert clause.startswith("WHERE ") and " AND " in clause

    def test_unknown_filter_key_is_ignored(self):
        """An unrecognised query param must not reach the SQL — no injection surface."""
        clause, params = self._repo()._where({"drop_table": "x"})
        assert clause == "" and params == {}

    def test_empty_like_value_is_ignored(self):
        clause, params = self._repo()._where({"name": ""})
        assert clause == "" and params == {}


class TestVesselSortWhitelist:
    """Sort keys are whitelisted, so an arbitrary `sort` cannot reach ORDER BY."""

    def test_known_sorts_map_to_qualified_columns(self):
        from services.marine.vessel import _SORTS
        assert _SORTS["teu_capacity"] == "v.teu_capacity"
        assert all(col.startswith("v.") for col in _SORTS.values())

    def test_teu_and_mmsi_are_selected(self):
        """The two newly-mapped columns must be in the projection or the API cannot
        expose what the parser now writes."""
        from services.marine.vessel import _COLUMNS
        assert "teu_capacity" in _COLUMNS
        assert "mmsi" in _COLUMNS
