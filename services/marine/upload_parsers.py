"""Marine UPLOAD parsers — template, CSV byte reader, validation & column mapping.

The reusable Data-Upload sub-module for the UC-I Marine vessel-call spine. Mirrors
:mod:`services.berthing.upload_parsers`: PURE functions that turn an uploaded CSV byte
payload into a validated, mapped record set plus a preview and user-friendly errors —
WITHOUT touching the DB. The import step hands the valid records to
:meth:`services.marine.repository.VesselCallRepository.persist`.

Column mapping is ALIAS-DRIVEN (header normalised, then matched against an alias table),
so "VCN", "Vessel Call Number", "vcn" all map to one field. The ONLY required column is
VCN — it is the vessel_call unique key (uq_vessel_call_vcn), so a CSV feed must carry it
(pre-VCN calls are seeded by the parser ingestion path, not by manual upload). Everything
else is optional.

SCOPE (this release): CSV only. PDF/XLS/XLSX are intentionally NOT handled here — the
service rejects a non-CSV filename before parsing.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from typing import Any, Optional

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# canonical field -> accepted NORMALISED header names (first present, non-empty wins).
ALIASES: dict[str, tuple[str, ...]] = {
    "vcn": ("vcn", "vesselcallnumber", "callnumber", "callno"),
    "via_no": ("via", "viano", "vianumber", "shortvia"),
    "imo_no": ("imo", "imono", "imonumber", "imonbr"),
    "vessel_name": ("vesselname", "vessel", "shipname", "ship", "name"),
    "voyage_no": ("voyageno", "voyage", "voyagenumber"),
    "rotation_no": ("rotationno", "rotation", "rotationnumber"),
    "purpose": ("purpose", "purposeofvisit", "visitpurpose"),
    "status": ("status", "callstatus", "state", "stage"),
    "eta": ("eta", "expectedarrival", "expectedtimeofarrival"),
    "etb": ("etb", "expectedberthing", "expectedtimeofberthing"),
    "etd": ("etd", "expecteddeparture", "expectedtimeofdeparture"),
    "ata": ("ata", "actualarrival", "alongside", "arrival"),
    "atd": ("atd", "actualdeparture", "sailed", "sailing", "departure"),
    "atc": ("atc", "actualcompletion", "opscompleted", "cargoend", "completion"),
    "source_note": ("sourcenote", "note", "remarks", "comment"),
}

# canonical label shown to the user -> the alias tuple that satisfies it.
_REQUIRED = {
    "VCN": ALIASES["vcn"],
}

_TS_FIELDS = ("eta", "etb", "etd", "ata", "atd", "atc")

_TEMPLATE_COLS = ["VCN", "VIA", "IMO", "Vessel Name", "Voyage No", "Rotation No",
                  "Purpose", "Status", "ETA", "ETB", "ETD", "ATA", "ATC", "ATD",
                  "Source Note"]
_TEMPLATE_EXAMPLE = ["INNSA1BM0R3119", "S0561", "9401234", "MAERSK SENTOSA", "0561W",
                     "3119", "Cargo", "BERTHED", "05/06/2026 06:00", "05/06/2026 09:00",
                     "06/06/2026 22:00", "05/06/2026 08:30", "06/06/2026 20:00", "", ""]

_TS_FORMATS = (
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
)


# --------------------------------------------------------------------------- ParseResult
class ParseResult:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.preview: list[dict[str, Any]] = []
        self.row_count = 0
        self.invalid_count = 0
        self.duplicate_count = 0
        self.rejected = False

    def err(self, row, col, code, detail, raw=None):
        self.errors.append({"row_number": row, "column_name": col, "error_code": code,
                            "error_detail": detail, "raw_value": (None if raw is None else str(raw))})

    def warn(self, row, col, code, detail):
        self.warnings.append({"row_number": row, "column_name": col, "error_code": code,
                              "error_detail": detail})


# --------------------------------------------------------------------------- helpers
def norm_header(h: Any) -> str:
    """Normalise a header cell: lowercase, drop everything but a-z0-9."""
    if h is None:
        return ""
    return "".join(ch for ch in str(h).strip().lower() if ch.isalnum())


def clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_ts(raw: Any) -> Optional[_dt.datetime]:
    """Parse a timestamp string against the accepted formats. Returns tz-aware IST, or
    None when the value is unrecognised (the caller records an invalid_timestamp error)."""
    s = clean(raw)
    if s is None:
        return None
    for fmt in _TS_FORMATS:
        try:
            dt = _dt.datetime.strptime(s, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def is_csv(filename: Optional[str]) -> bool:
    return bool(filename) and str(filename).lower().endswith(".csv")


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_TEMPLATE_COLS)
    w.writerow(_TEMPLATE_EXAMPLE)
    return buf.getvalue()


def read_rows_from_bytes(content: bytes, filename: Optional[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Decode CSV bytes -> (header, rows). Raises ValueError on an unreadable / empty
    file. CSV ONLY — the service guards the extension before calling this."""
    if not content:
        raise ValueError("empty file")
    try:
        text_data = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text_data = content.decode("latin-1")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"could not decode file as text: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text_data))
    header = list(reader.fieldnames or [])
    if not header:
        raise ValueError("no header row found")
    rows = [dict(r) for r in reader]
    return header, rows


