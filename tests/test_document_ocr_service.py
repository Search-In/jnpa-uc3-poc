"""/api/ocr — the LIVE -> LOCAL -> MOCK extraction chain.

Closes the audit finding that ``gateway/routers/document_ocr.py`` returned
``_mock_fields()`` for EVERY upload under a "field parsing TODO", even when a
real OCR read had succeeded — so the working Tesseract service in
``ingest/eir_ocr`` (validated against four real WhatsApp gate slips, built by
compose on port 8210) was never called by anything.

The tests exercise the chain with a stubbed upstream so they need neither the
sidecar nor a database.
"""
from __future__ import annotations

import pytest

from gateway.routers import document_ocr as doc


# ------------------------------------------------------------------ fixtures
class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Http:
    """Minimal stand-in for state.http capturing the outbound call."""

    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, **kw):
        self.calls.append((url, kw))
        if self._exc:
            raise self._exc
        return self._resp

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        if self._exc:
            raise self._exc
        return self._resp


class _Cfg:
    def __init__(self, eir_ocr_url="http://eir-ocr:8210"):
        self.eir_ocr_url = eir_ocr_url
        self.postgres_dsn = "postgresql://x/y"


class _State:
    def __init__(self, http, cfg=None):
        self.http, self.cfg = http, cfg or _Cfg()


#: The service's response for the first WhatsApp gate slip — the values the audit
#: named as proof of a real read (EIR 4339869, MH43BX1488, MSMU1908508).
#:
#: NOTE the shape: ingest/eir_ocr/app.py serialises FieldValue dataclasses via
#: fields_as_dict(), so `fields` is {name: {value, conf, evidence}} — NOT flat
#: scalars. The gateway must flatten it; this fixture keeps the real shape so
#: that requirement is actually exercised.
REAL_SLIP_RESPONSE = {
    "doc_type": "EIR",
    "raw_text": "EIR No 4339869\nLIC No MH43BX1488\nContainer MSMU1908508\n",
    "fields": {
        "Category": {"value": "IMPORT", "conf": 0.88, "evidence": "Category IMPORT"},
        "ShippingAgent": {"value": "MSC", "conf": 0.80, "evidence": "Agent MSC"},
        "EIRNo": {"value": "4339869", "conf": 0.85, "evidence": "EIR No 4339869"},
        "LICNo": {"value": "MH43BX1488", "conf": 0.93, "evidence": "LIC No MH43BX1488"},
        "ContainerNo": {"value": "MSMU1908508", "conf": 0.92, "evidence": "container_sweep"},
        "ISOCode": {"value": "2210", "conf": 0.75, "evidence": "ISO 2210"},
        "GrossWeight": {"value": "24.6 t", "conf": 0.70, "evidence": "24.6 t"},
        "BATNo": {"value": "B723", "conf": 0.66, "evidence": "BAT B723"},
    },
    "confidence": 0.91,
    "source": "TESSERACT",
    "engine_ready": True,
    "tesseract_version": "5.3.4",
    "variants_run": 2,
    "sha256": "f0151933fb88",
}

IMG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


# ============================================================== LIVE rung
@pytest.mark.asyncio
async def test_live_rung_returns_the_real_fields_not_mock():
    """The headline assertion: an EIR upload yields the REAL gate-slip values."""
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "slip.jpeg")

    assert out["source"] == "OCR_SERVICE"
    assert out["fields"]["EIRNo"] == "4339869"
    assert out["fields"]["LICNo"] == "MH43BX1488"
    assert out["fields"]["ContainerNo"] == "MSMU1908508"
    # The regression being fixed: these must NOT be the deterministic mock.
    mock = doc._mock_fields("EIR", "seed")
    assert out["fields"] != mock


@pytest.mark.asyncio
async def test_live_rung_posts_to_the_infer_endpoint():
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "slip.jpeg")
    url, kw = http.calls[0]
    assert url == "http://eir-ocr:8210/infer"
    assert "file" in kw["files"]
    assert kw["timeout"] == doc._EIR_OCR_TIMEOUT_S


