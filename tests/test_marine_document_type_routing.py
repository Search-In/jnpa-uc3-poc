"""Marine ``document_type`` routing — registry, explicit routing, the mismatch guard,
and the removal of the silent ``_target`` fallback.

Four tiers, each independently gated so a machine without the client corpus or without
Postgres still runs everything it can:

  * ``TestRegistry``        — pure table invariants + alias normalisation (always runs).
  * ``TestSyntheticRouting``— explicit routing / mismatch / per-message assertions over
                              inline XML+LOG (always runs; needs no client data).
  * ``TestBackwardCompat``  — REAL client fixtures parsed with and without an explicit
                              document_type must produce IDENTICAL results (per-fixture
                              skip). This is the backward-compatibility contract: an old
                              client sending only `file` is unaffected by the parameter.
  * ``TestMissingTarget``   — an untagged record must become a typed validation error
                              instead of being silently filed as a vessel_call (DB-gated).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import socket
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from services.marine.parsers import (
    DocumentTypeMismatch,
    PARSER_REGISTRY,
    UnknownDocumentType,
    known_document_types,
    normalise_document_type,
    parse_marine,
    resolve_by_document_type,
    resolve_by_format,
)

_CLIENT = Path(__file__).resolve().parents[2] / "client-data"
_NLP = Path(os.environ.get("MARINE_DATA_DIR", str(_CLIENT / "1-NLP Marine")))
_CRAFT_DIR = _CLIENT / "3- Port Craft & Pilot"
_PDF = _CRAFT_DIR / "Details_of_Port_Crafts.pdf"
_XLSX = _CRAFT_DIR / "Pilot_card_data.xlsx"
_SHP_BASE = _CLIENT / "2-JNPA_Sea_Channels_Bathymetry" / "Sea Channel" / "JNPA_Sea_Channels"

_VESPRO_XML = (b'<VesselProfile><DocumentType>VESPRO</DocumentType>'
               b'<Vessel><IMONumber>9999999</IMONumber></Vessel></VesselProfile>')
_CALINF_XML = (b'<VoyageRegistration><DocumentType>CALINF</DocumentType>'
               b'<Voyage><VIANo>S0001</VIANo></Voyage></VoyageRegistration>')


def _have_pdfplumber() -> bool:
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


def _log_with(*xmls: bytes) -> bytes:
    """Build a VESARR/VESDEP-style transmission log embedding each XML in ReqBody.XML."""
    parts = ", ".join('{"ReqBody": {"XML": %s}}' % json.dumps(x.decode()) for x in xmls)
    return ("[" + parts + "]").encode()


def _zip_shapefile() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for ext in (".shp", ".dbf", ".shx", ".prj"):
            p = _SHP_BASE.with_suffix(ext)
            if p.is_file():
                z.writestr(p.name, p.read_bytes())
    return buf.getvalue()


def _fingerprint(res) -> dict:
    """Everything an explicit document_type must NOT change."""
    return {
        "records": len(res.records),
        "targets": Counter(r.get("_target") for r in res.records),
        "messages": Counter(r.get("_message") for r in res.records),
        "row_count": res.row_count,
        "invalid": res.invalid_count,
        "duplicates": res.duplicate_count,
        "rejected": res.rejected,
        "errors": len(res.errors),
        "warnings": len(res.warnings),
    }


# --------------------------------------------------------------------------- tier 0
class TestRegistry:
    def test_every_spec_is_self_consistent(self):
        for canon, spec in PARSER_REGISTRY.items():
            assert spec.document_type == canon, "registry key must equal its document_type"
            assert spec.formats, f"{canon} declares no envelope format"
            # per-document types are routed per message and carry NO whole-file parser;
            # every other spec MUST carry one. The two are mutually exclusive.
            assert (spec.loader is None) == spec.per_document, (
                f"{canon}: loader/per_document disagree")

    def test_known_document_types_are_the_canonical_keys(self):
        assert known_document_types() == tuple(sorted(PARSER_REGISTRY))

    @pytest.mark.parametrize("raw,expected", [
        ("PORT_CRAFT", "PORT_CRAFT"), ("port-craft", "PORT_CRAFT"),
        ("Port Craft", "PORT_CRAFT"), ("  portcraft  ", "PORT_CRAFT"),
        ("sea_channels", "SEA_CHANNEL"), ("shp", "SEA_CHANNEL"),
        ("pilot_card", "PILOTAGE"), ("csv", "VESSEL_CALL_CSV"),
        ("berman", "BERMAN"),
    ])
    def test_aliases_and_spelling_normalise(self, raw, expected):
        assert resolve_by_document_type(raw).document_type == expected

    def test_normalise_is_idempotent(self):
        for canon in PARSER_REGISTRY:
            assert normalise_document_type(canon) == canon

    def test_unknown_document_type_raises_with_the_accepted_list(self):
        with pytest.raises(UnknownDocumentType) as ei:
            resolve_by_document_type("BATHYMETRY")   # not implemented yet — must reject
        assert ei.value.raw == "BATHYMETRY"
        assert "PORT_CRAFT" in ei.value.accepted

    @pytest.mark.parametrize("fmt,expected", [
        ("CSV", "VESSEL_CALL_CSV"), ("XLSX", "PILOTAGE"),
        ("PDF", "PORT_CRAFT"), ("SHP", "SEA_CHANNEL"),
    ])
    def test_format_mapping_reproduces_the_original_branch(self, fmt, expected):
        assert resolve_by_format(fmt).document_type == expected

    @pytest.mark.parametrize("fmt", ["XML", "LOG"])
    def test_xml_and_log_have_no_whole_file_parser(self, fmt):
        # They must fall through to per-message routing, exactly as the old chain did.
        assert resolve_by_format(fmt) is None


# --------------------------------------------------------------------------- tier 0b
class TestSyntheticRouting:
    def test_implicit_routing_unchanged_for_xml(self):
        res = parse_marine(_VESPRO_XML, "v.xml")
        assert res.row_count == 1 and not res.rejected

    def test_declared_type_matching_the_document_is_accepted(self):
        implicit = _fingerprint(parse_marine(_VESPRO_XML, "v.xml"))
        declared = _fingerprint(parse_marine(_VESPRO_XML, "v.xml", "VESPRO"))
        assert implicit == declared

    def test_declared_type_not_matching_the_document_is_a_typed_row_error(self):
        res = parse_marine(_VESPRO_XML, "v.xml", "BERMAN")
        assert res.invalid_count == 1
        assert res.errors[0]["error_code"] == "document_type_mismatch"
        assert res.errors[0]["raw_value"] == "VESPRO"
        assert not res.records, "a mismatched document must not be parsed"

    def test_declared_type_contradicting_the_envelope_raises(self):
        with pytest.raises(DocumentTypeMismatch) as ei:
            parse_marine(_VESPRO_XML, "v.xml", "PORT_CRAFT")   # PDF type, XML envelope
        assert ei.value.declared == "PORT_CRAFT"
        assert ei.value.detected == "XML"
        assert ei.value.expected == ("PDF",)

    def test_unknown_declared_type_raises(self):
        with pytest.raises(UnknownDocumentType):
            parse_marine(_VESPRO_XML, "v.xml", "NOT_A_TYPE")

    def test_blank_document_type_is_treated_as_absent(self):
        # A form field posted empty must not become an UnknownDocumentType.
        for blank in ("", "   "):
            assert _fingerprint(parse_marine(_VESPRO_XML, "v.xml", blank)) == \
                   _fingerprint(parse_marine(_VESPRO_XML, "v.xml"))

    def test_mixed_log_stays_per_message_routed(self):
        """Requirement: XML/LOG document types remain PER-MESSAGE routed."""
        log = _log_with(_VESPRO_XML, _CALINF_XML)
        res = parse_marine(log, "mixed.log")
        assert res.row_count == 2, "both embedded documents must be extracted"
        # Neither document may be rejected as an unsupported type — both are routable.
        assert not [e for e in res.errors if e["error_code"] == "unsupported_message_type"]

    def test_declared_type_on_a_mixed_log_asserts_per_document(self):
        log = _log_with(_VESPRO_XML, _CALINF_XML)
        res = parse_marine(log, "mixed.log", "VESPRO")
        mism = [e for e in res.errors if e["error_code"] == "document_type_mismatch"]
        assert len(mism) == 1, "only the CALINF document should mismatch"
        assert mism[0]["raw_value"] == "CALINF"


# --------------------------------------------------------------------------- tier 1
class TestBackwardCompat:
    """An explicit document_type must reproduce implicit routing EXACTLY."""

    @pytest.mark.skipif(not _NLP.is_dir(), reason=f"NLP Marine corpus absent: {_NLP}")
    def test_nlp_marine_xml_parity(self):
        files = sorted(_NLP.rglob("*.xml"))[:12]
        if not files:
            pytest.skip("no XML files in the NLP Marine corpus")
        checked = 0
        for f in files:
            content = f.read_bytes()
            implicit = parse_marine(content, f.name)
            dt = next((r.get("_message") for r in implicit.records if r.get("_message")), None)
            if dt not in PARSER_REGISTRY:
                continue    # mixed or unroutable file — parity is asserted per type below
            assert _fingerprint(parse_marine(content, f.name, dt)) == _fingerprint(implicit), \
                f"declaring {dt} changed the result for {f.name}"
            checked += 1
        assert checked, "no single-type XML fixture available to compare"

    @pytest.mark.skipif(not (_PDF.is_file() and _have_pdfplumber()),
                        reason=f"port-craft pdf/pdfplumber absent: {_PDF}")
    def test_port_craft_pdf_parity(self):
        content = _PDF.read_bytes()
        implicit = parse_marine(content, _PDF.name)
        assert implicit.records, "fixture should yield craft records"
        assert _fingerprint(parse_marine(content, _PDF.name, "PORT_CRAFT")) == _fingerprint(implicit)
        # the alias spelling must route identically too
        assert _fingerprint(parse_marine(content, _PDF.name, "port-craft")) == _fingerprint(implicit)

    @pytest.mark.skipif(not _XLSX.is_file(), reason=f"pilot card absent: {_XLSX}")
    def test_pilotage_xlsx_parity(self):
        content = _XLSX.read_bytes()
        implicit = parse_marine(content, _XLSX.name)
        assert implicit.records, "fixture should yield pilotage records"
        assert _fingerprint(parse_marine(content, _XLSX.name, "PILOTAGE")) == _fingerprint(implicit)

    @pytest.mark.skipif(not _SHP_BASE.with_suffix(".shp").is_file(),
                        reason=f"sea-channel shapefile absent: {_SHP_BASE}")
    def test_sea_channel_shp_parity(self):
        content = _zip_shapefile()
        implicit = parse_marine(content, "JNPA_Sea_Channels.zip")
        assert implicit.records, "fixture should yield channel records"
        assert _fingerprint(parse_marine(content, "JNPA_Sea_Channels.zip", "SEA_CHANNEL")) \
               == _fingerprint(implicit)

    @pytest.mark.skipif(not (_PDF.is_file() and _have_pdfplumber()),
                        reason=f"port-craft pdf/pdfplumber absent: {_PDF}")
    def test_wrong_declared_type_on_a_real_pdf_is_rejected_not_misrouted(self):
        """The silent-misroute class of bug: a PDF declared as something else must
        raise rather than be fed to the port-craft parser and quietly yield nothing."""
        with pytest.raises(DocumentTypeMismatch):
            parse_marine(_PDF.read_bytes(), _PDF.name, "SEA_CHANNEL")


# --------------------------------------------------------------------------- tier 2
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


def _run_isolated(run) -> None:
    async def _wrapped() -> None:
        from jnpa_shared.db import dispose_all
        try:
            await run()
        finally:
            await dispose_all()
    asyncio.run(_wrapped())


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres unreachable")
class TestMissingTarget:
    """The silent `_target` -> vessel_call fallback is gone: an untagged record is a
    typed validation error, never a row quietly filed into the vessel-call spine."""

    def _repo(self):
        from services.marine.repository import VesselCallRepository
        return VesselCallRepository(_DSN)

    async def _prepare(self):
        from gateway.marine_ext import ensure_marine_schema
        await ensure_marine_schema(_DSN)

    def _persist(self, record, tag: str):
        async def run():
            await self._prepare()
            res = await self._repo().persist(
                [record], filename=f"{tag}.xml",
                file_hash=hashlib.sha256(tag.encode()).hexdigest(),
                physical_format="XML", document_type="TEST")
            return res
        out = {}

        async def wrapper():
            out["res"] = await run()
        _run_isolated(wrapper)
        return out["res"]

    def test_untagged_record_fails_instead_of_becoming_a_vessel_call(self):
        res = self._persist({"vcn": "INNSA1UNTAGGED01", "vessel_name": "NO TARGET"},
                            "untagged")
        assert res["failed"] == 1, "an untagged record must be counted as failed"
        assert res["inserted"] == 0, "it must NOT be written to core.vessel_call"
        assert res["status"] in ("FAILED", "PARTIAL")

    def test_unrecognised_target_still_fails_and_reports_its_value(self):
        res = self._persist({"_target": "not_a_table", "vcn": "INNSA1BADTGT001"},
                            "badtarget")
        assert res["failed"] == 1
        assert res["inserted"] == 0
