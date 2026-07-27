"""NLP-Marine PCS parser framework (Phase 1) — pure, no DB, no API.

One entry point, ``parse_marine(content, filename)``, that:

  1. detects the envelope (CSV / XML / LOG)                — envelope.py
  2. extracts the PCS XML document(s)                      — envelope.py
  3. routes each by <DocumentType> to a message parser     — documents.py + REGISTRY
  4. returns normalized records in the SHARED ParseResult   — services.marine.upload_parsers.ParseResult

Every record carries ``_target`` (``vessel`` | ``vessel_call`` | ``vessel_call_event``)
so a later persistence phase can route it — this phase ONLY produces records.

Reuses ``ParseResult`` from the existing CSV upload parser (no duplication); the CSV
envelope delegates back to that parser unchanged, so this is a strict superset entry
point. It imports nothing from the service / repository / router layers.
"""
from __future__ import annotations

from typing import Any, Callable, Optional
import xml.etree.ElementTree as ET

from ..upload_parsers import ParseResult, parse as _csv_parse, read_rows_from_bytes
from .berman import parse_berman
from .calinf import parse_calinf
from .documents import document_type, safe_fromstring
from .envelope import detect_format, extract_xml_documents
from .pcs_common import MarineParseError
from .registry import (DocumentTypeError, DocumentTypeMismatch, PARSER_REGISTRY,
                       ParserSpec, UnknownDocumentType, known_document_types,
                       normalise_document_type, resolve_by_document_type,
                       resolve_by_format)
from .vesarr_vesdep import parse_vesarr, parse_vesdep
from .vespro import parse_vespro

# `document_type` (the <DocumentType> reader) is shadowed by parse_marine's parameter of
# the same name, so keep an unshadowed alias for internal use. The public export is
# unchanged — callers importing `document_type` from this package still get the function.
_document_type_of = document_type

# DocumentType → message parser. Adding a format = adding one entry here.
REGISTRY: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "VESPRO": parse_vespro,
    "CALINF": parse_calinf,
    "BERMAN": parse_berman,
    "VESARR": parse_vesarr,
    "VESDEP": parse_vesdep,
}

__all__ = [
    "parse_marine",
    "REGISTRY",
    "detect_format",
    "extract_xml_documents",
    "document_type",
    "ParseResult",
    # registry (explicit document_type routing)
    "PARSER_REGISTRY",
    "ParserSpec",
    "DocumentTypeError",
    "UnknownDocumentType",
    "DocumentTypeMismatch",
    "known_document_types",
    "normalise_document_type",
    "resolve_by_document_type",
    "resolve_by_format",
]


def _tag_csv(res: ParseResult) -> ParseResult:
    """Tag CSV records with the same discriminators the XML parsers emit, so every
    record leaving this framework has a ``_target``/``_message`` regardless of format."""
    for rec in res.records:
        rec.setdefault("_target", "vessel_call")
        rec.setdefault("_message", "CSV")
    return res


def parse_marine(content: bytes, filename: Optional[str] = None,
                 document_type: Optional[str] = None) -> ParseResult:
    """Parse one uploaded marine file into normalized records (ParseResult).

    Format-agnostic: CSV delegates to the existing tabular parser; XML/LOG are routed
    per message type. A single LOG may yield several messages; each unsupported or
    malformed document becomes a typed row error rather than an exception, exactly
    like the CSV path.

    ``document_type`` (optional, additive) selects the parser EXPLICITLY through
    :data:`registry.PARSER_REGISTRY`. When it is omitted — the historical call shape —
    routing is unchanged: ``detect_format()`` picks the envelope and the envelope picks
    the parser, so an existing client sees byte-identical behaviour.

    A declared PCS type (CALINF/BERMAN/VESPRO/VESARR/VESDEP) does NOT bypass per-message
    routing: one XML/LOG file may carry several message types, so the declaration becomes
    a per-document ASSERTION and a non-matching document is a typed row error.

    :raises UnknownDocumentType: ``document_type`` is not a registered value.
    :raises DocumentTypeMismatch: the declared type cannot arrive as the detected envelope.
    """
    fmt = detect_format(filename, content)

    declared: Optional[ParserSpec] = None
    if document_type is not None and str(document_type).strip():
        declared = resolve_by_document_type(document_type)      # -> UnknownDocumentType
        if fmt not in declared.formats:
            raise DocumentTypeMismatch(declared.document_type, fmt, declared.formats)

    # Whole-file parsers. Explicit declaration wins; otherwise fall back to the envelope
    # mapping, which reproduces the original per-format branch exactly.
    spec = declared if declared is not None else resolve_by_format(fmt)
    if spec is not None and not spec.per_document:
        return spec.load()(content, filename)

    # XML/LOG: per-message routing (unchanged). `expected` is set only when the client
    # declared a PCS type, and then acts as an assertion over each document.
    expected: Optional[str] = declared.document_type if declared is not None else None

    res = ParseResult()
    docs = extract_xml_documents(fmt, content)
    res.row_count = len(docs)
    if not docs:
        res.rejected = True
        res.err(None, None, "no_documents", f"no PCS XML found in {fmt} file")
        return res

    for i, doc in enumerate(docs, start=1):
        try:
            root: ET.Element = safe_fromstring(doc)
        except ET.ParseError as exc:
            res.err(i, None, "xml_parse_error", f"could not parse XML document {i}: {exc}")
            res.invalid_count += 1
            continue

        msg = _document_type_of(root)
        if expected is not None and msg != expected:
            res.err(i, "DocumentType", "document_type_mismatch",
                    f"declared document_type {expected} but document {i} is "
                    f"{msg or 'unknown'}", msg)
            res.invalid_count += 1
            continue

        parser = REGISTRY.get(msg or "")
        if parser is None:
            res.err(i, "DocumentType", "unsupported_message_type",
                    f"unsupported PCS message type: {msg or 'unknown'}", msg)
            res.invalid_count += 1
            continue

        try:
            records = parser(root, source_file=filename)
        except MarineParseError as exc:
            res.err(i, None, "parse_failed", str(exc))
            res.invalid_count += 1
            continue

        if not records:
            res.warn(i, None, "no_records", f"{msg}: no records extracted from document {i}")
        res.records.extend(records)

    return res
