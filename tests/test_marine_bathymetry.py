"""Bathymetry Phase 1 — canonical model, JSON adapter, PDF placeholder, routing.

Phase 1 delivers the pipeline WITHOUT chart extraction: the canonical model, both adapter
seams, the registry entry, and the persistence target. These tests lock the contract that
Phase 2 must not break — above all that the PDF arm and the JSON arm produce byte-identical
canonical records, including ``row_sha256``, so cross-source re-ingest is idempotent.

Tiers: everything here is pure except ``TestPersist``, which is DB-gated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from services.marine.parsers import (
    DocumentTypeMismatch,
    detect_format,
    parse_marine,
    resolve_by_document_type,
    resolve_by_format,
)
from services.marine.parsers.bathymetry_model import (
    DOCUMENT_TYPE,
    TARGET_SOUNDING,
    emit_document,
    make_sounding_record,
    normalise_sounding,
    row_sha256,
    sounding_errors,
)
from services.marine.parsers.bathymetry_pdf import NOT_IMPLEMENTED_CODE, parse_bathymetry_pdf
from services.marine.parsers.registry import _sniff_bathymetry
from services.marine.upload_parsers import ParseResult

_PORT_CRAFT_PDF = (Path(__file__).resolve().parents[2] / "client-data"
                   / "3- Port Craft & Pilot" / "Details_of_Port_Crafts.pdf")

_DRAWING = "6148-24-SUR-PO-119-JNPA"

# One georeferenced sounding, straight from the generated seed SQL.
_SOUNDING = {"easting_m": 271805.2, "northing_m": 2087828.9,
             "lat": 18.869910, "lon": 72.833965,
             "depth_m": 11.8, "above_design": True,
             "page_x_pt": 2739.2, "page_y_pt": 51.4}
# An UNGEOREFERENCED sounding — 30% of the real corpus looks like this.
_PAGE_ONLY = {"depth_m": 12.4, "above_design": True,
              "page_x_pt": 2720.9, "page_y_pt": 376.9}


def _doc(soundings, drawing_no=_DRAWING, **survey):
    s = {"drawing_no": drawing_no}
    s.update(survey)
    return {"document_type": DOCUMENT_TYPE, "survey": s, "soundings": soundings}


def _json_bytes(doc) -> bytes:
    return json.dumps(doc).encode()


# --------------------------------------------------------------------------- model
class TestCanonicalModel:
    def test_numeric_coercion_is_tolerant(self):
        rec = normalise_sounding({"depth_m": "11.8", "easting_m": "", "lat": None,
                                  "lon": "not-a-number", "above_design": "true"})
        assert rec["depth_m"] == 11.8
        assert rec["easting_m"] is None and rec["lat"] is None and rec["lon"] is None
        assert rec["above_design"] is True

    def test_nan_and_inf_are_treated_as_absent(self):
        rec = normalise_sounding({"depth_m": 5.0, "lat": float("nan"), "lon": float("inf")})
        assert rec["lat"] is None and rec["lon"] is None

    def test_depth_is_the_only_required_field(self):
        assert sounding_errors(normalise_sounding(_PAGE_ONLY)) is None
        assert sounding_errors(normalise_sounding({"above_design": False}))[0] == "missing_depth"

    @pytest.mark.parametrize("bad,code", [
        ({"depth_m": 1.0, "lat": 99.0}, "lat_out_of_range"),
        ({"depth_m": 1.0, "lon": -200.0}, "lon_out_of_range"),
    ])
    def test_coordinate_ranges_are_validated(self, bad, code):
        assert sounding_errors(normalise_sounding(bad))[0] == code

    def test_row_hash_is_stable_and_position_sensitive(self):
        a = normalise_sounding(_SOUNDING)
        assert row_sha256(_DRAWING, a) == row_sha256(_DRAWING, dict(a))
        moved = dict(a, page_x_pt=999.9)
        assert row_sha256(_DRAWING, moved) != row_sha256(_DRAWING, a), \
            "page position must participate: it is the only locator an ungeoreferenced chart has"
        assert row_sha256("OTHER-DRAWING", a) != row_sha256(_DRAWING, a)

    def test_same_depth_different_page_position_does_not_collide(self):
        """Two distinct soundings at equal depth on an ungeoreferenced chart."""
        one = make_sounding_record(_PAGE_ONLY, drawing_no=_DRAWING, source_file="a.pdf")
        two = make_sounding_record(dict(_PAGE_ONLY, page_x_pt=2734.8),
                                   drawing_no=_DRAWING, source_file="a.pdf")
        assert one["row_sha256"] != two["row_sha256"]

    def test_records_carry_the_house_routing_tags(self):
        rec = make_sounding_record(_SOUNDING, drawing_no=_DRAWING, source_file="x.pdf")
        assert rec["_target"] == TARGET_SOUNDING
        assert rec["_message"] == DOCUMENT_TYPE
        assert rec["_source_file"] == "x.pdf"
        assert rec["drawing_no"] == _DRAWING, "survey_id is resolved from this, never sent"
        assert "survey_id" not in rec, "survey_id is a DB surrogate; it must not be on the wire"


class TestEmitDocument:
    def test_happy_path(self):
        res = emit_document({"drawing_no": _DRAWING}, [_SOUNDING, _PAGE_ONLY],
                            filename="x.json", res=ParseResult())
        assert len(res.records) == 2 and not res.rejected
        assert res.row_count == 2
        assert {r["_target"] for r in res.records} == {TARGET_SOUNDING}

    def test_missing_drawing_no_is_rejected(self):
        res = emit_document({}, [_SOUNDING], filename="x.json", res=ParseResult())
        assert res.rejected and res.errors[0]["error_code"] == "missing_drawing_no"

    def test_empty_document_is_rejected_not_a_silent_success(self):
        res = emit_document({"drawing_no": _DRAWING}, [], filename="x.json", res=ParseResult())
        assert res.rejected and res.errors[0]["error_code"] == "no_soundings"

    def test_unusable_sounding_is_a_row_error_and_the_rest_still_import(self):
        res = emit_document({"drawing_no": _DRAWING},
                            [_SOUNDING, {"above_design": True}, _PAGE_ONLY],
                            filename="x.json", res=ParseResult())
        assert len(res.records) == 2
        assert res.invalid_count == 1
        assert res.errors[0]["error_code"] == "missing_depth"

    def test_in_file_duplicates_are_counted_not_stored_twice(self):
        res = emit_document({"drawing_no": _DRAWING}, [_SOUNDING, dict(_SOUNDING)],
                            filename="x.json", res=ParseResult())
        assert len(res.records) == 1 and res.duplicate_count == 1

    def test_ungeoreferenced_chart_warns_but_still_imports(self):
        res = emit_document({"drawing_no": _DRAWING}, [_PAGE_ONLY],
                            filename="x.json", res=ParseResult())
        assert len(res.records) == 1, "depth data must never be dropped for want of coordinates"
        assert any(w["error_code"] == "not_georeferenced" for w in res.warnings)


# --------------------------------------------------------------------------- JSON arm
class TestJsonAdapter:
    def test_canonical_document(self):
        res = parse_marine(_json_bytes(_doc([_SOUNDING])), "b.json")
        assert len(res.records) == 1 and not res.rejected
        assert res.records[0]["_target"] == TARGET_SOUNDING

    def test_document_type_key_is_optional(self):
        doc = _doc([_SOUNDING])
        del doc["document_type"]
        assert len(parse_marine(_json_bytes(doc), "b.json").records) == 1

    def test_flat_survey_header_is_accepted(self):
        flat = {"drawing_no": _DRAWING, "soundings": [_SOUNDING]}
        assert len(parse_marine(_json_bytes(flat), "b.json").records) == 1

    @pytest.mark.parametrize("payload,code", [
        (b"{not json", "invalid_json"),
        (b'[{"depth_m": 1}]', "not_a_document"),
        (b'{"document_type": "PORT_CRAFT", "survey": {"drawing_no": "X"}, "soundings": []}',
         "wrong_document_type"),
        (b'{"survey": {"drawing_no": "X"}}', "missing_soundings"),
    ])
    def test_malformed_payloads_are_typed_rejections(self, payload, code):
        res = parse_marine(payload, "b.json")
        assert res.rejected and res.errors[0]["error_code"] == code


class TestCrossSourceEquivalence:
    """THE Phase 1 guarantee: both arms funnel through emit_document, so a sounding
    ingested from a chart PDF and the same sounding ingested from the JSON API are
    byte-identical — which is what makes cross-source re-ingest idempotent rather than
    duplicating. Phase 2 must keep this test passing."""

    def test_json_arm_and_direct_emit_produce_identical_records(self):
        soundings = [_SOUNDING, _PAGE_ONLY]
        # JSON arm: bytes -> adapter -> emit_document
        via_json = parse_marine(_json_bytes(_doc(soundings)), "b.json").records
        # PDF arm (simulated): extracted rows -> emit_document, exactly what Phase 2 does.
        via_pdf = emit_document({"drawing_no": _DRAWING}, soundings,
                                filename="b.json", res=ParseResult()).records
        assert via_json == via_pdf

    def test_row_hash_matches_across_sources_so_reingest_is_idempotent(self):
        via_json = parse_marine(_json_bytes(_doc([_SOUNDING])), "chart.json").records[0]
        via_pdf = make_sounding_record(_SOUNDING, drawing_no=_DRAWING, source_file="chart.pdf")
        assert via_json["row_sha256"] == via_pdf["row_sha256"], \
            "the same sounding from two sources must collide on ON CONFLICT (row_sha256)"


# --------------------------------------------------------------------------- PDF arm
class TestPdfPlaceholder:
    def test_placeholder_rejects_explicitly_and_never_raises(self):
        res = parse_bathymetry_pdf(b"%PDF-1.4 fake", "bathy_chart.pdf")
        assert res.rejected
        assert res.errors[0]["error_code"] == NOT_IMPLEMENTED_CODE
        assert not res.records, "a placeholder must not look like a successful empty import"

    def test_bathymetry_named_pdf_reaches_the_placeholder_not_port_craft(self):
        """The silent-misroute bug this phase closes: before the sniff, a chart PDF was
        fed to the port-craft regex and reported ok with zero rows."""
        res = parse_marine(b"%PDF-1.4 fake chart", "6148-24-SUR-PO-119-JNPA.pdf")
        assert res.errors and res.errors[0]["error_code"] == NOT_IMPLEMENTED_CODE

    def test_explicit_document_type_reaches_the_placeholder(self):
        res = parse_marine(b"%PDF-1.4 fake", "anything.pdf", "BATHYMETRY")
        assert res.errors[0]["error_code"] == NOT_IMPLEMENTED_CODE


# --------------------------------------------------------------------------- routing
class TestRouting:
    def test_bathymetry_is_registered_for_both_envelopes(self):
        spec = resolve_by_document_type("BATHYMETRY")
        assert spec.formats == ("PDF", "JSON")
        assert spec.sniff is not None

    @pytest.mark.parametrize("alias", ["bathy", "soundings", "BATHYMETRY_JSON", "bathymetry-pdf"])
    def test_aliases_resolve(self, alias):
        assert resolve_by_document_type(alias).document_type == "BATHYMETRY"

    def test_json_envelope_is_detected(self):
        assert detect_format("b.json", _json_bytes(_doc([_SOUNDING]))) == "JSON"
        assert detect_format(None, b'{"a":1}') == "JSON"

    def test_transmission_log_still_beats_json(self):
        """A VESARR/VESDEP .log is also JSON — its ReqBody/XML signature must keep winning."""
        log = json.dumps([{"ReqBody": {"XML": "<VesselArrival/>"}}]).encode()
        assert detect_format("v.log", log) == "LOG"

    def test_pdf_still_defaults_to_port_craft(self):
        """PDF now has two claimants; the default MUST remain PORT_CRAFT."""
        assert resolve_by_format("PDF").document_type == "PORT_CRAFT"
        assert resolve_by_format("PDF", b"%PDF-1.4 nothing special",
                                 "Details_of_Port_Crafts.pdf").document_type == "PORT_CRAFT"

    def test_the_real_port_craft_filename_does_not_trigger_the_sniff(self):
        """Guards the one way this phase could break Port Craft: a false-positive sniff."""
        assert _sniff_bathymetry(b"%PDF-1.4 anything", "Details_of_Port_Crafts.pdf") is False

    @pytest.mark.parametrize("name", [
        "6148-24-SUR-PO-111-EF.pdf",
        "34_3652-JNPA-POST-BC-304_-_Post_Dredge_Survey_B-C_Area.pdf",
        "MB-005-25-BMCT-Chart_2k-Model.pdf",
        "some_bathymetry_survey.pdf",
    ])
    def test_real_bathymetry_filenames_trigger_the_sniff(self, name):
        assert _sniff_bathymetry(b"%PDF-1.4", name) is True

    def test_content_markers_catch_an_unhelpfully_named_chart(self):
        assert _sniff_bathymetry(b"%PDF-1.4 ... SOUNDINGS IN METRES ...", "scan001.pdf") is True

    def test_declaring_bathymetry_for_a_non_pdf_non_json_envelope_is_a_mismatch(self):
        with pytest.raises(DocumentTypeMismatch):
            parse_marine(b"<VesselProfile><DocumentType>VESPRO</DocumentType></VesselProfile>",
                         "v.xml", "BATHYMETRY")

    @pytest.mark.skipif(not _PORT_CRAFT_PDF.is_file(), reason="port-craft fixture absent")
    def test_the_real_port_craft_pdf_still_routes_to_port_craft(self):
        content = _PORT_CRAFT_PDF.read_bytes()
        assert resolve_by_format("PDF", content, _PORT_CRAFT_PDF.name).document_type == "PORT_CRAFT"


# --------------------------------------------------------------------------- persistence
_DSN = os.environ.get("POSTGRES_DSN", "")


def _pg_reachable() -> bool:
    if not _DSN or "asyncpg" not in _DSN:
        return False
    try:
        hostport = _DSN.split("@", 1)[1].split("/", 1)[0]
        host, _, port = hostport.partition(":")
        with socket.create_connection((host, int(port or "5432")), timeout=1.5):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres unreachable")
class TestPersist:
    def _run(self, records, tag):
        async def go():
            from gateway.marine_ext import ensure_marine_schema
            from jnpa_shared.db import dispose_all
            from services.marine.repository import VesselCallRepository
            try:
                await ensure_marine_schema(_DSN)
                return await VesselCallRepository(_DSN).persist(
                    records, filename=f"{tag}.json",
                    file_hash=hashlib.sha256(tag.encode()).hexdigest(),
                    physical_format="JSON", document_type=DOCUMENT_TYPE)
            finally:
                await dispose_all()
        return asyncio.run(go())

    def test_sounding_for_an_unknown_survey_is_a_typed_error_not_a_stub(self):
        rec = make_sounding_record(_SOUNDING, drawing_no="NO-SUCH-DRAWING-999",
                                   source_file="x.json")
        res = self._run([rec], "bathy_unresolved")
        assert res["failed"] == 1 and res["inserted"] == 0
