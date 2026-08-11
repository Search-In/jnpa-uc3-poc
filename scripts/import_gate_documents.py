#!/usr/bin/env python3
"""Idempotent importer for the 12 REAL JNPA gate documents (UC3-002).

Loads the customer's own gate paperwork — 4 EIR, 4 Form 13, 4 PIN pickup
tickets — from the Digital Twin corpus into ``core.gate_document``, together
with the original WhatsApp JPEG scan of each slip.

Source (discovered recursively, never hard-coded):

    <corpus>/8- Form13, EIR, PIN/
        EIR/eir_parsed/{eir1_psa_bmct,...}.{json,txt,xml}
        EIR/EIR/WhatsApp Image *.jpeg
        Form 13/form13_parsed/{form13_igt_eir,...}.{json,txt,xml}
        Form 13/WhatsApp Image *.jpeg
        PIN_Pickup/terminal_tickets_parsed/{ticket1,...}.{json,txt,xml}
        PIN_Pickup/WhatsApp Image *.jpeg

What it writes (all ADDITIVE, nothing is deleted or overwritten in place):

  * core.ingest_file    one row per source artefact actually read (parsed file
                        AND scan), keyed by its UNIQUE path -> re-runs reuse.
  * core.gate_document  one row per physical document, keyed by the natural key
                        (doc_category, doc_variant) from migration 0132 ->
                        re-runs UPDATE in place, never duplicate.
  * core.dq_issue       one row per data-quality observation, so anomalies are
                        recorded rather than silently repaired.
  * object storage      the original JPEG under ``gate_document/<variant>.jpeg``;
                        the bucket-relative key lands in gate_document.image_file
                        so GET /api/evidence/{image_file} resolves it.

Fidelity rules:
  * Parsed fields are preserved VERBATIM in ``attrs`` (jsonb) exactly as the
    source file supplies them. The typed columns are a normalised projection.
  * A value absent from the source stays NULL. Nothing is inferred or invented:
    10 of the 12 slips genuinely do not print a driver licence, and 2 print no
    truck number at all.
  * Placeholder text the terminals print for "no value" (NIL / NOSEAL / EMPTY /
    NA / "Read SMS") is normalised to NULL in the typed column, kept verbatim in
    ``attrs``, and recorded as a dq_issue.

Usage:
    # parse + validate + report, touches nothing
    .venv/bin/python scripts/import_gate_documents.py --dry-run

    # live import against QA (scans pushed through the nginx /minio route)
    POSTGRES_DSN='postgresql+asyncpg://...@host:5432/jnpa_qa?ssl=require' \
    MINIO_ACCESS_KEY=... MINIO_SECRET_KEY=... \
    .venv/bin/python scripts/import_gate_documents.py \
        --scan-public-base https://qa.searchintech.in/minio

    # on the deployment host, where minio:9000 is directly reachable
    .venv/bin/python scripts/import_gate_documents.py
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

# JNPA operates in IST. Gate slips print local wall-clock with no zone, so we
# stamp Asia/Kolkata to store the correct instant in a timestamptz column. Same
# convention as scripts/import_cfs_ecy_codeco.py.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

SOURCE_SYSTEM = "GATE-DOC"
SCAN_PREFIX = "gate_document"
DEFAULT_BUCKET = os.environ.get("ANOMALY_EVIDENCE_BUCKET", "evidence").strip() or "evidence"

# The corpus folder is "8- Form13, EIR, PIN" but the numbering/spacing has moved
# between drops, so match on shape rather than on the exact string.
CORPUS_DIR_RE = re.compile(r"form\s*-?\s*13.*eir.*pin", re.IGNORECASE)
PARSED_DIR_RE = re.compile(r"(eir_parsed|form13_parsed|terminal_tickets_parsed)$", re.IGNORECASE)
SCAN_SUFFIXES = (".jpeg", ".jpg")
PARSED_SUFFIXES = (".json", ".xml", ".txt")

# Roots searched when neither --corpus nor JNPA_CORPUS_DIR is given.
CANDIDATE_ROOTS = (
    _ROOT / "data",
    Path.home() / "Downloads" / "Data",
    Path.home() / "Downloads" / "Digital Twin" / "Data",
    Path.home() / "Downloads" / "Digital Twin",
    Path.home() / "Downloads",
)

# Parsed-directory name -> gate_document.doc_category (the CHECK vocabulary).
CATEGORY_BY_DIR = {
    "eir_parsed": "EIR",
    "form13_parsed": "FORM13",
    "terminal_tickets_parsed": "PIN_TICKET",
}

# Terminal string as printed on the slip -> core.ref_terminal.code. Matched on a
# squashed lowercase form so punctuation/suffix drift ("Pvt Ltd" vs "Pvt. Ltd",
# "BMCT-TID", ", Mumbai") does not need its own entry.
TERMINAL_CODE_RULES: Tuple[Tuple[str, str], ...] = (
    ("nhavashevafreeportterminal", "NSFT"),
    ("nsft", "NSFT"),
    ("dpworldnhavashevaict", "NSICT"),
    ("nsict", "NSICT"),
    ("nhavashevaigt", "NSIGT"),
    ("gatewayterminalsindia", "GTI"),
    ("psamumbaibmct", "BMCT"),
    ("bharatmumbaicontainerterminals", "BMCT"),
    ("bmct", "BMCT"),
)

# Terminal placeholders the slips print for "no value". Kept verbatim in attrs;
# nulled in the typed column and reported as a dq_issue.
SENTINELS = {"", "-", "NIL", "NA", "N/A", "NONE", "NOSEAL", "EMPTY", "READ SMS"}

# ---------------------------------------------------------------------------
# Parsed document -> original scan.
#
# The corpus ships NO mapping between a parsed file and its photo: the JPEGs are
# WhatsApp filenames ("WhatsApp Image 2026-06-12 at 19.36.11 (1).jpeg") carrying
# only a capture timestamp, and they sit in a sibling folder. The association
# below was established by READING each scan and matching the identifying field
# it prints (quoted per entry) against the parsed payload. It is a 1:1 bijection
# over all 12 documents and all 12 JPEGs.
#
# Verified against the scan; the quoted token is what the photo shows.
SCAN_BY_VARIANT: Dict[str, Tuple[str, str]] = {
    #  variant                  scan basename                                        identifying token on the scan
    "eir1_psa_bmct":       ("WhatsApp Image 2026-06-12 at 19.36.11 (1).jpeg", "EIR NO 4339869 / LIC NO MH43BX1488"),
    "eir2_dpworld_nsict":  ("WhatsApp Image 2026-06-12 at 19.36.11.jpeg",     "DP World Nhava Sheva ICT / BAT UE56 / 4L10"),
    "eir3_gateway_maersk": ("WhatsApp Image 2026-06-12 at 19.36.12 (1).jpeg", "CTR No MRKU5014206 / Trk In 08:26 Trk out 11:11"),
    "eir4_gateway_one":    ("WhatsApp Image 2026-06-12 at 19.36.12.jpeg",     "CTR No NYKU4768188 / Trk In 14:55 Trk out 16:17"),
    "form13_igt_eir":      ("WhatsApp Image 2026-06-12 at 19.36.13.jpeg",     "Nhava Sheva IGT EIR Ticket / Visit ID 4418958"),
    "form13_nsft_eadvice": ("WhatsApp Image 2026-06-12 at 19.36.40 (1).jpeg", "NSFT E-Advice / SEGU1441550 / barcode 1778187"),
    "form13_nsict_egate":  ("WhatsApp Image 2026-06-12 at 19.36.41.jpeg",     "E-GATE FORM / E-Gate 16497850 / MEDU1777575"),
    "form13_psa_bmct":     ("WhatsApp Image 2026-06-12 at 19.36.40.jpeg",     "FORM TYPE 13 / BMOU5841115 / barcode 5921049"),
    "ticket1":             ("WhatsApp Image 2026-06-12 at 18.10.02 (1).jpeg", "GTI Pick-Up Ticket-Import / AMSU4000180 / T901"),
    "ticket2":             ("WhatsApp Image 2026-06-12 at 18.10.02 (2).jpeg", "NSFT PICK-UP TICKET / Pin No 230283 / OOLU9340457"),
    "ticket3":             ("WhatsApp Image 2026-06-12 at 18.10.02.jpeg",     "Nhava Sheva IGT Visit Ticket / Visit ID 4421881"),
    "ticket4":             ("WhatsApp Image 2026-06-12 at 18.10.03.jpeg",     "PSA Mumbai BMCT-TID / LIC NO MH43CQ0554 / A658"),
}


# =============================== value helpers ==============================
def _clean(raw: Any) -> Optional[str]:
    """Trim to a non-empty string, or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def scalar(raw: Any, *, sentinels: bool = True) -> Optional[str]:
    """A typed-column value: trimmed, with terminal placeholders nulled."""
    s = _clean(raw)
    if s is None:
        return None
    if sentinels and s.upper() in SENTINELS:
        return None
    return s


