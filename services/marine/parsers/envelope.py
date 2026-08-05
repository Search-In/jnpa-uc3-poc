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

import io
import json
import re
from typing import Literal, Optional

Format = Literal["CSV", "XML", "LOG", "XLSX", "PDF", "SHP", "JSON", "JOURNAL"]

# PCS message-journal signature. The NLP Inbound/Outbound Data Reports are CSVs whose
# real header sits BELOW a title banner (row 7 in the corpus), and whose REQUEST column
# carries the PCS message itself — raw XML in the inbound report, JSON-wrapped
# (``ReqBody.XML``) in the outbound one. Detected by column signature so an ordinary
# vessel-call template CSV can never match.
_JOURNAL_MARKERS = ("COMMON_REF_NO", "MESSAGE_TYPE", "REQUEST")
#: Rows scanned for the real header before giving up (corpus: banner + 5 filter rows).
_JOURNAL_HEADER_SCAN = 12

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
        # A PCS message journal is a CSV by extension but a message CARRIER by content:
        # its rows hold whole PCS documents. Content wins, exactly as it does for .log.
        head = decode(content[:64 * 1024])
        return "JOURNAL" if all(m in head for m in _JOURNAL_MARKERS) else "CSV"
    # PDF magic (%PDF) — the port-craft roster (Details_of_Port_Crafts.pdf).
    if content[:5] == b"%PDF-" or name.endswith(".pdf"):
        return "PDF"
    # A bare ESRI .shp starts with the big-endian file code 9994 (0x0000270A).
    if content[:4] == b"\x00\x00\x27\x0a" or name.endswith(".shp"):
        return "SHP"
    # ZIP (PK\x03\x04) is ambiguous: a shapefile bundle OR an xlsx. Peek the entries —
    # a member ending in .shp means a shapefile bundle; otherwise it's a spreadsheet.
    if content[:4] == b"PK\x03\x04":
        try:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if any(n.lower().endswith(".shp") for n in z.namelist()):
                    return "SHP"
        except Exception:  # noqa: BLE001 — not a readable zip → fall through to XLSX
            pass
        return "XLSX"
    # Legacy xls is an OLE compound file (\xD0\xCF\x11\xE0).
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return "XLSX"
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return "XLSX"
    text = decode(content)
    if '"ReqBody"' in text and '"XML"' in text:
        return "LOG"
    stripped = text.lstrip("﻿ \t\r\n")
    # Canonical bathymetry JSON. Probed AFTER the LOG check on purpose: a VESARR/VESDEP
    # transmission log is ALSO JSON, and its ReqBody/XML signature must keep winning.
    # Before this branch a .json upload was misdetected as XML and died with
    # `xml_parse_error`, so this can only affect uploads that already failed.
    if name.endswith(".json") or stripped.startswith("{") or stripped.startswith("["):
        return "JSON"
    if stripped.startswith("<?xml") or stripped.startswith("<"):
        return "XML"
    if name.endswith(".log"):
        return "LOG"
    # Last resort: if it looks like it embeds XML, treat as LOG, else XML.
    return "LOG" if ('"XML"' in text) else "XML"


def _strip_decl(xml: str) -> str:
    return _XML_DECL_RE.sub("", xml).strip()


def journal_header_index(rows: list[list[str]]) -> int:
    """Index of the REAL header row in a PCS journal, or -1.

    The corpus files open with a title banner and five filter rows ('From Date:', …)
    before the header, so ``csv.DictReader``'s row-0 assumption picks up
    ``['NLP Inbound Data Report', '', …]`` and every column lookup misses. Pure: pick the
    first row inside the scan window that carries the journal's signature columns.
    """
    for i, row in enumerate(rows[:_JOURNAL_HEADER_SCAN]):
        cells = {c.strip().upper() for c in row}
        if all(m in cells for m in _JOURNAL_MARKERS):
            return i
    return -1


def _journal_documents(text: str) -> list[str]:
    """Every PCS document carried by a journal's REQUEST column, in file order.

    Handles BOTH corpus shapes without guessing: the outbound report wraps the message in
    a pseudo-JSON envelope (``ReqBody.XML``) that is NOT valid JSON — it uses semicolons
    as separators — so the embedded payload is taken with the same regex the .log path
    uses rather than by ``json.loads``. The inbound report carries the XML raw.
    """
    import csv as _csv

    _csv.field_size_limit(1 << 30)  # a REQUEST cell holds a whole PCS document
    rows = list(_csv.reader(io.StringIO(text)))
    hi = journal_header_index(rows)
    if hi < 0:
        return []
    header = [c.strip().upper() for c in rows[hi]]
    try:
        req = header.index("REQUEST")
    except ValueError:  # pragma: no cover — guarded by journal_header_index
        return []

    docs: list[str] = []
    for row in rows[hi + 1:]:
        if len(row) <= req:
            continue
        cell = (row[req] or "").strip()
        if not cell:
            continue
        m = _LOG_XML_RE.search(cell)
        if m:
            try:
                xml = json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                xml = (m.group(1).replace('\\"', '"').replace("\\/", "/")
                       .replace("\\n", "").replace("\\\\", "\\"))
        else:
            xml = cell  # inbound report: the message sits in the cell verbatim
        xml = _strip_decl(xml)
        if xml.startswith("<"):
            docs.append(xml)
    return docs


def extract_xml_documents(fmt: Format, content: bytes) -> list[str]:
    """Return the raw XML string(s) carried by the upload.

    * XML     → the whole file (declaration stripped) as one document.
    * LOG     → every embedded ``ReqBody.XML`` payload, JSON-unescaped, in order.
    * JOURNAL → every PCS document in the REQUEST column of a message journal.
    * CSV     → [] (CSV is handled by the tabular parser, not here).
    """
    if fmt == "CSV":
        return []
    text = decode(content)
    if fmt == "JOURNAL":
        return _journal_documents(text)
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
