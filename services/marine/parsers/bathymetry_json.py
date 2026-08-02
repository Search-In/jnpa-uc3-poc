"""Canonical bathymetry JSON -> sounding records. Pure, no DB.

The JSON arm of the bathymetry pipeline. It does NOT define a shape of its own: it decodes
the upload and hands the survey header + sounding list to
:func:`bathymetry_model.emit_document`, which is the same function the PDF parser will call
once chart extraction lands. That is what guarantees the two sources stay identical.

Accepted payloads (all decode to one canonical document):

  1. ``{"document_type": "BATHYMETRY", "survey": {...}, "soundings": [...]}``  — canonical
  2. ``{"survey": {...}, "soundings": [...]}``                                — document_type optional
  3. ``{"drawing_no": "...", "soundings": [...]}``                            — flat survey header

A JSON array at the top level is rejected with a typed error rather than guessed at: a bare
list carries no ``drawing_no``, so its soundings could not be attributed to a survey.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from ..upload_parsers import ParseResult
from .bathymetry_model import DOCUMENT_TYPE, SURVEY_FIELDS, emit_document


def _decode(content: bytes) -> Any:
    text = content.decode("utf-8-sig") if content[:3] == b"\xef\xbb\xbf" else content.decode("utf-8")
    return json.loads(text)


def _survey_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the survey header, accepting either a nested `survey` object or flat keys."""
    nested = doc.get("survey")
    if isinstance(nested, Mapping):
        return dict(nested)
    return {k: doc[k] for k in SURVEY_FIELDS if k in doc}


def parse_bathymetry_json(content: bytes, filename: Optional[str] = None) -> ParseResult:
    """Parse a canonical bathymetry JSON upload into sounding records."""
    res = ParseResult()

    try:
        doc = _decode(content)
    except UnicodeDecodeError as exc:
        res.rejected = True
        res.err(None, None, "unreadable_json", f"could not decode JSON as UTF-8: {exc}")
        return res
    except json.JSONDecodeError as exc:
        res.rejected = True
        res.err(None, None, "invalid_json", f"could not parse JSON: {exc}")
        return res

    if not isinstance(doc, Mapping):
        res.rejected = True
        res.err(None, None, "not_a_document",
                "expected a JSON object with survey + soundings; a bare "
                f"{type(doc).__name__} carries no drawing_no to attribute soundings to")
        return res

    declared = str(doc.get("document_type") or DOCUMENT_TYPE).strip().upper()
    if declared != DOCUMENT_TYPE:
        res.rejected = True
        res.err(None, "document_type", "wrong_document_type",
                f"document_type {declared} is not {DOCUMENT_TYPE}")
        return res

    soundings = doc.get("soundings")
    if not isinstance(soundings, list):
        res.rejected = True
        res.err(None, "soundings", "missing_soundings",
                "expected a `soundings` array")
        return res

    return emit_document(_survey_of(doc), soundings, filename=filename, res=res)


__all__ = ["parse_bathymetry_json"]
