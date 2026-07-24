"""Envelope detection + XML extraction — the first stage of the marine parser
framework. Pure: no I/O, no DB.

Three envelope shapes exist in the NLP-Marine corpus:

  * ``CSV``  — the existing vessel-call upload template.
  * ``XML``  — a direct PCS message file (CALINF / BERMAN / VESPRO).
  * ``LOG``  — a VESARR / VESDEP ``.log`` transmission log where the PCS XML is
               embedded inside a JSON ``ReqBody.XML`` string. One log may carry
               SEVERAL messages (multiple VIAs) — every embedded XML is returned.

Routing is by the ``<DocumentType>`` tag (see documents.py), not the extension —
VESARR and VESDEP share the ``.log`` extension but carry different root elements.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional

Format = Literal["CSV", "XML", "LOG"]

# A JSON string body for the "XML" key: text between quotes, honouring \" escapes.
_LOG_XML_RE = re.compile(r'"XML"\s*:\s*"((?:[^"\\]|\\.)*)"')
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)


def decode(content: bytes) -> str:
    """Decode upload bytes to text tolerantly (utf-8-sig → latin-1)."""
    if not content:
        return ""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def detect_format(filename: Optional[str], content: bytes) -> Format:
    """Classify an upload into CSV / XML / LOG.

    Content wins over extension: a ``.log`` that carries a ``ReqBody``/``XML`` JSON
    wrapper is LOG; anything opening with an XML tag is XML; a ``.csv`` is CSV.
    """
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return "CSV"
    text = decode(content)
    if '"ReqBody"' in text and '"XML"' in text:
        return "LOG"
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith("<?xml") or stripped.startswith("<"):
        return "XML"
    if name.endswith(".log"):
        return "LOG"
    # Last resort: if it looks like it embeds XML, treat as LOG, else XML.
    return "LOG" if ('"XML"' in text) else "XML"


def _strip_decl(xml: str) -> str:
    return _XML_DECL_RE.sub("", xml).strip()


def extract_xml_documents(fmt: Format, content: bytes) -> list[str]:
    """Return the raw XML string(s) carried by the upload.

    * XML → the whole file (declaration stripped) as one document.
    * LOG → every embedded ``ReqBody.XML`` payload, JSON-unescaped, in order.
    * CSV → [] (CSV is handled by the tabular parser, not here).
    """
    if fmt == "CSV":
        return []
    text = decode(content)
    if fmt == "XML":
        body = _strip_decl(text)
        return [body] if body else []
    # LOG: pull each embedded XML string and JSON-unescape it (\" \\ \/ \n …).
    docs: list[str] = []
    for m in _LOG_XML_RE.finditer(text):
        try:
            xml = json.loads(f'"{m.group(1)}"')
        except json.JSONDecodeError:
            # Fallback: manual unescape of the common sequences.
            xml = m.group(1).replace('\\"', '"').replace("\\/", "/").replace("\\n", "").replace("\\\\", "\\")
        xml = _strip_decl(xml)
        if xml:
            docs.append(xml)
    return docs
