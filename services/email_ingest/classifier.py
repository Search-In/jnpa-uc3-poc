"""Attachment -> existing UC3 master table.

This is an ORCHESTRATION layer, not a new parser. Format detection and parser
selection are delegated wholesale to the shipped marine pipeline:

    services/marine/parsers/envelope.py :: detect_format   (content beats extension)
    services/marine/parsers/registry.py :: resolve_by_format / resolve_by_document_type

and the gate-document target tables are IMPORTED from their owning module
(``services/gate_documents/repository.TABLES``) rather than restated, so they
cannot drift from the real schema.

WHY A MAPPING TABLE AT ALL
    The registry knows which PARSER handles a document type; it does not record
    which MASTER TABLE the parser ultimately writes. The page has to tell the
    operator where data will land BEFORE importing, so that one fact is recorded
    here — verified against the DDL, with the defining file cited per row.

CONFIDENCE RULE
    A route is only returned when the detected content agrees with the subject
    hint (or there is no hint). Disagreement, an unknown envelope, or a document
    type with no recorded master table all yield ``NEEDS_REVIEW`` — never a
    guessed insert. That is the "do not put uncertain data in the wrong table"
    requirement, enforced in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Target master table per document type. Every entry verified against the DDL:
#   core.vessel_call        gateway/marine_ext.py:50
#   core.pilotage           gateway/marine_ext.py:251
#   core.port_craft         gateway/marine_ext.py:464
#   core.sea_channel        gateway/marine_ext.py:281
#   core.bathymetry_survey  gateway/marine_ext.py:520
_MARINE_MASTER: Dict[str, str] = {
    "VESSEL_CALL_CSV": "core.vessel_call",
    "PILOTAGE": "core.pilotage",
    "PORT_CRAFT": "core.port_craft",
    "SEA_CHANNEL": "core.sea_channel",
    "BATHYMETRY": "core.bathymetry_survey",
    # PCS message types are routed per message inside parse_marine and all land in
    # the vessel-call family (call header + its events).
    "CALINF": "core.vessel_call",
    "CALINV": "core.vessel_call",
    "BERMAN": "core.vessel_call",
    "BERALT": "core.vessel_call",
    "VESPRO": "core.vessel_call",
    "VESARR": "core.vessel_call",
    "VESDEP": "core.vessel_call",
    "ACKPLM": "core.pilotage",
}

#: Subject keyword -> declared document type. The subject is a HINT from the
#: sender, never the decision: content always wins, and a conflict is a review.
_SUBJECT_HINTS: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("VESSEL CALL", "VESSELCALL", "VESSEL-CALL"), "MARINE", "VESSEL_CALL_CSV"),
    (("PILOT CARD", "PILOTCARD", "PILOTAGE"), "MARINE", "PILOTAGE"),
    (("PORT CRAFT", "PORTCRAFT"), "MARINE", "PORT_CRAFT"),
    (("BATHYMETRY", "SOUNDING"), "MARINE", "BATHYMETRY"),
    (("SEA CHANNEL", "SEACHANNEL"), "MARINE", "SEA_CHANNEL"),
    (("EIR",), "GATE_DOC", "EIR"),
    (("PIN TICKET", "PIN-TICKET"), "GATE_DOC", "PIN"),
    (("FORM 13", "FORM13"), "GATE_DOC", "FORM13"),
)

DOMAIN_MARINE = "MARINE"
DOMAIN_GATE_DOC = "GATE_DOC"


@dataclass
class Route:
    """Where one attachment should go, and how confident we are."""

    filename: str
    detected_format: Optional[str] = None
    domain: Optional[str] = None
    document_type: Optional[str] = None
    master_table: Optional[str] = None
    confident: bool = False
    #: Machine token + human sentence for the NEEDS_REVIEW / UNSUPPORTED case.
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    #: Tables this content might plausibly belong to, when ambiguous.
    candidates: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "detected_format": self.detected_format,
            "domain": self.domain,
            "document_type": self.document_type,
            "master_table": self.master_table,
            "confident": self.confident,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


def _gate_doc_tables() -> Dict[str, str]:
    """Gate-document targets, read from the module that owns them."""
    try:
        from services.gate_documents.repository import TABLES
        return dict(TABLES)
    except Exception:  # noqa: BLE001 — keep the classifier importable standalone
        return {}


def subject_hint(subject: str) -> Optional[Tuple[str, str]]:
    """``(domain, document_type)`` declared by the subject line, if any."""
    upper = (subject or "").upper()
    for keywords, domain, doc_type in _SUBJECT_HINTS:
        if any(k in upper for k in keywords):
            return domain, doc_type
    return None


def classify(filename: str, content: bytes, subject: str = "") -> Route:
    """Decide the master table for ONE attachment.

    Never raises: an undecidable attachment comes back as a non-confident Route
    carrying the reason, which the service records as NEEDS_REVIEW.
    """
    route = Route(filename=filename or "attachment")
    if not content:
        route.reason_code = "empty_attachment"
        route.reason = "The attachment is empty (0 bytes)."
        return route

    hint = subject_hint(subject)

    # ---- 1. envelope detection (content beats extension) --------------------
    try:
        from services.marine.parsers.envelope import detect_format
        fmt = detect_format(filename, content)
    except Exception:  # noqa: BLE001
        fmt = None
    route.detected_format = fmt

    # ---- 2. gate-document hint: no envelope detector of its own -------------
    # Gate documents are CSV/XLS/XLSX and share the CSV/XLSX envelope with the
    # marine sources, so they are only routed when the SUBJECT declares them.
    gate_tables = _gate_doc_tables()
    if hint and hint[0] == DOMAIN_GATE_DOC:
        doc_type = hint[1]
        table = gate_tables.get(doc_type)
        if not table:
            route.reason_code = "unknown_gate_doc_type"
            route.reason = f"No master table is registered for gate document {doc_type}."
            return route
        if (filename or "").lower().endswith((".csv", ".xls", ".xlsx", ".xlsm")):
            route.domain, route.document_type = DOMAIN_GATE_DOC, doc_type
            route.master_table, route.confident = table, True
            return route
        route.reason_code = "format_mismatch"
        route.reason = (f"The subject declares a {doc_type} gate document, which must be "
                        f"CSV/XLS/XLSX, but the attachment is {fmt or 'an unrecognised format'}.")
        route.candidates = [table]
        return route

    if not fmt:
        route.reason_code = "unknown_format"
        route.reason = ("The attachment format could not be recognised. Supported: "
                        "CSV, XLSX, XML, LOG, JSON, PDF, SHP.")
        return route

    # ---- 3. marine registry routing -----------------------------------------
    try:
        from services.marine.parsers import registry as R
    except Exception:  # noqa: BLE001
        route.reason_code = "registry_unavailable"
        route.reason = "The document registry could not be loaded on the server."
        return route

    declared = hint[1] if (hint and hint[0] == DOMAIN_MARINE) else None
    if declared:
        try:
            spec = R.resolve_by_document_type(declared)
        except Exception:  # noqa: BLE001 — unknown declared type
            spec = None
        if spec is None:
            route.reason_code = "unknown_document_type"
            route.reason = f"The subject declares {declared}, which is not a known document type."
            return route
        if fmt not in spec.formats:
            # The sender said one thing and the bytes say another — exactly the
            # DocumentTypeMismatch guard the upload API applies, surfaced as a
            # review rather than a bad import.
            route.reason_code = "declared_vs_detected"
            route.reason = (f"The subject declares {declared} (expects "
                            f"{'/'.join(spec.formats)}) but the attachment is {fmt}.")
            route.candidates = [t for t in {_MARINE_MASTER.get(declared, ""),
                                            _MARINE_MASTER.get(
                                                R.FORMAT_TO_DOCUMENT_TYPE.get(fmt, ""), "")} if t]
            return route
        return _resolved(route, DOMAIN_MARINE, spec.document_type)

    # No usable hint: fall back to the registry's own implicit routing.
    spec = R.resolve_by_format(fmt, content, filename)
    if spec is None:
        # XML/LOG/JOURNAL have no whole-file parser — they are PCS carriers routed
        # per message by parse_marine, which is still a confident marine route.
        if fmt in ("XML", "LOG", "JOURNAL"):
            route.domain = DOMAIN_MARINE
            route.document_type = None          # decided per message downstream
            route.master_table = "core.vessel_call"
            route.confident = True
            return route
        route.reason_code = "no_parser"
        route.reason = f"No importer is registered for {fmt} attachments."
        return route
    return _resolved(route, DOMAIN_MARINE, spec.document_type)


def _resolved(route: Route, domain: str, document_type: str) -> Route:
    table = _MARINE_MASTER.get(document_type)
    if not table:
        route.domain, route.document_type = domain, document_type
        route.reason_code = "no_master_table"
        route.reason = (f"{document_type} was recognised but no master table is recorded "
                        "for it, so it cannot be imported automatically.")
        return route
    route.domain, route.document_type = domain, document_type
    route.master_table, route.confident = table, True
    return route


def known_master_tables() -> List[str]:
    """Every master table this layer can currently target (for the UI legend)."""
    return sorted({*_MARINE_MASTER.values(), *_gate_doc_tables().values()})


__all__ = ["DOMAIN_GATE_DOC", "DOMAIN_MARINE", "Route", "classify",
           "known_master_tables", "subject_hint"]
