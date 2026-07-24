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
from .vesarr_vesdep import parse_vesarr, parse_vesdep
from .vespro import parse_vespro

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
]


def _tag_csv(res: ParseResult) -> ParseResult:
    """Tag CSV records with the same discriminators the XML parsers emit, so every
    record leaving this framework has a ``_target``/``_message`` regardless of format."""
    for rec in res.records:
        rec.setdefault("_target", "vessel_call")
        rec.setdefault("_message", "CSV")
    return res


def parse_marine(content: bytes, filename: Optional[str] = None) -> ParseResult:
    """Parse one uploaded marine file into normalized records (ParseResult).

    Format-agnostic: CSV delegates to the existing tabular parser; XML/LOG are routed
    per message type. A single LOG may yield several messages; each unsupported or
    malformed document becomes a typed row error rather than an exception, exactly
    like the CSV path.
    """
    fmt = detect_format(filename, content)

    if fmt == "CSV":
        header, rows = read_rows_from_bytes(content, filename)
        return _tag_csv(_csv_parse(header, rows, source_file=filename))

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

        msg = document_type(root)
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