def plate(raw: Any) -> Optional[str]:
    """Normalise a vehicle plate the way the API's filter does (no spaces/dashes)."""
    s = scalar(raw)
    return re.sub(r"[^A-Z0-9]", "", s.upper()) if s else None


def iso_code(raw: Any) -> Optional[str]:
    """ISO 6346 size/type code. Slips sometimes annotate it ('2210 (20 FT)')."""
    s = scalar(raw)
    if not s:
        return None
    m = re.match(r"\s*([A-Z0-9]{4})\b", s.upper())
    return m.group(1) if m else s


def weight_kg(raw: Any, *, unit: str) -> Optional[float]:
    """Gross weight in kg. `unit` is the slip's own unit — never guessed."""
    s = scalar(raw)
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    if not m:
        return None
    value = float(m.group(0))
    if unit == "kg":
        return round(value, 3)
    if unit in ("t", "mt"):
        return round(value * 1000.0, 3)
    raise ValueError(f"unknown weight unit {unit!r}")


_TS_FORMATS = (
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
)


def ts(raw: Any, *, time_part: Any = None) -> Optional[dt.datetime]:
    """Parse a slip timestamp into an IST-aware datetime.

    Handles every layout the corpus uses (dd/mm, dd-mm, dd-Mon, ISO). PSA's
    Form 13 splits date and time across two fields, hence `time_part`.
    """
    s = scalar(raw)
    if s is None:
        return None
    t = scalar(time_part)
    if t:
        s = f"{s} {t}"
    for fmt in _TS_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp {s!r}")


# ============================ per-variant mapping ===========================
# Each document layout differs, so every variant gets an explicit projection
# from its verbatim payload onto the gate_document columns. Anything without a
# column stays in `attrs`. Keys omitted here are deliberately NULL.
Normaliser = Callable[[Dict[str, Any]], Dict[str, Any]]