def _pick(row_norm: dict[str, Any], canonical: str) -> Optional[str]:
    for src in ALIASES.get(canonical, ()):
        if src in row_norm:
            v = clean(row_norm[src])
            if v is not None:
                return v
    return None


def check_required_columns(res: ParseResult, header: list[str]) -> bool:
    hset = {norm_header(h) for h in header if norm_header(h)}
    missing = [label for label, aliases in _REQUIRED.items()
               if not any(a in hset for a in aliases)]
    if missing:
        for label in missing:
            res.err(None, label, "missing_column",
                    f"{label} column not found. Please download the latest template.")
        res.rejected = True
        return False
    return True


# --------------------------------------------------------------------------- parse
def parse(header: list[str], rows: list[dict[str, Any]], *,
          source_file: Optional[str] = None) -> ParseResult:
    """Validate + map vessel-call rows for one uploaded CSV. VCN is required and is the
    in-file dedup + DB upsert key. imo_no/terminal_id/berth_id are handled downstream
    (imo_no is resolved against core.vessel in the repository; terminal/berth are left
    for a later slice)."""
    res = ParseResult()
    res.row_count = len(rows)
    if not check_required_columns(res, header):
        return res

    seen: set[str] = set()
    for i, raw in enumerate(rows, start=1):
        row_norm = {norm_header(k): v for k, v in raw.items() if norm_header(k)}

        vcn = _pick(row_norm, "vcn")
        if not vcn:
            res.err(i, "VCN", "empty_required", "VCN is empty")
            res.invalid_count += 1
            continue
        vcn = vcn.upper()

        rec: dict[str, Any] = {
            "vcn": vcn,
            "via_no": _pick(row_norm, "via_no"),
            "imo_no": _pick(row_norm, "imo_no"),
            "vessel_name": (_pick(row_norm, "vessel_name") or None),
            "voyage_no": _pick(row_norm, "voyage_no"),
            "rotation_no": _pick(row_norm, "rotation_no"),
            "purpose": _pick(row_norm, "purpose"),
            "status": _pick(row_norm, "status"),
            "source_note": (_pick(row_norm, "source_note") or source_file),
        }
        if rec["vessel_name"]:
            rec["vessel_name"] = rec["vessel_name"].upper()

        bad_ts = False
        for f in _TS_FIELDS:
            raw_v = _pick(row_norm, f)
            if raw_v is None:
                rec[f] = None
                continue
            v = parse_ts(raw_v)
            if v is None:
                res.err(i, f.upper(), "invalid_timestamp",
                        f"'{raw_v}' is not a recognised date/time (expected DD/MM/YYYY HH:MM)",
                        raw_v)
                bad_ts = True
                break
            rec[f] = v
        if bad_ts:
            res.invalid_count += 1
            continue

        if vcn in seen:
            res.duplicate_count += 1
            res.warn(i, "VCN", "duplicate_in_file",
                     f"VCN {vcn} already appears earlier in this file (skipped)")
            continue
        seen.add(vcn)
        res.records.append(rec)

    res.preview = [{
        "VCN": r["vcn"], "Vessel": r.get("vessel_name") or "—",
        "VIA": r.get("via_no") or "—", "Status": r.get("status") or "—",
        "ETA": r["eta"].strftime("%d/%m/%Y %H:%M") if r.get("eta") else "—",
        "ATA": r["ata"].strftime("%d/%m/%Y %H:%M") if r.get("ata") else "—",
    } for r in res.records[:20]]
    return res