@pytest.mark.asyncio
async def test_live_rung_reports_the_engine_that_read_the_document():
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert out["engine"]["service"] == "eir-ocr"
    assert out["engine"]["ready"] is True
    assert out["engine"]["tesseract_version"] == "5.3.4"


# ======================================================== field validation
@pytest.mark.asyncio
async def test_high_value_identifiers_are_validated():
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    v = out["validation"]
    assert v["ContainerNo"]["valid"] is True   # MSMU1908508 -> ISO 6346 shape
    assert v["LICNo"]["valid"] is True         # MH43BX1488  -> Indian plate
    assert v["EIRNo"]["valid"] is True         # 4339869     -> numeric


def test_validation_flags_a_misread_container_number():
    """A garbled read is reported as invalid, not silently dropped."""
    v = doc._validate_fields({"ContainerNo": "MSMU19085",  # too short
                              "LICNo": "MH43BX1488"})
    assert v["ContainerNo"]["valid"] is False
    assert v["LICNo"]["valid"] is True


def test_validation_normalises_before_matching():
    """OCR often inserts spaces/dashes; the format check must see through them."""
    v = doc._validate_fields({"ContainerNo": "msmu 1908508", "LICNo": "MH-43-BX-1488"})
    assert v["ContainerNo"]["valid"] is True
    assert v["ContainerNo"]["normalised"] == "MSMU1908508"
    assert v["LICNo"]["valid"] is True


def test_validation_skips_blank_sentinels():
    # eir2_dpworld_nsict genuinely has no container number on the slip.
    assert doc._validate_fields({"ContainerNo": "__BLANK__"}) == {}
    assert doc._validate_fields({"ContainerNo": None}) == {}


# ========================================================= fallback rungs
@pytest.mark.asyncio
async def test_falls_back_when_the_service_is_unreachable():
    http = _Http(exc=ConnectionError("no route to host"))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert out["source"] in {"OCR", "MOCK"}   # degraded, but never an exception
    assert "fields" in out


@pytest.mark.asyncio
async def test_falls_back_on_a_non_200():
    http = _Http(_Resp(503, {}))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert out["source"] in {"OCR", "MOCK"}


@pytest.mark.asyncio
async def test_falls_back_when_the_service_extracted_nothing():
    """A reachable service that read no fields is not better than a local read."""
    http = _Http(_Resp(200, {"fields": {}, "raw_text": "", "engine_ready": True}))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert out["source"] != "OCR_SERVICE"


@pytest.mark.asyncio
async def test_blank_sentinels_do_not_count_as_extracted_fields():
    http = _Http(_Resp(200, {
        "fields": {"ContainerNo": {"value": "__BLANK__", "conf": 0.0},
                   "LICNo": {"value": "", "conf": 0.0}},
        "raw_text": "x", "engine_ready": True}))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert out["source"] != "OCR_SERVICE"


# ========================================================= field flattening
class TestFlattenFields:
    def test_flattens_the_rich_service_shape_to_scalars(self):
        values, conf = doc._flatten_fields({
            "EIRNo": {"value": "4339869", "conf": 0.85, "evidence": "EIR No 4339869"},
        })
        # The jsonb column and every existing client expect a plain scalar here;
        # leaking the FieldValue dict would break them and is not serialisable.
        assert values == {"EIRNo": "4339869"}
        assert conf == {"EIRNo": 0.85}

    def test_tolerates_already_flat_scalars(self):
        values, conf = doc._flatten_fields({"EIRNo": "4339869"})
        assert values == {"EIRNo": "4339869"}
        assert conf == {}

    def test_drops_blanks_and_nulls(self):
        values, _ = doc._flatten_fields({
            "A": {"value": "__BLANK__"}, "B": {"value": ""},
            "C": {"value": None}, "D": {"value": "keep"},
        })
        assert values == {"D": "keep"}

    def test_empty_input_is_safe(self):
        assert doc._flatten_fields(None) == ({}, {})
        assert doc._flatten_fields({}) == ({}, {})


