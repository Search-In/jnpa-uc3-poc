"""Deterministic index over the sample-data-pack for the API simulator.

Walks the dump's group folders once at startup and produces, per file, an
indexed record faithful to the live API's shape — including its defects:

  * record ids are GLOBALLY SEQUENTIAL integers wearing a thin base64 coat
    (defect D2): ``rec_<b64(decimal)><12-char tag>``; the fileRef is the SAME
    string with the prefix swapped (``ref_...``) — exactly what the captures
    show, despite the docs calling the reference "opaque";
  * ``publishedAt`` is a deterministic spread across the real coverage
    window (2026-07-09 → 2026-07-31 IST), and the LAST FOUR records of every
    group with at least six files SHARE one timestamp — the non-unique-sort-
    key tie (defect D13b) that makes exclusive-``since`` boundary handling
    testable;
  * checksums are real sha256 over the real file bytes, so client-side
    verification and the sync layer's dump-vs-API dedup are exercised with
    true values.

Videos (14-Videos) and the API-materials folder (15-API Access) are not
groups and are never indexed; bathymetry is catalogued but static (records 0).
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

COVERAGE_FROM = datetime(2026, 7, 9, 0, 0, 0, tzinfo=IST)
COVERAGE_TO = datetime(2026, 7, 31, 23, 36, 0, tzinfo=IST)

# Group slug -> Data/ subfolder. Mirrors the live catalogue exactly.
GROUP_FOLDERS: Dict[str, str] = {
    "nlp-marine": "1-NLP Marine",
    "bathymetry": "2-JNPA_Sea_Channels_Bathymetry",
    "port-craft-pilot": "3- Port Craft & Pilot",
    "shipping-lines": "4-Shipping Lines",
    "customs": "5- Customs",
    "edi-messages": "6- EDI Message and Format",
    "berthing-reports": "7-Berthing Reports",
    "gate-documents": "8- Form13, EIR, PIN",
    "rail-fois": "9-NLDS_FOIS",
    "rail-form11-icd": "10-Form 11_ICD Rail",
    "transport": "11-Transport Data",
    "daily-reports": "12-Performance & Daily Reports",
    "cfs-ecy": "13-CFS-ECY",
}

REPORT_GROUPS = frozenset({"berthing-reports", "daily-reports"})
STATIC_GROUPS = frozenset({"bathymetry"})

GROUP_NAMES: Dict[str, str] = {
    "nlp-marine": "NLP Marine",
    "bathymetry": "Sea Channels and Bathymetry",
    "port-craft-pilot": "Port Craft and Pilot",
    "shipping-lines": "Shipping Lines",
    "customs": "Customs",
    "edi-messages": "EDI Messages",
    "berthing-reports": "Daily Berthing Reports",
    "gate-documents": "Form 13, EIR and PIN",
    "rail-fois": "NLDS and FOIS",
    "rail-form11-icd": "Form 11 and ICD Rail",
    "transport": "Transport Data",
    "daily-reports": "Performance and Daily Status Reports",
    "cfs-ecy": "CFS and Empty Yard",
}

# Non-uniform on purpose (defect D11): only nlp-marine advertises
# messageTypes, only bathymetry carries a note.
NLP_MARINE_MESSAGE_TYPES = [
    "Berth allotment", "Berth management request", "Berth request",
    "Call information", "Call information response",
    "Expected time of arrival", "Expected time of arrival (report)",
    "Pilot memo", "Port and ISPS declaration", "Port authority notice",
    "Pre-arrival notification", "Service loop registration",
    "Vessel departure", "Vessel profile", "Vessel profile (report)",
    "Voyage registration",
]

_MEDIA_TYPES = {
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".xlsx": ("application/vnd.openxmlformats-officedocument"
              ".spreadsheetml.sheet"),
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
}

# The `type=` filter vocabulary the live API accepts (defect D19: it maps to
# none of the messageType spellings records actually carry).
TYPE_FILTER_VOCAB = ("IGM", "OOC", "SMTP", "FORM11", "RAKE", "DBR", "LEO", "E-DO")

_TYPE_FILTER_PREFIX = {
    "IGM": "CHPOI03",
    "OOC": "CHPOI10",
    "SMTP": "CHPOI13",
}


def _b64_decimal(number: int) -> str:
    return base64.b64encode(str(number).encode("ascii")).decode("ascii").rstrip("=")


def _tag(rel_path: str) -> str:
    """12 deterministic base64url characters derived from the path — the
    stand-in for the live service's HMAC tag."""
    digest = hashlib.sha256(rel_path.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")[:12]


def _message_type(rel: Path) -> str:
    """Message-type code for a corpus file — same casing chaos as the live
    service (defect D20): EDI codes from filename prefixes, kebab folder
    names otherwise."""
    stem = rel.stem
    for prefix in ("CHPOI03", "CHPOI10", "CHPOI13"):
        if stem.startswith(prefix):
            return prefix
    parent = rel.parent.name if rel.parent.name else rel.parts[0]
    slug = parent.strip().lower().replace(" ", "-").replace("_", "-")
    return slug or "document"


@dataclass
class SimRecord:
    seq: int
    group: str
    rel_path: str                    # relative to the dump Data/ dir
    filename: str
    message_type: str
    published_at: datetime
    size_bytes: int
    sha256: str
    media_type: str

    @property
    def record_id(self) -> str:
        return f"rec_{_b64_decimal(self.seq)}{_tag(self.rel_path)}"

    @property
    def file_ref(self) -> str:
        return f"ref_{_b64_decimal(self.seq)}{_tag(self.rel_path)}"

    def as_item(self) -> Dict:
        return {
            "recordId": self.record_id,
            "group": self.group,
            "messageType": self.message_type,
            "messageName": self.message_type.replace("-", " ").capitalize(),
            "publishedAt": self.published_at.isoformat(),
            "containerCount": 0,
            "vesselCall": None,
            "summary": f"{self.filename} — simulated record",
            "file": {
                "fileRef": self.file_ref,
                "mediaType": self.media_type,
                "sizeBytes": self.size_bytes,
                "checksumSha256": self.sha256,
                "url": f"/v2/files/{self.file_ref}",
            },
        }


@dataclass
class SimIndex:
    data_dir: Path
    records_by_group: Dict[str, List[SimRecord]] = field(default_factory=dict)
    by_file_ref: Dict[str, SimRecord] = field(default_factory=dict)

    def group_records(self, group: str) -> List[SimRecord]:
        return self.records_by_group.get(group, [])

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.records_by_group.values())


