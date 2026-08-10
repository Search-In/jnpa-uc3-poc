"""UC3-002 — the 12 REAL gate documents: discovery, parsing and serving.

Two layers, deliberately separated:

  * Corpus tests run against the actual source drop and are skipped when it is
    not present (CI machines without the customer data). They assert the source
    inventory, the parsed->scan bijection and the field-level projection.
  * Router tests run everywhere against a stub service, and pin the API contract
    the T-04 screen consumes.

The projection is checked against the customer's OWN interpretation of their
data: their schema.sql/seed.sql ships a hand-curated INSERT for these same 12
documents, so the expectations below are their values, not ours. If the importer
and the customer disagree about a field, that is a real defect.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "shared")]


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_gate_documents", _ROOT / "scripts" / "import_gate_documents.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


I = _load_importer()

HERO_TRUCK = "MH43BX1488"
HERO_DL = "UP6420140008203"


# =============================== corpus layer ===============================
@pytest.fixture(scope="module")
def corpus():
    try:
        return I.find_corpus(None)
    except I.SourceError as exc:
        pytest.skip(f"gate-document corpus not available: {exc}")


@pytest.fixture(scope="module")
def documents(corpus):
    docs, scans = I.discover(corpus)
    I.attach_scans(docs, scans)
    rows = [I.normalise(d) for d in docs]
    return docs, rows, scans


def test_corpus_yields_exactly_twelve_documents(documents):
    docs, _rows, _scans = documents
    assert len(docs) == 12, [d.variant for d in docs]


def test_four_of_each_document_type(documents):
    docs, _rows, _scans = documents
    counts = {}
    for d in docs:
        counts[d.category] = counts.get(d.category, 0) + 1
    assert counts == {"EIR": 4, "FORM13": 4, "PIN_TICKET": 4}


def test_twelve_scans_map_one_to_one(documents):
    docs, _rows, scans = documents
    assert len(scans) == 12
    linked = [d.scan for d in docs]
    assert all(s is not None for s in linked), "every document needs its scan"
    assert len({s.name for s in linked}) == 12, "the mapping must be a bijection"
    assert {s.name for s in linked} == {s.name for s in scans}


def test_every_mapped_scan_is_a_real_readable_jpeg(documents):
    docs, _rows, _scans = documents
    for d in docs:
        head = d.scan.read_bytes()[:3]
        assert head == b"\xff\xd8\xff", f"{d.variant}: {d.scan.name} is not a JPEG"


def test_duplicate_document_is_folded_but_not_discarded(documents):
    """The corpus ships one Form 13 twice (GTI/IGT misspelling)."""
    docs, _rows, _scans = documents
    keeper = next(d for d in docs if d.variant == "form13_igt_eir")
    assert "form13_gti_eir" in keeper.aliases
    # Both source files stay registered, and the collision is reported.
    names = {p.name for p in keeper.all_files}
    assert "form13_gti_eir.xml" in names and "form13_igt_eir.json" in names
    assert any(kind == "duplicate" for kind, _sev, _detail in keeper.dq)
    # ...and it is not counted twice.
    assert [d.variant for d in docs].count("form13_gti_eir") == 0


# --- field-level projection, checked against the customer's own values -------
# (variant, column, expected) — drawn from the customer's seed INSERT.
EXPECTED = [
    ("eir3_gateway_maersk", "doc_ref", "5599372"),
    ("eir3_gateway_maersk", "container_no", "MRKU5014206"),
    ("eir3_gateway_maersk", "bat_no", "D391"),
    ("eir3_gateway_maersk", "vehicle_no", "MH43BX1488"),
    ("eir3_gateway_maersk", "driver_name", "BABALU KUMAR"),
    ("eir3_gateway_maersk", "driver_licence", "UP6420140008203"),
    ("eir3_gateway_maersk", "gross_weight_kg", 31810.0),
    ("eir3_gateway_maersk", "load_status", "FCL"),
    ("eir3_gateway_maersk", "group_code", "CLP"),
    ("eir4_gateway_one", "container_no", "NYKU4768188"),
    ("eir4_gateway_one", "gross_weight_kg", 29750.0),
    ("eir4_gateway_one", "vessel_name", "ONE RECOGNITION"),
    ("eir4_gateway_one", "pod", "INNSA/NLRTM"),
    ("eir1_psa_bmct", "doc_ref", "4339869"),
    ("eir1_psa_bmct", "container_no", "MSMU1908508"),
    ("eir1_psa_bmct", "vehicle_no", "MH43BX1488"),   # printed as "LIC NO"
    ("eir1_psa_bmct", "gross_weight_kg", 24600.0),   # "24.6 t" -> kg
    ("eir1_psa_bmct", "cfs", "CLP CFS"),
    ("eir1_psa_bmct", "seal2", None),                # 'NOSEAL' placeholder
    ("eir2_dpworld_nsict", "container_no", None),    # DP World layout has none
    ("eir2_dpworld_nsict", "yard_position", "4L10"),
    ("form13_igt_eir", "doc_ref", "250720653"),
    ("form13_igt_eir", "visit_id", "4418958"),
    ("form13_igt_eir", "container_no", "FFAU4770682"),
    ("form13_igt_eir", "bat_no", "B723"),
    ("form13_igt_eir", "gross_weight_kg", 31860.0),  # already kg
    ("form13_nsict_egate", "doc_ref", "16497850"),
    ("form13_nsict_egate", "iso_code", "2210"),      # from "2210 (20 FT)"
    ("form13_nsict_egate", "gross_weight_kg", 29350.0),
    ("form13_nsict_egate", "booking_no", "MNL030"),
    ("form13_psa_bmct", "doc_ref", "5921049"),
    ("form13_psa_bmct", "gross_weight_kg", 4500.0),  # "4.5" MT -> kg
    ("form13_psa_bmct", "seal1", None),              # 'NIL' placeholder
    ("form13_psa_bmct", "load_status", None),        # 'EMPTY' placeholder
    ("form13_nsft_eadvice", "doc_ref", "1778187"),
    ("form13_nsft_eadvice", "vehicle_no", None),     # slip prints no truck
    ("ticket1", "container_no", "AMSU4000180"),
    ("ticket1", "gross_weight_kg", 30490.0),
    ("ticket1", "yard_position", None),              # 'Read SMS' placeholder
    ("ticket2", "pin_no", "230283"),
    ("ticket2", "doc_ref", "1216572"),
    ("ticket2", "gate_no", "Gate 10"),
    ("ticket3", "visit_id", "4421881"),
    ("ticket3", "container_no", None),
    ("ticket4", "container_no", "SEGU5833837"),
    ("ticket4", "bat_no", "A658"),
]


@pytest.mark.parametrize("variant,column,expected", EXPECTED,
                         ids=[f"{v}.{c}" for v, c, _ in EXPECTED])
def test_projection_matches_the_customer_reference(documents, variant, column, expected):
    docs, rows, _scans = documents
    row = next(r for d, r in zip(docs, rows) if d.variant == variant)
    assert row[column] == expected


def test_timestamps_are_ist_aware_and_match_the_printed_wall_clock(documents):
    """The slip prints local time; we must store the matching instant."""
    docs, rows, _scans = documents
    row = next(r for d, r in zip(docs, rows) if d.variant == "eir3_gateway_maersk")
    assert row["truck_in_ts"] == dt.datetime(2026, 6, 6, 8, 26, tzinfo=I.IST)
    assert row["truck_out_ts"] == dt.datetime(2026, 6, 6, 11, 11, tzinfo=I.IST)
    assert row["doc_ts"].utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_attrs_keeps_the_source_payload_verbatim(documents):
    docs, rows, _scans = documents
    for doc, row in zip(docs, rows):
        assert row["attrs"] == doc.fields, f"{doc.variant}: attrs was altered"


def test_missing_values_stay_null_and_are_never_invented(documents):
    docs, rows, _scans = documents
    by_variant = {d.variant: r for d, r in zip(docs, rows)}
    # Only the two GTI EIRs print a driver licence — the other 10 slips do not.
    with_dl = [v for v, r in by_variant.items() if r["driver_licence"]]
    assert sorted(with_dl) == ["eir3_gateway_maersk", "eir4_gateway_one"]
    # Two Form 13s carry no truck number at all.
    assert by_variant["form13_nsft_eadvice"]["vehicle_no"] is None
    assert by_variant["form13_psa_bmct"]["vehicle_no"] is None


def test_every_document_resolves_to_a_known_terminal(documents):
    docs, rows, _scans = documents
    codes = {r["_terminal_code"] for r in rows}
    assert None not in codes
    assert codes == {"BMCT", "NSICT", "GTI", "NSIGT", "NSFT"}


def test_every_document_is_marked_real(documents):
    _docs, rows, _scans = documents
    assert {r["data_origin"] for r in rows} == {"REAL"}


def test_hero_truck_has_four_documents_three_terminals_over_seven_days(documents):
    docs, rows, _scans = documents
    hero = [r for r in rows if r["vehicle_no"] == HERO_TRUCK]
    assert len(hero) == 4
    assert len({r["_terminal_code"] for r in hero}) == 3
    days = sorted(r["doc_ts"].date() for r in hero)
    assert (days[-1] - days[0]).days + 1 == 7
    assert days[0] == dt.date(2026, 6, 6) and days[-1] == dt.date(2026, 6, 12)


def test_hero_truck_documents_carry_the_expected_containers(documents):
    _docs, rows, _scans = documents
    hero = {r["container_no"] for r in rows if r["vehicle_no"] == HERO_TRUCK}
    assert hero == {"MRKU5014206", "NYKU4768188", "FFAU4770682", "MSMU1908508"}


def test_data_quality_records_the_known_anomalies(documents):
    docs, _rows, _scans = documents
    kinds = {kind for d in docs for kind, _sev, _detail in d.dq}
    assert "duplicate" in kinds        # the GTI/IGT twin
    assert "placeholder" in kinds      # NIL / NOSEAL / EMPTY / Read SMS
    assert "missing_key" in kinds      # absent truck / licence / container
    assert "bad_date" not in kinds     # every document has a parseable date


def test_scan_object_keys_are_stable_and_bucket_relative(documents):
    docs, _rows, _scans = documents
    keys = {I.scan_key(d) for d in docs}
    assert len(keys) == 12
    assert all(k.startswith("gate_document/") and k.endswith(".jpeg") for k in keys)


# ---- discovery is real, not hard-coded -------------------------------------
def test_discovery_rejects_a_directory_that_is_not_the_corpus(tmp_path):
    with pytest.raises(I.SourceError):
        I.find_corpus(str(tmp_path))


def test_unmapped_document_fails_loudly_rather_than_guessing():
    """A new document with no verified scan mapping must stop the import."""
    doc = I.ParsedDoc("brand_new_slip", "EIR", {}, {})
    with pytest.raises(I.SourceError, match="no scan mapping"):
        I.attach_scans([doc], [])


# --- pure unit checks on the value helpers (no corpus needed) ---------------
def test_weight_conversion_is_unit_explicit():
    assert I.weight_kg("24.6 t", unit="t") == 24600.0
    assert I.weight_kg("31.81 MT", unit="mt") == 31810.0
    assert I.weight_kg("31860", unit="kg") == 31860.0
    assert I.weight_kg(None, unit="kg") is None


def test_placeholder_text_becomes_null():
    for token in ("NIL", "NOSEAL", "EMPTY", "NA", "Read SMS", "  "):
        assert I.scalar(token) is None
    assert I.scalar("CLP CFS") == "CLP CFS"


def test_plate_normalisation_is_punctuation_insensitive():
    assert I.plate("mh 43 bx 1488") == HERO_TRUCK
    assert I.plate("MH-43-BX-1488") == HERO_TRUCK


def test_timestamp_parser_covers_every_corpus_layout():
    for raw in ("12/06/2026 06:26", "12-Jun-2026 18:25:53", "06-06-2026 11:11",
                "2026-06-12 16:01:37"):
        assert I.ts(raw).tzinfo is not None
    with pytest.raises(ValueError):
        I.ts("not a date")


# ================================ API layer =================================
from gateway.routers import gate_documents as R  # noqa: E402

_DOC = {
    "doc_id": 1, "doc_category": "EIR", "doc_variant": "eir3_gateway_maersk",
    "doc_ref": "5599372", "container_no": "MRKU5014206", "vehicle_no": HERO_TRUCK,
    "bat_no": "D391", "driver_licence": HERO_DL, "driver_name": "BABALU KUMAR",
    "terminal": "GTI", "doc_ts": "2026-06-06T11:11:00+05:30",
    "image_file": "gate_document/eir3_gateway_maersk.jpeg",
    "evidence_uri": "/api/evidence/gate_document/eir3_gateway_maersk.jpeg",
    "data_origin": "REAL", "attrs": {"TruckNo": HERO_TRUCK},
}


class _StubService:
    def __init__(self):
        self.calls = []

    async def list_source_documents(self, **kw):
        self.calls.append(kw)
        return {"items": [_DOC], "total": 4, "limit": kw["limit"],
                "offset": kw["offset"], "count": 1,
                "terminals": ["BMCT", "GTI", "NSIGT"], "terminal_count": 3,
                "first_doc_ts": "2026-06-06T11:11:00+05:30",
                "last_doc_ts": "2026-06-12T06:26:00+05:30"}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(R.router)
    stub = _StubService()
    app.dependency_overrides[R.get_service] = lambda: stub
    c = TestClient(app)
    c.stub = stub  # type: ignore[attr-defined]
    return c


def test_documents_endpoint_reports_the_truck_visit_shape(client):
    r = client.get("/api/gate-docs/documents", params={"vehicle": HERO_TRUCK})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert body["terminal_count"] == 3
    assert body["terminals"] == ["BMCT", "GTI", "NSIGT"]
    assert r.headers["X-Total-Count"] == "4"


def test_documents_endpoint_exposes_the_original_scan(client):
    body = client.get("/api/gate-docs/documents").json()
    item = body["items"][0]
    assert item["evidence_uri"] == "/api/evidence/gate_document/eir3_gateway_maersk.jpeg"
    assert item["image_file"] == "gate_document/eir3_gateway_maersk.jpeg"
    # A same-origin proxy path, never a bucket URL or a local filesystem path.
    assert item["evidence_uri"].startswith("/api/evidence/")


def test_documents_endpoint_exposes_provenance(client):
    body = client.get("/api/gate-docs/documents").json()
    assert body["items"][0]["data_origin"] == "REAL"


def test_vehicle_filter_is_normalised_before_the_query(client):
    client.get("/api/gate-docs/documents", params={"vehicle": "mh 43 bx 1488"})
    assert client.stub.calls[-1]["vehicle"] == HERO_TRUCK  # type: ignore[attr-defined]


def test_every_documented_filter_reaches_the_service(client):
    client.get("/api/gate-docs/documents", params={
        "category": "EIR", "container": "MRKU5014206", "driver_licence": HERO_DL,
        "terminal": "GTI", "from_date": "2026-06-06", "to_date": "2026-06-12"})
    call = client.stub.calls[-1]  # type: ignore[attr-defined]
    assert call["category"] == "EIR"
    assert call["container"] == "MRKU5014206"
    assert call["driver_licence"] == HERO_DL
    assert call["terminal"] == "GTI"
    # Inclusive dates become a half-open IST window.
    assert call["from_ts"] == dt.datetime(2026, 6, 6, tzinfo=R._IST)
    assert call["to_ts"] == dt.datetime(2026, 6, 13, tzinfo=R._IST)


def test_documents_endpoint_rejects_an_inverted_window(client):
    r = client.get("/api/gate-docs/documents",
                   params={"from_date": "2026-06-12", "to_date": "2026-06-06"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_window"
