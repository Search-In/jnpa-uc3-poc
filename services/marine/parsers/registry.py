"""Marine parser REGISTRY — the single table mapping a document type to its parser.

Replaces the per-format ``if fmt == …`` chain that ``parse_marine`` used to carry, so
adding a source is ONE registry entry rather than another branch. Pure: no DB, no API,
no I/O at import time.

Two lookup directions, both landing on the same table:

  * ``resolve_by_document_type(dt)`` — EXPLICIT routing, used when a client declares
    ``document_type`` on /api/marine/{validate,upload}.
  * ``resolve_by_format(fmt)``       — IMPLICIT routing, the historical behaviour:
    ``envelope.detect_format()`` picks the envelope, the envelope picks the parser.

PCS message types (CALINF / BERMAN / VESPRO / VESARR / VESDEP) are marked
``per_document=True`` and carry NO loader: a single XML/LOG file may legitimately hold
several messages of different types, so routing stays PER MESSAGE inside ``parse_marine``
via the existing ``REGISTRY`` in ``__init__``. A declared PCS type is therefore an
ASSERTION over each document, never a bypass of per-message routing.

Parser loading is LAZY (``ParserSpec.load()``). The port-craft and pilot-card parsers pull
in pdfplumber / openpyxl transitively, and importing this module must stay free of those
optional dependencies so a gateway without them still serves the XML/CSV paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..upload_parsers import ParseResult

# (content, filename) -> ParseResult — the uniform parser signature.
ParserFn = Callable[[bytes, Optional[str]], ParseResult]


class DocumentTypeError(ValueError):
    """Base for the two client-supplied ``document_type`` faults."""


class UnknownDocumentType(DocumentTypeError):
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.accepted = known_document_types()
        super().__init__(f"unknown document_type {raw!r}; accepted: {', '.join(self.accepted)}")


class DocumentTypeMismatch(DocumentTypeError):
    def __init__(self, declared: str, detected: str, expected: tuple[str, ...]) -> None:
        self.declared = declared
        self.detected = detected
        self.expected = expected
        super().__init__(
            f"declared document_type {declared} expects envelope {'/'.join(expected)}, "
            f"but the uploaded file was detected as {detected}")


# --------------------------------------------------------------------------- loaders
# Each returns the parser callable; imported on demand so optional deps stay optional.

def _load_vessel_call_csv() -> ParserFn:
    def _parse(content: bytes, filename: Optional[str]) -> ParseResult:
        # Adapter: the CSV parser is the only two-step one (read rows, then parse), and
        # the only one whose records are tagged by the framework rather than by itself.
        from . import _tag_csv  # lazy: the package is fully initialised by call time
        from ..upload_parsers import parse as _csv_parse, read_rows_from_bytes
        header, rows = read_rows_from_bytes(content, filename)
        return _tag_csv(_csv_parse(header, rows, source_file=filename))
    return _parse


def _load_pilot_card() -> ParserFn:
    from .pilot_card_xlsx import parse_pilot_card
    return parse_pilot_card


def _load_port_craft() -> ParserFn:
    from .port_craft_pdf import parse_port_craft_pdf
    return parse_port_craft_pdf


def _load_sea_channel() -> ParserFn:
    from .sea_channel_shp import parse_sea_channel_shp
    return parse_sea_channel_shp


# --------------------------------------------------------------------------- spec
@dataclass(frozen=True)
class ParserSpec:
    """One routable marine source.

    ``formats`` is the set of envelope formats (``envelope.Format``) this document type
    may legitimately arrive as — it is what the declared-vs-detected mismatch guard
    checks against. ``loader`` is None exactly when ``per_document`` is True.
    """
    document_type: str
    formats: tuple[str, ...]
    loader: Optional[Callable[[], ParserFn]] = None
    per_document: bool = False
    aliases: tuple[str, ...] = field(default=())

    def load(self) -> ParserFn:
        if self.loader is None:
            raise RuntimeError(
                f"{self.document_type} is routed per document; it has no whole-file parser")
        return self.loader()


_PCS_FORMATS = ("XML", "LOG")


def _pcs(document_type: str) -> ParserSpec:
    """A PCS message type: per-message routed, no whole-file parser."""
    return ParserSpec(document_type=document_type, formats=_PCS_FORMATS, per_document=True)


PARSER_REGISTRY: dict[str, ParserSpec] = {
    # --- whole-file parsers (one source per envelope format, today) ---
    "VESSEL_CALL_CSV": ParserSpec(
        "VESSEL_CALL_CSV", ("CSV",), _load_vessel_call_csv,
        aliases=("CSV", "VESSEL_CALL", "VESSELCALL")),
    "PILOTAGE": ParserSpec(
        "PILOTAGE", ("XLSX",), _load_pilot_card,
        aliases=("PILOT_CARD", "PILOTCARD", "PILOT")),
    "PORT_CRAFT": ParserSpec(
        "PORT_CRAFT", ("PDF",), _load_port_craft,
        aliases=("PORTCRAFT", "PORT_CRAFT_PDF")),
    "SEA_CHANNEL": ParserSpec(
        "SEA_CHANNEL", ("SHP",), _load_sea_channel,
        aliases=("SEACHANNEL", "SEA_CHANNELS", "SHP")),
    # --- PCS message types: per-message routed inside parse_marine ---
    "CALINF": _pcs("CALINF"),
    "BERMAN": _pcs("BERMAN"),
    "VESPRO": _pcs("VESPRO"),
    "VESARR": _pcs("VESARR"),
    "VESDEP": _pcs("VESDEP"),
}

# Envelope format -> canonical document type, for the IMPLICIT (historical) path.
# XML/LOG are deliberately ABSENT: they fall through to per-message routing, exactly as
# the original `if fmt == …` chain did.
FORMAT_TO_DOCUMENT_TYPE: dict[str, str] = {
    "CSV": "VESSEL_CALL_CSV",
    "XLSX": "PILOTAGE",
    "PDF": "PORT_CRAFT",
    "SHP": "SEA_CHANNEL",
}

# alias (normalised) -> canonical, built once from the specs above.
_ALIASES: dict[str, str] = {}
for _canon, _spec in PARSER_REGISTRY.items():
    for _alias in _spec.aliases:
        _ALIASES[_alias] = _canon
del _canon, _spec, _alias  # keep the module namespace clean


# --------------------------------------------------------------------------- lookup
def normalise_document_type(raw: str) -> str:
    """Client spelling -> canonical key. ``port-craft`` / ``Port Craft`` -> ``PORT_CRAFT``."""
    return "_".join(str(raw or "").strip().upper().replace("-", "_").replace(" ", "_").split("_"))


def known_document_types() -> tuple[str, ...]:
    """Canonical, client-facing accepted values (aliases are accepted but not advertised)."""
    return tuple(sorted(PARSER_REGISTRY))


def resolve_by_document_type(raw: str) -> ParserSpec:
    """EXPLICIT routing. Raises :class:`UnknownDocumentType` for an unroutable value."""
    key = normalise_document_type(raw)
    spec = PARSER_REGISTRY.get(key) or PARSER_REGISTRY.get(_ALIASES.get(key, ""))
    if spec is None:
        raise UnknownDocumentType(raw)
    return spec


def resolve_by_format(fmt: str) -> Optional[ParserSpec]:
    """IMPLICIT routing. None ⇒ no whole-file parser for this envelope (XML/LOG)."""
    canon = FORMAT_TO_DOCUMENT_TYPE.get(fmt)
    return PARSER_REGISTRY[canon] if canon else None


def formats_for(raw: str) -> tuple[str, ...]:
    """Envelope formats a document type may arrive as (mismatch-guard helper)."""
    return resolve_by_document_type(raw).formats