def build_index(data_dir: str | Path, *, first_seq: int = 6000,
                tie_last_n: int = 4) -> SimIndex:
    """Index the dump deterministically (path-sorted, sequential ids).

    ``tie_last_n``: in every group with >= 6 files, the final N records are
    collapsed onto one shared publishedAt — the boundary-tie fixture.
    """
    root = Path(data_dir)
    index = SimIndex(data_dir=root)
    seq = first_seq
    for group, folder in GROUP_FOLDERS.items():
        if group in STATIC_GROUPS or group in REPORT_GROUPS:
            # static: never served; report: served as JSON, not indexed files
            continue
        base = root / folder
        if not base.is_dir():
            index.records_by_group[group] = []
            continue
        files = sorted(p for p in base.rglob("*")
                       if p.is_file() and p.name != ".DS_Store")
        span = (COVERAGE_TO - COVERAGE_FROM).total_seconds()
        step = span / max(1, len(files))
        records: List[SimRecord] = []
        for i, path in enumerate(files):
            rel = path.relative_to(root)
            published = COVERAGE_FROM + timedelta(seconds=step * (i + 1))
            content = path.read_bytes()
            record = SimRecord(
                seq=seq,
                group=group,
                rel_path=str(rel),
                filename=path.name,
                message_type=_message_type(rel.relative_to(Path(folder))
                                           if str(rel).startswith(folder)
                                           else rel),
                published_at=published,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                media_type=_MEDIA_TYPES.get(path.suffix.lower(),
                                            "application/octet-stream"),
            )
            records.append(record)
            seq += 1
        if len(records) >= 6 and tie_last_n > 1:
            shared = records[-1].published_at
            for record in records[-tie_last_n:]:
                record.published_at = shared
        index.records_by_group[group] = records
        for record in records:
            index.by_file_ref[record.file_ref] = record
    for group in GROUP_FOLDERS:
        index.records_by_group.setdefault(group, [])
    return index


def matches_type_filter(record: SimRecord, type_value: str) -> bool:
    prefix = _TYPE_FILTER_PREFIX.get(type_value)
    if prefix:
        return record.filename.startswith(prefix)
    return record.message_type.upper() == type_value.upper()


_REPORT_TERMINALS = ("APMT", "NSICT", "NSIGT", "BMCT", "NSFT")


def _marker(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:6], 16)