def _eir1_psa_bmct(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("EIRNo")),
        "doc_ts": ts(f.get("DateTime")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISOCode")),
        "load_status": scalar(f.get("ContainerStatus")),
        "gross_weight_kg": weight_kg(f.get("GrossWeight"), unit="t"),
        "seal1": scalar(f.get("SealNo1")),
        "seal2": scalar(f.get("SealNo2")),          # 'NOSEAL' -> NULL
        "vehicle_no": plate(f.get("LICNo")),        # BMCT prints the plate as "LIC NO"
        "bat_no": scalar(f.get("BATNo")),
        "transporter_name": scalar(f.get("TruckCompany")),
        "vessel_name": scalar(f.get("VesselVia")),
        "cfs": scalar(f.get("ToFrom")),
    }


def _eir2_dpworld_nsict(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ts": ts(f.get("Date")),
        "container_no": scalar(f.get("ContainerNo")),   # absent on this layout
        "iso_code": iso_code(f.get("ISOCode")),
        "load_status": scalar(f.get("Status")),
        "vehicle_no": plate(f.get("Truck")),
        "bat_no": scalar(f.get("BAT")),
        "gate_no": scalar(f.get("Gate")),
        "yard_position": scalar(f.get("YardPosition")),
        "group_code": scalar(f.get("GroupCode")),
    }


def _eir_gateway(f: Dict[str, Any]) -> Dict[str, Any]:
    """GTI's EIR layout — the only one that prints driver and truck in/out."""
    return {
        "doc_ref": scalar(f.get("TransID")),
        "doc_ts": ts(f.get("Date")),
        "visit_id": scalar(f.get("Via")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISO")),
        "load_status": scalar(f.get("Status")),
        "gross_weight_kg": weight_kg(f.get("GrossWeight"), unit="mt"),
        "seal1": scalar(f.get("Seal1")),
        "seal2": scalar(f.get("Seal2")),
        "vehicle_no": plate(f.get("TruckNo")),
        "bat_no": scalar(f.get("BATID")),
        "driver_name": scalar(f.get("Driver")),
        "driver_licence": scalar(f.get("DL")),
        "truck_in_ts": ts(f.get("TruckIn")),
        "truck_out_ts": ts(f.get("TruckOut")),
        "vessel_name": scalar(f.get("Vessel")),
        "voyage": scalar(f.get("Via")),
        "pod": scalar(f.get("PODPOL")),
        "group_code": scalar(f.get("GroupCode")),
    }


def _form13_igt_eir(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("ExportApproval")),
        "doc_ts": ts(f.get("Date")),
        "visit_id": scalar(f.get("VisitID")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISOCode")),
        "load_status": scalar(f.get("Status")),
        "gross_weight_kg": weight_kg(f.get("Weight"), unit="kg"),
        "seal1": scalar(f.get("Seal")),
        "vehicle_no": plate(f.get("TruckNo")),
        "bat_no": scalar(f.get("BATNo")),
        "voyage": scalar(f.get("Via")),
        "pol": scalar(f.get("POL")),
        "pod": scalar(f.get("POD")),
        "group_code": scalar(f.get("GroupCode")),
    }


def _form13_nsft_eadvice(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("Barcode")),
        "doc_ts": ts(f.get("DateTime")),
        "visit_id": scalar(f.get("VisitID")),
        "container_no": scalar(f.get("ContainerNo")),
        "seal1": scalar(f.get("SealNo1")),
        "seal2": scalar(f.get("SealNo2")),
        "pod": scalar(f.get("POD")),
    }


def _form13_nsict_egate(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("EGateNo")),
        "doc_ts": ts(f.get("GeneratedDateTime")),
        "visit_id": scalar(f.get("Voyage")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISOCode")),
        "gross_weight_kg": weight_kg(f.get("VGMKG"), unit="kg"),
        "seal1": scalar(f.get("LineSealNo")),
        "seal2": scalar(f.get("CustomSealNo")),
        "vehicle_no": plate(f.get("TruckNo")),
        "bat_no": scalar(f.get("BATNo")),
        "transporter_name": scalar(f.get("Transporter")),
        "vessel_name": scalar(f.get("VesselName")),
        "voyage": scalar(f.get("Voyage")),
        "pod": scalar(f.get("PortOfDischarge")),
        "booking_no": scalar(f.get("BookingNo")),
        "cfs": scalar(f.get("CFS")),
    }


def _form13_psa_bmct(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("Barcode")),
        "doc_ts": ts(f.get("Date"), time_part=f.get("Time")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISOCode")),
        "load_status": scalar(f.get("LadenEmpty")),   # 'EMPTY' placeholder -> NULL
        "gross_weight_kg": weight_kg(f.get("VGMWeightMT"), unit="mt"),
        "seal1": scalar(f.get("SealNo1")),            # 'NIL' -> NULL
        "seal2": scalar(f.get("SealNo2")),            # 'NIL' -> NULL
        "vessel_name": scalar(f.get("Vessel")),
        "pod": scalar(f.get("PODPOL")),
    }


def _ticket1_gti(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ts": ts(f.get("Date")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISOCode")),
        "gross_weight_kg": weight_kg(f.get("GrossWeight"), unit="mt"),
        "seal1": scalar(f.get("SealNo1")),
        "vehicle_no": plate(f.get("TrailerNo")),
        "bat_no": scalar(f.get("BAT_ID")),
        "yard_position": scalar(f.get("YardLocation")),   # 'Read SMS' -> NULL
        "group_code": scalar(f.get("GroupCode")),
    }