@pytest.mark.asyncio
async def test_live_fields_are_flat_scalars_ready_for_jsonb():
    """End-to-end: nothing FieldValue-shaped reaches the persisted payload."""
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    out = await doc._extract_async(_State(http), IMG, "image/jpeg", "EIR", "s.jpg")
    assert all(isinstance(v, str) for v in out["fields"].values())
    import json as _json
    _json.dumps(out["fields"])  # must not raise
    assert out["field_confidence"]["ContainerNo"] == 0.92


@pytest.mark.asyncio
async def test_unconfigured_upstream_skips_the_live_rung_entirely():
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    state = _State(http, _Cfg(eir_ocr_url=""))
    out = await doc._extract_async(state, IMG, "image/jpeg", "EIR", "s.jpg")
    assert http.calls == []
    assert out["source"] in {"OCR", "MOCK"}


@pytest.mark.asyncio
async def test_non_eir_doc_types_never_call_the_eir_service():
    """An invoice is not a gate slip — do not spend a round trip on it."""
    http = _Http(_Resp(200, REAL_SLIP_RESPONSE))
    for dtype in ("INVOICE", "LR", "EWAYBILL", "PERMIT", "UNKNOWN"):
        http.calls.clear()
        await doc._extract_async(_State(http), IMG, "image/jpeg", dtype, "f.jpg")
        assert http.calls == [], f"{dtype} should not hit the EIR OCR service"


# ============================================================ local rung
def test_local_rung_parses_fields_instead_of_mirroring_mock(monkeypatch):
    """The precise bug: a successful real read used to return _mock_fields().

    Stubs pytesseract so the test runs without the binary.
    """
    text = "EIR No 4339869\nLIC No MH43BX1488\nContainer MSMU1908508\n"

    class _FakeTess:
        @staticmethod
        def image_to_string(_img):
            return text

    class _FakeImage:
        @staticmethod
        def open(_buf):
            return object()

    import sys
    import types

    monkeypatch.setitem(sys.modules, "pytesseract", _FakeTess)
    pil = types.ModuleType("PIL")
    pil.Image = _FakeImage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", _FakeImage)

    out = doc._extract_local(IMG, "image/jpeg", "EIR")
    assert out is not None
    assert out["source"] == "OCR"
    assert out["raw_text"] == text.strip()
    # Real parsed values, not the hash-derived mock.
    assert out["fields"].get("EIRNo") == "4339869"
    assert out["fields"].get("ContainerNo") == "MSMU1908508"
    assert "lr_number" not in out["fields"]


def test_field_parser_returns_empty_rather_than_inventing(monkeypatch):
    """With no extractor importable, {} is the honest answer."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name.startswith("eir_ocr"):
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert doc._parse_fields_from_text("EIR No 4339869", "EIR") == {}


def test_parser_handles_empty_text():
    assert doc._parse_fields_from_text("", "EIR") == {}


# ============================================================== mock rung
def test_mock_rung_is_deterministic_and_tagged():
    a = doc._extract_mock(IMG, "LR")
    b = doc._extract_mock(IMG, "LR")
    assert a == b
    assert a["source"] == "MOCK"
    assert a["raw_text"].startswith("[MOCK OCR]")


def test_sync_extract_still_works_for_callers_without_state():
    """Backward compatibility: the old sync entry point is unchanged in shape."""
    out = doc._extract(b"", None, "INVOICE")
    assert out["source"] == "MOCK"
    assert set(out) >= {"raw_text", "fields", "confidence", "source"}


# =============================================================== doc types
def test_eir_and_gate_slip_are_accepted_doc_types():
    assert "EIR" in doc._DOC_TYPES and "GATE_SLIP" in doc._DOC_TYPES
    # Existing types must survive — removing one would break stored records.
    assert {"LR", "INVOICE", "EWAYBILL", "PERMIT", "UNKNOWN"} <= doc._DOC_TYPES
