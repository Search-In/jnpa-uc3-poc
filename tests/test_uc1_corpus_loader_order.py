"""RC1 - the corpus loader must process call producers before event-only families.

VESARR/VESDEP carry only milestones (ANCHORED / DEPARTED); they create no vessel_call.
persist() step 4 resolves an event to its call and records `unresolved_call` rather than
inventing a stub call - a deliberate decision - so an event whose call does not exist yet
is a row error, and with every row erroring the file is FAILED.

Ordering them ahead of the journals that create their calls cost both logs entirely:
8 + 12 events, 0 rows persisted, both files FAILED.

Tier 1 - the declared order (pure).
Tier 2 - the corpus proves the order is SUFFICIENT: every VCN the two logs reference is
         produced as a vessel_call by a family that now runs earlier.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.ingest_uc1_corpus import (_CALL_PRODUCING_FAMILIES, _EVENT_ONLY_FAMILIES,
                                       _MARINE_FAMILIES, discover)
from services.marine.parsers import parse_marine

CORPUS = Path(os.environ.get(
    "UC1_CORPUS_DIR", str(Path(__file__).resolve().parents[2] / "client-data")))
corpus = pytest.mark.skipif(not (CORPUS / "1-NLP Marine").is_dir(),
                            reason=f"corpus absent: {CORPUS}")

_ORDER = [f[0] for f in _MARINE_FAMILIES]


# ---------------------------------------------------------------- Tier 1 - declared order
class TestDeclaredOrder:
    def test_every_event_only_family_follows_every_call_producer(self):
        last_producer = max(_ORDER.index(f) for f in _CALL_PRODUCING_FAMILIES)
        for fam in _EVENT_ONLY_FAMILIES:
            assert _ORDER.index(fam) > last_producer, (
                f"{fam} carries only events; it must run after every call-producing "
                f"family or its events cannot resolve. Order: {_ORDER}")

    def test_vespro_is_still_first(self):
        """The vessel master must precede any call that binds to an IMO (migration 0044)."""
        assert _ORDER[0] == "VESPRO"

    def test_the_named_families_are_all_in_the_plan(self):
        for fam in (*_CALL_PRODUCING_FAMILIES, *_EVENT_ONLY_FAMILIES):
            assert fam in _ORDER


# ---------------------------------------------------------------- Tier 2 - corpus proof
@corpus
class TestOrderIsSufficientForTheCorpus:
    """The declared order is only useful if the earlier families actually create the calls
    the later ones reference. This asserts that against the real files."""

    @staticmethod
    def _records_by_family():
        out: dict[str, list] = {}
        for item in discover(CORPUS):
            content = item["content"] or item["path"].read_bytes()
            out.setdefault(item["family"], []).extend(
                parse_marine(content, item["filename"]).records)
        return out

    def test_every_vesarr_vesdep_vcn_is_created_by_an_earlier_family(self):
        recs = self._records_by_family()

        produced = set()
        for fam in _CALL_PRODUCING_FAMILIES:
            for r in recs.get(fam, []):
                if r.get("_target") == "vessel_call" and r.get("vcn"):
                    produced.add(r["vcn"])

        referenced = set()
        for fam in _EVENT_ONLY_FAMILIES:
            for r in recs.get(fam, []):
                if r.get("_target") == "vessel_call_event" and r.get("vcn"):
                    referenced.add(r["vcn"])

        assert referenced, "expected VESARR/VESDEP to reference at least one VCN"
        unresolved = referenced - produced
        assert not unresolved, (
            f"{len(unresolved)} VCN(s) referenced by VESARR/VESDEP are created by no "
            f"earlier family, so reordering alone cannot fix them: {sorted(unresolved)[:5]}")