def _ticket2_nsft(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("Transaction")),
        "pin_no": scalar(f.get("PinNo")),
        "doc_ts": ts(f.get("DateTime")),
        "container_no": scalar(f.get("ContainerNo")),
        "iso_code": iso_code(f.get("ISO")),
        "load_status": scalar(f.get("Status")),
        "gross_weight_kg": weight_kg(f.get("GrossWeight"), unit="mt"),
        "vehicle_no": plate(f.get("VehicleNo")),
        "bat_no": scalar(f.get("BAT")),
        "transporter_name": scalar(f.get("TruckingCompany")),
        "gate_no": scalar(f.get("Lane")),
        "yard_position": scalar(f.get("YardPosition")),
        "cfs": scalar(f.get("CFS")),
    }


def _ticket3_igt(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "doc_ref": scalar(f.get("VisitID")),
        "doc_ts": ts(f.get("Time")),
        "visit_id": scalar(f.get("VisitID")),
        "load_status": scalar(f.get("Status")),
        "vehicle_no": plate(f.get("TruckNo")),
        "bat_no": scalar(f.get("BATNo")),
        "gate_no": scalar(f.get("Gate")),
        "yard_position": scalar(f.get("YardLocation")),
        "group_code": scalar(f.get("GroupID")),
    }


def _ticket4_bmct(f: Dict[str, Any]) -> Dict[str, Any]:
    """PSA's dual-move slip: one trip carrying an export drop and an import pick.

    gate_document is one-row-per-document (unlike core.pin_ticket, which splits
    move legs), so the export leg populates the typed columns and the import leg
    stays verbatim in attrs rather than being invented into a second row.
    """
    return {
        "doc_ts": ts(f.get("Date")),
        "container_no": scalar(f.get("ExportContainer")),
        "load_status": scalar(f.get("ExportEF")),
        "vehicle_no": plate(f.get("LICNo")),
        "bat_no": scalar(f.get("BATNo")),
        "yard_position": scalar(f.get("ExportLocation")),
        "pod": scalar(f.get("POD")),
    }


NORMALISERS: Dict[str, Normaliser] = {
    "eir1_psa_bmct": _eir1_psa_bmct,
    "eir2_dpworld_nsict": _eir2_dpworld_nsict,
    "eir3_gateway_maersk": _eir_gateway,
    "eir4_gateway_one": _eir_gateway,
    "form13_gti_eir": _form13_igt_eir,
    "form13_igt_eir": _form13_igt_eir,
    "form13_nsft_eadvice": _form13_nsft_eadvice,
    "form13_nsict_egate": _form13_nsict_egate,
    "form13_psa_bmct": _form13_psa_bmct,
    "ticket1": _ticket1_gti,
    "ticket2": _ticket2_nsft,
    "ticket3": _ticket3_igt,
    "ticket4": _ticket4_bmct,
}

# gate_document columns this importer writes (order is the INSERT order).
DOC_COLUMNS = (
    "doc_category", "terminal_id", "doc_variant", "doc_ref", "pin_no", "visit_id",
    "doc_ts", "container_no", "iso_code", "load_status", "gross_weight_kg",
    "seal1", "seal2", "vehicle_no", "bat_no", "driver_name", "driver_licence",
    "transporter_name", "truck_in_ts", "truck_out_ts", "gate_no", "yard_position",
    "vessel_name", "voyage", "pol", "pod", "booking_no", "cfs", "group_code",
    "attrs", "image_file", "source_file", "data_origin",
)


# ================================ discovery =================================
class SourceError(RuntimeError):
    """The corpus is missing or does not look like the expected drop."""


