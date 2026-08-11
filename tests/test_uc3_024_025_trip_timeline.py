"""UC3-024 universal trip resolver + UC3-025 checkpoint timeline.

The two behaviours these tickets are judged on, tested without a database:

  * UC3-024 — a plate, a container, an e-seal and a Form 13 number all resolve to
    the SAME trip, with a match confidence; several matches are reported as
    ambiguous rather than silently resolved to one.
  * UC3-025 — every checkpoint carries an evidence label. A step the corpus
    cannot source says NOT_IN_CORPUS instead of showing an invented time, and
    dwell is never computed across such a gap.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from services.trip_search.repository import norm  # noqa: E402
from services.trip_search.service import (  # noqa: E402
    CHECKPOINTS,
    KEY_ONLY,
    NOT_IN_CORPUS,
    VERIFIED,
    TripSearchService,
    build_timeline,
    detect_key_kind,
)

UTC = dt.timezone.utc

#: The Excel's hero trip: NSICT export Form 13, 12-06-2026. Every value below is
#: as printed on the customer's own document.
HERO = {
    "doc_id": 4, "doc_category": "FORM13", "doc_variant": "form13_nsict_egate",
    "doc_ref": "16497850", "pin_no": None, "visit_id": None,
    "doc_ts": dt.datetime(2026, 6, 12, 7, 53, tzinfo=UTC),
    "container_no": "MEDU1777575", "iso_code": "2210 (20 FT)", "load_status": None,
    "gross_weight_kg": 29350, "seal1": "0008264", "seal2": "5826371",
    "vehicle_no": "MH43CK1959", "bat_no": "U104", "driver_name": None,
    "driver_licence": None, "transporter_name": "Transtar Handling & Warehousing Co",
    "truck_in_ts": None, "truck_out_ts": None, "gate_no": None,
    "yard_position": None, "vessel_name": "NORTHERN PRACTISE", "voyage": "NTPS0633",
    "pol": None, "pod": "Colombo (LKCMB)", "booking_no": "MNL030", "cfs": None,
    "group_code": None, "attrs": {}, "image_file": None, "source_file": None,
    "data_origin": "REAL", "terminal_code": "NSICT",
    "terminal_name": "Nhava Sheva International Container Terminal",
    "terminal_operator": "DP World",
}

#: The Excel's hero VISIT for the timeline: eir4, GTI, 10-06-2026, with REAL
#: truck-in / truck-out times (14:55 / 16:17 IST).
EIR4 = {
    **HERO,
    "doc_id": 9, "doc_category": "EIR", "doc_variant": "eir4_gateway_one",
    "doc_ref": "5614330", "container_no": "NYKU4768188", "vehicle_no": "MH43BX1488",
    "seal1": "U1376237", "seal2": None, "bat_no": "D391",
    "driver_name": "BABALU KUMAR", "driver_licence": "UP6420140008203",
    "gross_weight_kg": 29750, "terminal_code": "GTI",
    "doc_ts": dt.datetime(2026, 6, 10, 10, 47, tzinfo=UTC),
    "truck_in_ts": dt.datetime(2026, 6, 10, 9, 25, tzinfo=UTC),
    "truck_out_ts": dt.datetime(2026, 6, 10, 10, 47, tzinfo=UTC),
}


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    async def find_by_key(self, q):
        n = norm(q)
        out = []
        for r in self._rows:
            for col in ("doc_ref", "pin_no", "container_no", "vehicle_no",
                        "seal1", "seal2"):
                if r.get(col) and norm(str(r[col])) == n:
                    out.append(r)
                    break
        return out

    async def find_by_prefix(self, q, limit=10):
        return []

    async def by_doc_id(self, doc_id):
        return next((r for r in self._rows if r["doc_id"] == doc_id), None)

    async def related_documents(self, *, container_no, vehicle_no):
        return [r for r in self._rows
                if (container_no and r.get("container_no") == container_no)
                or (vehicle_no and r.get("vehicle_no") == vehicle_no)]

    async def gate_events_for(self, vehicle_no, limit=50):
        return []


# ----------------------------------------------------------------- UC3-024
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["16497850", "MEDU1777575", "MH43CK1959", "5826371", "0008264"],
)
async def test_every_supported_key_resolves_to_the_same_trip(key):
    """UI-105: plate / container / e-seal / Form 13 all resolve to one trip."""
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    out = await svc.resolve(key)
    assert out["status"] == "RESOLVED"
    assert out["resolved_trip_id"] == "GD-4"
    assert out["trips"][0]["container_no"] == "MEDU1777575"
    assert out["trips"][0]["vessel_name"] == "NORTHERN PRACTISE"


@pytest.mark.asyncio
async def test_corpus_unique_keys_resolve_at_full_confidence():
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    for key in ("16497850", "MEDU1777575", "5826371"):
        out = await svc.resolve(key)
        assert out["trips"][0]["match_confidence"] == 1.0, key


@pytest.mark.asyncio
async def test_a_plate_resolves_at_lower_confidence_than_a_container():
    """A plate names a TRACTOR, not a trip: matching once is chance, not identity."""
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    by_plate = await svc.resolve("MH43CK1959")
    by_container = await svc.resolve("MEDU1777575")
    assert by_plate["trips"][0]["match_confidence"] < (
        by_container["trips"][0]["match_confidence"])


@pytest.mark.asyncio
async def test_normalised_keys_match_regardless_of_spacing():
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    out = await svc.resolve("mh43 ck 1959")
    assert out["status"] == "RESOLVED"
    assert out["resolved_trip_id"] == "GD-4"


@pytest.mark.asyncio
async def test_several_matches_are_ambiguous_and_nothing_is_chosen():
    """Picking the newest would look identical to a correct answer while being
    wrong, so the resolver must select nothing."""
    second = {**HERO, "doc_id": 9, "container_no": "NYKU4768188",
              "doc_ref": "5614330"}
    svc = TripSearchService(repository=_FakeRepo([HERO, second]))
    out = await svc.resolve("MH43CK1959")
    assert out["status"] == "AMBIGUOUS"
    assert out["ambiguous"] is True
    assert out["resolved_trip_id"] is None
    assert out["count"] == 2
    assert all(t["match_confidence"] < 1.0 for t in out["trips"])


@pytest.mark.asyncio
async def test_unknown_key_returns_no_match_and_invents_nothing():
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    out = await svc.resolve("ZZZZ9999999")
    assert out["status"] == "NO_MATCH"
    assert out["trips"] == []


@pytest.mark.asyncio
async def test_too_short_and_empty_queries_are_rejected_not_guessed():
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    for bad in ("", "  ", "ab"):
        out = await svc.resolve(bad)
        assert out["status"] == "INVALID_INPUT", bad
        assert out["trips"] == []


def test_key_kind_detection_is_advisory_and_shaped_right():
    assert detect_key_kind("MEDU1777575") == "CONTAINER"
    assert detect_key_kind("16497850") == "DOCUMENT_NO"
    assert detect_key_kind("MH43CK1959") == "PLATE"


# ----------------------------------------------------------------- UC3-025
def test_timeline_has_all_ten_checkpoints_in_order():
    steps = build_timeline(EIR4)["steps"]
    assert [s["key"] for s in steps] == [c["key"] for c in CHECKPOINTS]
    assert len(steps) == 10


def test_real_gate_times_are_verified():
    """eir4 prints truck-in 14:55 and truck-out 16:17 IST — both real."""
    steps = {s["key"]: s for s in build_timeline(EIR4)["steps"]}
    assert steps["recognition_portal"]["evidence"] == VERIFIED
    assert steps["gate_out"]["evidence"] == VERIFIED
    assert steps["documents_ready"]["evidence"] == VERIFIED
    assert steps["recognition_portal"]["ts"] is not None


def test_enroute_and_plaza_steps_are_labelled_not_in_corpus_with_no_time():
    """Gaps G6/G9. The honest gap is the demo point — it must not be filled in."""
    steps = {s["key"]: s for s in build_timeline(EIR4)["steps"]}
    for key in ("corridor_entry", "plaza_entry", "plaza_release", "gate_queue_join"):
        assert steps[key]["evidence"] == NOT_IN_CORPUS, key
        assert steps[key]["ts"] is None, f"{key} must not carry a fabricated time"


def test_evidenced_but_untimed_steps_are_key_only():
    """The slip prints a weight and a BAT number but no time for either."""
    steps = {s["key"]: s for s in build_timeline(EIR4)["steps"]}
    assert steps["weighbridge"]["evidence"] == KEY_ONLY
    assert steps["weighbridge"]["ts"] is None
    assert "29750" in steps["weighbridge"]["detail"].replace(",", "")
    assert steps["security_documentation"]["evidence"] == KEY_ONLY
    assert "D391" in steps["security_documentation"]["detail"]


def test_dwell_is_never_computed_across_a_not_in_corpus_gap():
    """A dwell spanning an unsourced step would be an invented duration."""
    steps = build_timeline(EIR4)["steps"]
    by_key = {s["key"]: s for s in steps}
    # recognition_portal follows four NOT_IN_CORPUS steps -> no dwell.
    assert by_key["recognition_portal"]["dwell_minutes"] is None
    for s in steps:
        if s["evidence"] == NOT_IN_CORPUS:
            assert s["dwell_minutes"] is None, s["key"]


def test_summary_counts_and_in_gate_time_are_derived_not_asserted():
    summary = build_timeline(EIR4)["summary"]
    assert summary["total_steps"] == 10
    assert summary["verified"] == 3
    assert summary["key_only"] == 2
    assert summary["not_in_corpus"] == 5
    assert summary["verified"] + summary["key_only"] + summary["not_in_corpus"] == 10
    # 09:25 -> 10:47 UTC = 82 minutes (14:55 -> 16:17 IST).
    assert summary["in_gate_minutes"] == 82.0


def test_a_document_with_no_gate_times_reports_no_in_gate_duration():
    summary = build_timeline(HERO)["summary"]
    assert summary["in_gate_minutes"] is None
    assert summary["verified"] >= 1  # documents_ready still has a real timestamp


@pytest.mark.asyncio
async def test_trip_detail_carries_timeline_documents_and_share_link():
    svc = TripSearchService(repository=_FakeRepo([HERO, EIR4]))
    trip = await svc.trip("GD-9")
    assert trip is not None
    assert trip["container_no"] == "NYKU4768188"
    assert len(trip["timeline"]) == 10
    assert trip["share_path"] == "/truck-visit?trip=GD-9"
    assert trip["evidence_labels"][VERIFIED]
    assert trip["documents"], "the visit's paper trail must be listed"


@pytest.mark.asyncio
async def test_unknown_trip_id_returns_none_rather_than_an_empty_shell():
    svc = TripSearchService(repository=_FakeRepo([HERO]))
    assert await svc.trip("GD-9999") is None
    assert await svc.trip("not-a-trip") is None
