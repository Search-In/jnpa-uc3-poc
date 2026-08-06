"""The 13-group consumption table: which upload service ingests each API
group, and how the service's dimension parameter (list_type / facility /
doc_type / entity) is recovered from the record + filename.

The API's fileRef is deliberately opaque, but GET /v2/files returns the real
corpus filename in Content-Disposition — and the dump-import services derive
their routing hints from filenames, so the same heuristics apply verbatim.

Groups with no consumer yet are routed UNROUTED: the record + raw bytes are
landed (api_record + file store) and replayed by ``replay_unrouted`` the
moment a consumer exists — nothing is dropped, nothing is re-downloaded.

Kinds: indexed (file-backed, routed here) · report (JSON, report_ingest) ·
static (bathymetry — never served by the API; the dump remains the source).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

log = get_logger("services.jnpa_sync.routing")

UPLOADED_BY = "jnpa-api"

REPORT_GROUPS = ("berthing-reports", "daily-reports")
STATIC_GROUPS = ("bathymetry",)
INDEXED_GROUPS = (
    "nlp-marine", "port-craft-pilot", "shipping-lines", "customs",
    "edi-messages", "gate-documents", "rail-fois", "rail-form11-icd",
    "transport", "cfs-ecy",
)
ALL_GROUPS = INDEXED_GROUPS + REPORT_GROUPS + STATIC_GROUPS


@dataclass
class RouteOutcome:
    service: str                    # consumer name, or UNROUTED/STATIC
    status: str                     # SUCCESS | PARTIAL | SKIPPED_DUPLICATE |
                                    # FAILED | REJECTED | UNROUTED
    file_id: Optional[int] = None   # the consumer's own ledger row id
    detail: Dict[str, Any] = field(default_factory=dict)


def _normalize(service: str, result: Dict[str, Any]) -> RouteOutcome:
    """Upload services return slightly different envelopes — normalise:
    status may be `status` or `import_status`; the ledger id may be
    `file_id`, `message_id` or `upload_id`; IMPORTED (performance-style)
    counts as SUCCESS."""
    status = str(result.get("status") or result.get("import_status")
                 or "FAILED").upper()
    if status == "IMPORTED":
        status = "SUCCESS"
    file_id = result.get("file_id") or result.get("message_id") \
        or result.get("upload_id")
    return RouteOutcome(service=service, status=status,
                        file_id=int(file_id) if file_id else None,
                        detail={k: result.get(k) for k in
                                ("imported", "updated", "skipped", "invalid",
                                 "duplicate_file", "duplicate", "summary",
                                 "error_detail")
                                if result.get(k) is not None})


# ------------------------------------------------- filename hint heuristics
def sl_list_type(filename: str) -> Optional[str]:
    """IAL / EAL / EDO from the shipping-lines filename (the dump layout
    encodes it: '4-Shipping Lines/IAL FORMAT/IAL NSICT.xlsx' etc.)."""
    upper = filename.upper()
    for list_type in ("IAL", "EAL", "EDO"):
        if list_type in upper:
            return list_type
    return None


def gate_doc_type(filename: str, message_type: Optional[str]) -> Optional[str]:
    """EIR / PIN / FORM13 from the record's messageType, else the filename
    stem (the corpus convention: eir1_psa_bmct, form13_nsict_egate,
    ticket1...)."""
    probe = f"{message_type or ''} {filename}".upper()
    if "EIR" in probe or "EQUIPMENT-INTERCHANGE" in probe:
        return "EIR"
    if "FORM13" in probe or "FORM 13" in probe:
        return "FORM13"
    if "PIN" in probe or "TICKET" in probe:
        return "PIN"
    return None


def transport_entity(filename: str) -> Optional[str]:
    upper = filename.upper()
    if "TRANSPORTER" in upper:
        return "TRANSPORTER"
    if "PDP" in upper or "DRIVER" in upper:
        return "DRIVER"
    return None


# nlp-marine messageTypes whose files are tabular DOUBLES of the PCS XML feed
# (live corpus; the XML is authoritative — see _route's nlp-marine branch).
_MARINE_REPORT_DOUBLES = frozenset({
    "eta", "berth-request", "pre-arrival-notifiaction", "vessel-profile",
    "voyage-registration", "expected-time-of-arrival", "loop",
})


def cfs_facility(filename: str) -> Optional[str]:
    upper = filename.upper()
    if "ECY" in upper:
        return "ECY"
    if "CFS" in upper:
        return "CFS"
    return None


_TABULAR_SUFFIXES = (".csv", ".txt", ".xls", ".xlsx", ".xlsm")


class JnpaRouter:
    """Lazy per-service routing. Services are constructed on first use with
    the shared DSN (the marine_imports.py lazy-singleton idiom); tests
    inject fakes via the ``services`` mapping."""

    def __init__(self, dsn: Optional[str] = None,
                 services: Optional[Dict[str, Any]] = None) -> None:
        self._dsn = dsn
        self._services: Dict[str, Any] = dict(services or {})

    def kind(self, group: str) -> str:
        if group in REPORT_GROUPS:
            return "report"
        if group in STATIC_GROUPS:
            return "static"
        return "indexed"

    # ------------------------------------------------------- lazy services
    def _service(self, name: str) -> Any:
        if name in self._services:
            return self._services[name]
        if name == "marine":
            from services.marine import MarineUploadService
            svc = MarineUploadService(dsn=self._dsn)
        elif name == "customs":
            from services.customs.service import CustomsService
            svc = CustomsService(dsn=self._dsn)
        elif name == "shipping_lines":
            from services.shipping_lines.upload_service import (
                ShippingLinesUploadService)
            svc = ShippingLinesUploadService(dsn=self._dsn)
        elif name == "cfs_ecy":
            from services.cfs_ecy.upload_service import CfsEcyUploadService
            svc = CfsEcyUploadService(dsn=self._dsn)
        elif name == "gate_documents":
            from services.gate_documents.service import GateDocumentService
            svc = GateDocumentService(dsn=self._dsn)
        elif name == "transporters_drivers":
            from services.transporters_drivers.upload_service import (
                TransportersDriversUploadService)
            svc = TransportersDriversUploadService(dsn=self._dsn)
        elif name == "rail_fois":
            from services.rail.fois_service import RailFoisService
            svc = RailFoisService(dsn=self._dsn)
        elif name == "rail_form11_icd":
            from services.rail.form11_icd_service import Form11IcdService
            svc = Form11IcdService(dsn=self._dsn)
        elif name == "edi_vessel":
            from services.edi_vessel import EdiVesselService
            svc = EdiVesselService(dsn=self._dsn)
        else:
            raise KeyError(f"unknown consumer service {name!r}")
        self._services[name] = svc
        return svc

    # ------------------------------------------------------------- routing
    async def route(self, group: str, *, filename: str, content: bytes,
                    message_type: Optional[str] = None) -> RouteOutcome:
        """Feed one downloaded file into its consumer. Never raises: any
        consumer failure comes back as a FAILED/REJECTED outcome (the record
        and raw bytes are already landed by the caller)."""
        try:
            return await self._route(group, filename=filename,
                                     content=content,
                                     message_type=message_type)
        except Exception as exc:  # noqa: BLE001 - outcome, not crash
            log.warning("jnpa_route_failed", group=group, filename=filename,
                        error=str(exc))
            return RouteOutcome(service=group, status="FAILED",
                                detail={"error": str(exc)})

    async def _route(self, group: str, *, filename: str, content: bytes,
                     message_type: Optional[str]) -> RouteOutcome:
        if group in ("nlp-marine", "port-craft-pilot"):
            if group == "nlp-marine":
                mt = (message_type or "").strip()
                up = filename.upper()
                # LIVE-corpus surprises (absent from the sample pack):
                # 1. FOIS train intimations delivered as messageType "JNPA"
                #    ("Port authority notice") — the rail consumer parses the
                #    CSV verbatim (verified against the live corpus).
                if "TRAIN_INTIMATION" in up:
                    svc = self._service("rail_fois")
                    result = await svc.import_file(content, filename,
                                                   UPLOADED_BY)
                    return _normalize("rail_fois", result)
                # 2. Tabular report DOUBLES of the PCS XML feed (berth-request
                #    / pre-arrival / vessel-Profile / voyage-registration /
                #    expected-time-of-arrival xlsx, ETA_ETD csv) plus the
                #    LOOP import-cohort csv and non-train JNPA notices.
                #    The XML messages are authoritative for the call spine;
                #    feeding these to the marine parsers only produced
                #    REJECTED noise. Land + keep replayable instead.
                if (mt.lower() in _MARINE_REPORT_DOUBLES
                        or up.startswith(("ETA_ETD", "LOOP_COHORT"))
                        or mt.upper() == "JNPA"):
                    return RouteOutcome(
                        service="marine", status="UNROUTED",
                        detail={"reason": "tabular report double / cohort "
                                          "extract — PCS XML is authoritative;"
                                          " no consumer wired",
                                "message_type": mt, "filename": filename})
            # The marine parser registry auto-detects the document type from
            # envelope + filename (PCS XML, pilot-card xlsx, port-craft PDF).
            svc = self._service("marine")
            result = await svc.import_file(content, filename, UPLOADED_BY)
            return _normalize("marine", result)

        if group == "customs":
            svc = self._service("customs")
            result = await svc.import_bytes(content, filename)
            return _normalize("customs", result)

        if group == "shipping-lines":
            mt = (message_type or "").upper()
            # The LIVE corpus also serves COPARN (empty-container release
            # orders, <ContainerRelease> XML) under this group — not in the
            # sample pack. Route to the vessel-side EDI consumer.
            if mt == "COPARN" or b"<ContainerRelease" in content[:2048]:
                svc = self._service("edi_vessel")
                result = await svc.import_file(content, filename, UPLOADED_BY)
                return _normalize("edi_vessel", result)
            list_type = sl_list_type(filename)
            if list_type is None:
                return RouteOutcome(
                    service="shipping_lines", status="UNROUTED",
                    detail={"reason": "no IAL/EAL/EDO hint in filename",
                            "filename": filename})
            svc = self._service("shipping_lines")
            result = await svc.import_file(list_type, content, filename,
                                           UPLOADED_BY)
            return _normalize("shipping_lines", result)

        if group == "cfs-ecy":
            facility = cfs_facility(filename)
            if facility is None:
                return RouteOutcome(
                    service="cfs_ecy", status="UNROUTED",
                    detail={"reason": "no CFS/ECY hint in filename",
                            "filename": filename})
            svc = self._service("cfs_ecy")
            result = await svc.import_file(facility, content, filename,
                                           UPLOADED_BY)
            return _normalize("cfs_ecy", result)

        if group == "gate-documents":
            doc_type = gate_doc_type(filename, message_type)
            suffix = ("." + filename.rsplit(".", 1)[-1].lower()
                      if "." in filename else "")
            if doc_type is None or suffix not in _TABULAR_SUFFIXES:
                # Single-document JSON/XML/photo payloads have no tabular
                # reader today — land + replay when one exists.
                return RouteOutcome(
                    service="gate_documents", status="UNROUTED",
                    detail={"reason": "non-tabular gate document or no "
                                      "EIR/PIN/FORM13 hint",
                            "filename": filename,
                            "message_type": message_type})
            svc = self._service("gate_documents")
            result = await svc.import_file(doc_type, content, filename,
                                           UPLOADED_BY)
            return _normalize("gate_documents", result)

        if group == "transport":
            entity = transport_entity(filename)
            if entity is None:
                return RouteOutcome(
                    service="transporters_drivers", status="UNROUTED",
                    detail={"reason": "no TRANSPORTER/DRIVER hint",
                            "filename": filename})
            svc = self._service("transporters_drivers")
            result = await svc.import_file(entity, content, filename,
                                           UPLOADED_BY)
            return _normalize("transporters_drivers", result)

        if group == "rail-fois":
            # FOIS Train Intimation CSV → services/rail FOIS consumer.
            svc = self._service("rail_fois")
            result = await svc.import_file(content, filename, UPLOADED_BY)
            return _normalize("rail_fois", result)

        if group == "rail-form11-icd":
            # Form 11 XLSX + CTO TXT → services/rail Form11/CTO consumer;
            # ICD daily-report PDFs come back REJECTED (UNSUPPORTED_FORMAT).
            svc = self._service("rail_form11_icd")
            result = await svc.import_file(content, filename, UPLOADED_BY)
            return _normalize("rail_form11_icd", result)

        if group == "edi-messages":
            # Live corpus (verified against dt.jnpa.in): CFS-CODECO /
            # ECY-CODECO gate-move workbooks (same shape as the cfs-ecy
            # group) and bare CODECO XML gate move reports (the raw-XML
            # seam in ShippingLinesUploadService). COARRI/COPRAR remain
            # UNROUTED — no consumer exists (vessel discharge/load ops).
            mt = (message_type or "").upper()
            if "CODECO" in mt and mt != "CODECO":
                facility = cfs_facility(filename) or (
                    "CFS" if "CFS" in mt else "ECY" if "ECY" in mt else None)
                if facility is not None:
                    svc = self._service("cfs_ecy")
                    result = await svc.import_file(facility, content,
                                                   filename, UPLOADED_BY)
                    return _normalize("cfs_ecy", result)
            if mt == "CODECO" or b"<CODECODetails" in content:
                svc = self._service("shipping_lines")
                result = await svc.import_file("EDO", content, filename,
                                               UPLOADED_BY)
                return _normalize("shipping_lines", result)
            if (mt in ("COARRI", "COPRAR", "COPARN")
                    or b"<ContLoadingNDischargeOder" in content[:2048]
                    or b"<AdvContainerList" in content[:2048]
                    or b"<ContainerRelease" in content[:2048]):
                # Vessel-side container documents → services.edi_vessel
                # (core.edi_vessel_container, migration 0125).
                svc = self._service("edi_vessel")
                result = await svc.import_file(content, filename, UPLOADED_BY)
                return _normalize("edi_vessel", result)
            return RouteOutcome(service=group, status="UNROUTED",
                                detail={"reason": "no consumer for "
                                                  f"{mt or 'unknown'} yet",
                                        "filename": filename})

        return RouteOutcome(service=group, status="UNROUTED",
                            detail={"reason": f"unknown group {group!r}"})


__all__ = [
    "JnpaRouter", "RouteOutcome", "UPLOADED_BY",
    "ALL_GROUPS", "INDEXED_GROUPS", "REPORT_GROUPS", "STATIC_GROUPS",
    "sl_list_type", "gate_doc_type", "transport_entity", "cfs_facility",
]