def _berthing_item(terminal: str, date_str: str) -> Dict:
    """One per-terminal berthing report — the real nested shape (vesselCalls)."""
    m = _marker("berthing-reports", date_str, terminal)
    return {
        "reportType": "DAILY_BERTHING_REPORT",
        "terminal": terminal,
        "reportDate": date_str,
        "reportGeneratedAt": f"{date_str}T06:00:00+05:30",
        "modifiedDate": f"{date_str}T06:00:00+05:30",
        "summary": {
            "vesselsOnBerth": 1, "arrivals": m % 3, "sailings": m % 2,
            "dischargeMoves": 1000 + m % 500, "loadMoves": 800 + m % 400,
            "yardImportTeu": 20000 + m % 9000, "yardExportTeu": 90000 + m % 9000},
        "vesselCalls": [{
            "vesselName": f"SIM VESSEL {m % 97:02d}",
            "voyage": f"INNSA1NS0S{m % 10000:04d}",
            "line": "NA", "service": "ADHOC", "loaMetres": 300.0,
            "berth": f"{terminal[:2]}{1 + m % 3:02d}",
            "alongside": f"{date_str}T{6 + m % 12:02d}:00:00+05:30",
            "operationsStart": f"{date_str}T{7 + m % 10:02d}:30:00+05:30",
            "operationsEnd": f"{date_str}T{18 + m % 4:02d}:00:00+05:30",
            "sailed": f"{date_str}T{20 + m % 3:02d}:00:00+05:30",
            "importMoves": 1000 + m % 500, "exportMoves": 800 + m % 400,
            "totalMoves": 1800 + m % 900}],
        "sourceFormat": "PDF in the sample set; served here as JSON (simulated)",
    }


def _daily_item(date_str: str) -> Dict:
    """One per-date daily status report — the real nested shape (byTerminal)."""
    by_terminal: List[Dict] = []
    for terminal in _REPORT_TERMINALS:
        m = _marker("daily-reports", date_str, terminal)
        by_terminal.append({
            "terminal": terminal, "vesselsOnBerth": 1 + m % 12,
            "arrivals": m % 6, "sailings": m % 5,
            "dischargeMoves": 500 + m % 4000, "loadMoves": 500 + m % 4000,
            "yardImportTeu": 20000 + m % 9000, "yardExportTeu": 90000 + m % 9000})
    return {
        "reportType": "DAILY_STATUS_REPORT",
        "reportDate": date_str,
        "reportGeneratedAt": f"{date_str}T06:00:00+05:30",
        "modifiedDate": f"{date_str}T06:00:00+05:30",
        "portTotals": {
            "vesselsOnBerth": 21, "arrivals": 9, "sailings": 11,
            "dischargeMoves": 7800, "loadMoves": 7700,
            "yardImportTeu": 133000, "yardExportTeu": 497000},
        "byTerminal": by_terminal,
        "sourceFormat": "PDF in the sample set; served here as JSON (simulated)",
    }


def synthetic_report_items(group: str, date_str: str,
                           terminal: Optional[str] = None) -> List[Dict]:
    """Real-shaped (nested) report items for ONE date — building block, and the
    handle the mapper unit tests import. Berthing yields one item per terminal
    (each with a ``vesselCalls`` array); daily yields one item (``byTerminal``
    array). Enabled in the running sim only with JNPA_SIM_REPORT_ITEMS=synthetic."""
    if group == "berthing-reports":
        terminals = [terminal] if terminal else list(_REPORT_TERMINALS)
        return [_berthing_item(t, date_str) for t in terminals]
    return [_daily_item(date_str)]


def synthetic_report_set(group: str, now: datetime, *, days: int = 2) -> List[Dict]:
    """The FULL set the live report endpoint returns in ONE call: every report
    item across the last ``days`` dates, each self-describing with its own
    reportDate (+ terminal for berthing). The real endpoint applies no
    date/terminal request filter — it returns them all."""
    items: List[Dict] = []
    for k in range(days):
        date_str = (now.date() - timedelta(days=k)).isoformat()
        items.extend(synthetic_report_items(group, date_str))
    return items


__all__ = [
    "IST", "COVERAGE_FROM", "COVERAGE_TO",
    "GROUP_FOLDERS", "GROUP_NAMES", "REPORT_GROUPS", "STATIC_GROUPS",
    "NLP_MARINE_MESSAGE_TYPES", "TYPE_FILTER_VOCAB",
    "SimRecord", "SimIndex", "build_index",
    "matches_type_filter", "synthetic_report_items", "synthetic_report_set",
]