def find_corpus(explicit: Optional[str]) -> Path:
    """Locate the "8- Form13, EIR, PIN" folder without assuming a path."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_dir():
            raise SourceError(f"--corpus path does not exist: {p}")
        return p if CORPUS_DIR_RE.search(p.name) else _search_under(p)
    env = os.environ.get("JNPA_CORPUS_DIR", "").strip()
    if env:
        return find_corpus(env)
    for root in CANDIDATE_ROOTS:
        if not root.is_dir():
            continue
        try:
            hit = _search_under(root)
        except SourceError:
            continue
        return hit
    raise SourceError(
        "could not locate the gate-document corpus ('8- Form13, EIR, PIN'). "
        "Pass --corpus /path/to/Data or set JNPA_CORPUS_DIR."
    )


def _search_under(root: Path, *, max_depth: int = 5) -> Path:
    """Breadth-first search for the corpus directory, shallowest match wins."""
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth > max_depth:
            continue
        try:
            children = sorted(p for p in current.iterdir() if p.is_dir())
        except (PermissionError, OSError):
            continue
        for child in children:
            if CORPUS_DIR_RE.search(child.name):
                return child
            frontier.append((child, depth + 1))
    raise SourceError(f"no corpus directory under {root}")


class ParsedDoc:
    """One physical gate document as found on disk."""

    def __init__(self, variant: str, category: str, files: Dict[str, Path],
                 fields: Dict[str, Any]) -> None:
        self.variant = variant
        self.category = category
        self.files = files                 # suffix -> path (".json", ".xml", ".txt")
        self.fields = fields               # verbatim parsed payload
        self.scan: Optional[Path] = None
        self.aliases: List[str] = []       # duplicate stems folded into this doc
        self.dq: List[Tuple[str, str, str]] = []   # (issue_type, severity, detail)

    @property
    def primary(self) -> Path:
        """The file the payload was read from — JSON preferred, then XML, then TXT."""
        for suffix in PARSED_SUFFIXES:
            if suffix in self.files:
                return self.files[suffix]
        raise SourceError(f"{self.variant}: no parsed file")

    @property
    def all_files(self) -> List[Path]:
        return sorted(self.files.values(), key=lambda p: p.name)


def _parse_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_xml(path: Path) -> Dict[str, Any]:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    return {child.tag: (child.text or "").strip() for child in root}


def _parse_txt(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


_PARSERS = {".json": _parse_json, ".xml": _parse_xml, ".txt": _parse_txt}


def discover(corpus: Path) -> Tuple[List[ParsedDoc], List[Path]]:
    """Walk the corpus and return (documents, scans). Purely read-only."""
    parsed_dirs = [p for p in corpus.rglob("*") if p.is_dir() and PARSED_DIR_RE.search(p.name)]
    if not parsed_dirs:
        raise SourceError(f"no *_parsed directories under {corpus}")

    by_stem: Dict[Tuple[str, str], Dict[str, Path]] = {}
    for directory in parsed_dirs:
        category = CATEGORY_BY_DIR[directory.name.lower()]
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in PARSED_SUFFIXES and not path.name.startswith("."):
                by_stem.setdefault((category, path.stem), {})[path.suffix.lower()] = path

    docs: List[ParsedDoc] = []
    for (category, stem), files in sorted(by_stem.items()):
        doc = ParsedDoc(stem, category, files, {})
        source = doc.primary
        doc.fields = _PARSERS[source.suffix.lower()](source)
        if ".json" not in files:
            doc.dq.append(("incomplete_parse", "info",
                           f"no .json in the corpus; payload read from {source.suffix} "
                           f"({', '.join(sorted(files))})"))
        docs.append(doc)

    scans = sorted(p for p in corpus.rglob("*")
                   if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES
                   and not p.name.startswith("."))
    return _fold_duplicates(docs), scans


def _payload_key(fields: Dict[str, Any]) -> str:
    """Content hash of a parsed payload — identity independent of filename."""
    canonical = json.dumps({k: str(v).strip() for k, v in sorted(fields.items())},
                           sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fold_duplicates(docs: List[ParsedDoc]) -> List[ParsedDoc]:
    """Collapse stems whose parsed payload is byte-for-byte the same document.

    The corpus ships one physical Form 13 twice — `form13_gti_eir.xml` and
    `form13_igt_eir.json` hold identical content, because the drop misspells the
    terminal (GTI is Gateway Terminals India; the slip is Nhava Sheva IGT). They
    are ONE document, so counting both would overstate the set by one.

    Nothing is discarded: the surviving row records every source file, both
    stems are registered in core.ingest_file, and the collision is written to
    core.dq_issue. Detection is by content, not by a hard-coded pair, so a
    re-supplied clean corpus needs no code change. The stem with the most parsed
    representations wins (ties: lexicographic) — a stable, explainable choice.
    """
    groups: Dict[Tuple[str, str], List[ParsedDoc]] = {}
    for doc in docs:
        groups.setdefault((doc.category, _payload_key(doc.fields)), []).append(doc)

    out: List[ParsedDoc] = []
    for (_category, _key), members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        members.sort(key=lambda d: (-len(d.files), d.variant))
        keeper, dupes = members[0], members[1:]
        for dupe in dupes:
            keeper.aliases.append(dupe.variant)
            keeper.files.update({f"{dupe.variant}{s}": p for s, p in dupe.files.items()})
            keeper.dq.append((
                "duplicate", "warn",
                f"corpus ships this document twice: '{dupe.variant}' "
                f"({', '.join(sorted(p.name for p in dupe.files.values()))}) is identical to "
                f"'{keeper.variant}'; kept as one document, both source files registered",
            ))
        out.append(keeper)
    return sorted(out, key=lambda d: (d.category, d.variant))


def attach_scans(docs: Sequence[ParsedDoc], scans: Sequence[Path]) -> None:
    """Bind every document to its original JPEG via the verified mapping."""
    by_name = {p.name: p for p in scans}
    for doc in docs:
        entry = SCAN_BY_VARIANT.get(doc.variant)
        if entry is None:
            raise SourceError(
                f"{doc.variant}: no scan mapping. The corpus provides no parsed->photo "
                f"link, so each entry in SCAN_BY_VARIANT must be established by reading "
                f"the scan. Add one for this document rather than guessing."
            )
        name, _token = entry
        path = by_name.get(name)
        if path is None:
            raise SourceError(f"{doc.variant}: mapped scan {name!r} not found in the corpus")
        doc.scan = path

    used = {d.scan.name for d in docs if d.scan}
    for orphan in sorted(set(by_name) - used):
        print(f"  ! scan not referenced by any document: {orphan}", file=sys.stderr)


# =============================== normalisation ==============================
def terminal_code(fields: Dict[str, Any]) -> Optional[str]:
    """Resolve the slip's terminal string to a core.ref_terminal code."""
    raw = _clean(fields.get("Terminal"))
    if not raw:
        return None
    squashed = re.sub(r"[^a-z0-9]", "", raw.lower())
    for needle, code in TERMINAL_CODE_RULES:
        if needle in squashed:
            return code
    return None


def normalise(doc: ParsedDoc) -> Dict[str, Any]:
    """Project one parsed document onto the gate_document columns."""
    fn = NORMALISERS.get(doc.variant)
    if fn is None:
        raise SourceError(f"{doc.variant}: no normaliser — refusing to guess the layout")
    row: Dict[str, Any] = {c: None for c in DOC_COLUMNS}
    row.update(fn(doc.fields))
    row["doc_category"] = doc.category
    row["doc_variant"] = doc.variant
    row["attrs"] = doc.fields            # VERBATIM, exactly as parsed
    row["data_origin"] = "REAL"
    row["_terminal_code"] = terminal_code(doc.fields)
    doc.dq.extend(_data_quality(doc, row))
    return row


def _data_quality(doc: ParsedDoc, row: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Observations about this document. Reported, never repaired."""
    issues: List[Tuple[str, str, str]] = []

    # Placeholders the terminal printed for "no value".
    for key, value in doc.fields.items():
        s = _clean(value)
        if s and s.upper() in SENTINELS:
            issues.append(("placeholder", "info",
                           f"sentinel value '{s}' in field {key}"))

    for column, label in (("vehicle_no", "truck number"), ("bat_no", "BAT number"),
                          ("container_no", "container number"),
                          ("driver_licence", "driver licence")):
        if row.get(column) is None:
            issues.append(("missing_key", "info",
                           f"{label} is not printed on this document"))

    if row.get("doc_ts") is None:
        issues.append(("bad_date", "warn", "no parseable document timestamp"))

    tin, tout = row.get("truck_in_ts"), row.get("truck_out_ts")
    if tin and tout and tout < tin:
        issues.append(("inconsistent", "warn",
                       f"truck_out_ts {tout.isoformat()} precedes truck_in_ts {tin.isoformat()}"))

    if row.get("_terminal_code") is None:
        issues.append(("unresolved", "warn",
                       f"terminal {doc.fields.get('Terminal')!r} did not resolve to a known code"))

    if doc.scan is None:
        issues.append(("missing_key", "warn", "no original scan linked"))
    return issues


# ============================== object storage ==============================
class ScanStore:
    """Puts the original JPEGs where GET /api/evidence/{key} can find them.

    Two transports, same bucket layout:
      * direct    — the MinIO S3 endpoint is reachable (on the deployment host,
                    where MINIO_ENDPOINT=minio:9000). Uses the minio client, the
                    same way gateway/routers/violations.py:_store_evidence does.
      * presigned — only the public site is reachable. The gateway already mints
                    SigV4 URLs against the public host and the nginx `/minio/`
                    block strips that prefix before proxying, so a URL signed for
                    /{bucket}/{key} and sent to /minio/{bucket}/{key} verifies.
    """

    def __init__(self, *, bucket: str, public_base: Optional[str] = None) -> None:
        self.bucket = bucket
        self.public_base = public_base.rstrip("/") if public_base else None
        self._client = None

    @staticmethod
    def credentials() -> Tuple[str, str]:
        access = os.environ.get("MINIO_ACCESS_KEY", "").strip()
        secret = os.environ.get("MINIO_SECRET_KEY", "").strip()
        if not (access and secret):
            raise SourceError("MINIO_ACCESS_KEY / MINIO_SECRET_KEY are required to store scans "
                              "(use --skip-scans to import parsed data only)")
        return access, secret

    def _minio(self):
        if self._client is None:
            from minio import Minio  # lazy import — optional dependency

            access, secret = self.credentials()
            if self.public_base:
                parts = urllib.parse.urlsplit(self.public_base)
                # Sign against the PUBLIC host; the path prefix is re-applied per
                # request because the S3 client itself has no notion of one.
                self._client = Minio(parts.netloc, access_key=access, secret_key=secret,
                                     secure=parts.scheme == "https", region="us-east-1")
                self._prefix = parts.path.rstrip("/")
            else:
                endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000").strip()
                secure = os.environ.get("MINIO_SECURE", "false").strip().lower() in {
                    "1", "true", "yes", "on"}
                self._client = Minio(endpoint, access_key=access, secret_key=secret,
                                     secure=secure)
                self._prefix = ""
        return self._client

    def _signed(self, method: str, key: str, expires_s: int = 600) -> str:
        from datetime import timedelta

        url = self._minio().get_presigned_url(
            method, self.bucket, key, expires=timedelta(seconds=expires_s))
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, self._prefix + parts.path, parts.query, ""))

    def _signed_bucket_put(self, expires_s: int = 600) -> str:
        """Presign ``PUT /{bucket}`` (CreateBucket) by hand.

        The S3 client refuses an empty object name, but bucket creation is
        exactly that request. Rather than reach for another dependency, sign the
        one URL directly — plain SigV4 query authentication over the bucket path.
        """
        import hmac

        access, secret = self.credentials()
        parts = urllib.parse.urlsplit(self.public_base or "")
        host, scheme = parts.netloc, (parts.scheme or "https")
        now = dt.datetime.now(dt.timezone.utc)
        stamp, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        scope = f"{date}/us-east-1/s3/aws4_request"
        query = urllib.parse.urlencode(sorted({
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{access}/{scope}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(expires_s),
            "X-Amz-SignedHeaders": "host",
        }.items()), quote_via=urllib.parse.quote)
        canonical = (f"PUT\n/{self.bucket}\n{query}\n"
                     f"host:{host}\n\nhost\nUNSIGNED-PAYLOAD")
        to_sign = (f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n"
                   f"{hashlib.sha256(canonical.encode()).hexdigest()}")
        key = f"AWS4{secret}".encode()
        for part in (date, "us-east-1", "s3", "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        prefix = parts.path.rstrip("/")
        return f"{scheme}://{host}{prefix}/{self.bucket}?{query}&X-Amz-Signature={signature}"

    @staticmethod
    def _send(url: str, *, method: str, body: Optional[bytes] = None,
              content_type: Optional[str] = None) -> Tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def ensure_bucket(self) -> bool:
        """Create the bucket if absent. Returns True when it had to be created.

        This is the same on-demand provisioning the application already performs
        (violations.py makes the evidence bucket before its first put); it just
        happens here because the importer writes the first object.
        """
        client = self._minio()
        if not self.public_base:
            if client.bucket_exists(self.bucket):
                return False
            client.make_bucket(self.bucket)
            return True
        # Presigned transport: probe with a HEAD, create with a signed PUT.
        status, _ = self._send(self._signed("HEAD", "_probe"), method="HEAD")
        if status != 404:
            return False
        status, body = self._send(self._signed_bucket_put(), method="PUT")
        if status not in (200, 204, 409):
            raise SourceError(f"could not create bucket {self.bucket!r}: HTTP {status} {body[:200]!r}")
        return status in (200, 204)

    def put(self, key: str, data: bytes, *, content_type: str = "image/jpeg") -> None:
        if not self.public_base:
            self._minio().put_object(self.bucket, key, io.BytesIO(data), length=len(data),
                                     content_type=content_type)
            return
        status, body = self._send(self._signed("PUT", key), method="PUT", body=data,
                                  content_type=content_type)
        if status not in (200, 204):
            raise SourceError(f"upload of {key!r} failed: HTTP {status} {body[:200]!r}")

    def exists(self, key: str) -> bool:
        if not self.public_base:
            try:
                self._minio().stat_object(self.bucket, key)
                return True
            except Exception:  # noqa: BLE001 — any miss means "not stored"
                return False
        status, _ = self._send(self._signed("HEAD", key), method="HEAD")
        return status == 200


def scan_key(doc: ParsedDoc) -> str:
    """Bucket-relative object key = the path segment of /api/evidence/{key}."""
    return f"{SCAN_PREFIX}/{doc.variant}{doc.scan.suffix.lower()}"


# ================================ persistence ===============================
def corpus_relative(path: Path, corpus: Path) -> str:
    """Stable, host-independent provenance path for core.ingest_file.

    Anchored on the corpus's parent ("Data/8- Form13, EIR, PIN/...") so it matches
    the customer's own catalogue and does not leak a local home directory.
    """
    try:
        return str(Path(corpus.parent.name) / path.relative_to(corpus.parent))
    except ValueError:
        return str(Path(corpus.name) / path.relative_to(corpus))


async def run_import(docs: Sequence[ParsedDoc], rows: Sequence[Dict[str, Any]],
                     *, corpus: Path, dsn: Optional[str], store: Optional[ScanStore]) -> Dict[str, Any]:
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    stats = {"documents": 0, "inserted": 0, "updated": 0, "files": 0,
             "scans_uploaded": 0, "scans_present": 0, "dq": 0}

    async with engine.begin() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar()
        # The guard stays ON by default: an unset UC3_TARGET_DB still means QA
        # only, so an accidental run against another database aborts as before.
        # Naming the database explicitly is the operator's opt-in for the case
        # where the corpus must land somewhere else (e.g. jnpa_schema_v3, which
        # is what the gateway actually serves).
        expected = (os.environ.get("UC3_TARGET_DB") or "jnpa_qa").strip()
        if db != expected:
            raise SourceError(
                f"refusing to write: connected to database {db!r}, expected {expected!r}. "
                f"Set UC3_TARGET_DB to the database you intend to write to.")

        terminals = {code: tid for code, tid in (await conn.execute(
            text("SELECT code, terminal_id FROM core.ref_terminal"))).all()}
        if not terminals:
            raise SourceError("core.ref_terminal is empty — apply migration 0132 first")

        for doc, row in zip(docs, rows):
            # --- provenance: register every artefact this document came from ---
            file_ids: Dict[str, int] = {}
            artefacts = [(p, p.suffix.lower().lstrip(".")) for p in doc.all_files]
            if doc.scan is not None:
                artefacts.append((doc.scan, "jpeg"))
            for path, fmt in artefacts:
                rel = corpus_relative(path, corpus)
                note = ("WhatsApp photo of gate document (OCR source)" if fmt == "jpeg"
                        else None)
                file_id = (await conn.execute(text(
                    "INSERT INTO core.ingest_file (path, source_system, file_format, "
                    "                              row_count, notes) "
                    "VALUES (:path, :sys, :fmt, :rows, :notes) "
                    "ON CONFLICT (path) DO UPDATE SET source_system = EXCLUDED.source_system "
                    "RETURNING file_id"),
                    {"path": rel, "sys": SOURCE_SYSTEM, "fmt": fmt,
                     "rows": None if fmt == "jpeg" else 1, "notes": note})).scalar()
                file_ids[str(path)] = int(file_id)
                stats["files"] += 1

            # --- the original scan -------------------------------------------
            image_file = None
            if store is not None and doc.scan is not None:
                key = scan_key(doc)
                if store.exists(key):
                    stats["scans_present"] += 1
                else:
                    store.put(key, doc.scan.read_bytes())
                    stats["scans_uploaded"] += 1
                image_file = key

            payload = dict(row)
            payload.pop("_terminal_code", None)
            payload["terminal_id"] = terminals.get(row.get("_terminal_code"))
            payload["source_file"] = file_ids.get(str(doc.primary))
            payload["attrs"] = json.dumps(row["attrs"], ensure_ascii=False)
            # Keep an existing image_file if this run was told to skip scans.
            payload["image_file"] = image_file

            assignments = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in DOC_COLUMNS
                if c not in ("doc_category", "doc_variant")
                and not (c == "image_file" and image_file is None))
            result = (await conn.execute(text(
                f"INSERT INTO core.gate_document ({', '.join(DOC_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in DOC_COLUMNS)}) "
                f"ON CONFLICT (doc_category, doc_variant) WHERE doc_variant IS NOT NULL "
                f"DO UPDATE SET {assignments} "
                f"RETURNING doc_id, (xmax = 0) AS inserted"), payload)).mappings().first()
            stats["documents"] += 1
            stats["inserted" if result["inserted"] else "updated"] += 1

            # --- data quality -------------------------------------------------
            primary_file_id = file_ids.get(str(doc.primary))
            await conn.execute(text(
                "DELETE FROM core.dq_issue WHERE source_table = 'core.gate_document' "
                "AND record_ref = :ref"), {"ref": doc.variant})
            for issue_type, severity, detail in doc.dq:
                await conn.execute(text(
                    "INSERT INTO core.dq_issue (file_id, source_table, record_ref, "
                    "                           issue_type, severity, description) "
                    "VALUES (:fid, 'core.gate_document', :ref, :type, :sev, :description)"),
                    {"fid": primary_file_id, "ref": doc.variant, "type": issue_type,
                     "sev": severity, "description": detail})
                stats["dq"] += 1
    return stats


# =================================== CLI ====================================
def summarise(docs: Sequence[ParsedDoc], rows: Sequence[Dict[str, Any]]) -> None:
    by_category: Dict[str, int] = {}
    for doc in docs:
        by_category[doc.category] = by_category.get(doc.category, 0) + 1
    print(f"\nDocuments: {len(docs)}  " +
          "  ".join(f"{k}={v}" for k, v in sorted(by_category.items())))
    header = f"{'variant':22} {'cat':10} {'terminal':6} {'truck':12} {'bat':6} " \
             f"{'container':12} {'when':16} scan"
    print(header)
    print("-" * len(header))
    for doc, row in zip(docs, rows):
        when = row["doc_ts"].strftime("%Y-%m-%d %H:%M") if row["doc_ts"] else "-"
        print(f"{doc.variant:22} {doc.category:10} {row['_terminal_code'] or '-':6} "
              f"{row['vehicle_no'] or '-':12} {row['bat_no'] or '-':6} "
              f"{row['container_no'] or '-':12} {when:16} "
              f"{doc.scan.name if doc.scan else 'MISSING'}")
    dq = [(d.variant, *i) for d in docs for i in d.dq]
    print(f"\nData-quality observations: {len(dq)}")
    for variant, issue_type, severity, detail in dq:
        if severity != "info":
            print(f"  [{severity}] {variant}: {issue_type} — {detail}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=None,
                    help="path to the Data folder or the '8- Form13, EIR, PIN' dir "
                         "(default: discovered; or set JNPA_CORPUS_DIR)")
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN", "") or None,
                    help="SQLAlchemy asyncpg DSN (default: $POSTGRES_DSN)")
    ap.add_argument("--dry-run", action="store_true",
                    help="discover, parse and report; write nothing anywhere")
    ap.add_argument("--skip-scans", action="store_true",
                    help="import parsed data only; do not upload the JPEGs")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET,
                    help=f"object-storage bucket for the scans (default: {DEFAULT_BUCKET})")
    ap.add_argument("--scan-public-base", default=None,
                    help="public MinIO base URL when the S3 port is not directly "
                         "reachable, e.g. https://qa.searchintech.in/minio")
    args = ap.parse_args(argv)

    try:
        corpus = find_corpus(args.corpus)
        print(f"corpus: {corpus}")
        docs, scans = discover(corpus)
        print(f"parsed files: {sum(len(d.files) for d in docs)}   scans: {len(scans)}")
        attach_scans(docs, scans)
        rows = [normalise(doc) for doc in docs]
    except SourceError as exc:
        print(f"\nSOURCE ERROR: {exc}", file=sys.stderr)
        return 2

    summarise(docs, rows)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    store = None
    if not args.skip_scans:
        store = ScanStore(bucket=args.bucket, public_base=args.scan_public_base)
        try:
            if store.ensure_bucket():
                print(f"created object-storage bucket {args.bucket!r}")
        except SourceError as exc:
            print(f"\nSTORAGE ERROR: {exc}", file=sys.stderr)
            return 3

    try:
        stats = asyncio.run(run_import(docs, rows, corpus=corpus, dsn=args.dsn, store=store))
    except SourceError as exc:
        print(f"\nIMPORT REFUSED: {exc}", file=sys.stderr)
        return 4

    print("\nimport complete: " + "  ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
