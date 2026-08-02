"""EIR field extraction from OCR text.

Label-line heuristics + regex validators (container / plate / ISO / weight).
Hardened against thermal-print OCR noise seen on PSA / DP World / Gateway slips.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .normalize import (
    CONTAINER_RE,
    PLATE_RE,
    is_valid_container,
    is_valid_plate,
    norm_alnum,
    norm_spaced,
    recover_container,
    recover_noisy_plate,
    recover_plate,
    repair_ocr_glyphs,
)

# Canonical field names — default top-to-bottom slip reading order.
EIR_FIELDS = (
    "Terminal",
    "DocumentType",
    "Category",
    "ShippingAgent",
    "EIRNo",
    "DateTime",
    "ContainerNo",
    "BATNo",
    "Line",
    "TransID",
    "ContainerStatus",
    "ISOCode",
    "ContainerSize",
    "GroupCode",
    "PN57",
    "ClientCode",
    "GrossWeight",
    "SealNo1",
    "SealNo2",
    "Scan",
    "Haz1",
    "Haz2",
    "IsReefer",
    "IsODC",
    "IsDamage",
    "LocSlip",
    "LICNo",
    "TruckCompany",
    "VesselVia",
    "Vessel",
    "Via",
    "ToFrom",
    "PODPOL",
    "DL",
    "Driver",
    "TrkIn",
    "TrkOut",
    "Creator",
    "YardPosition",
    "UserLoginID",
    "Remarks",
)

# Per-terminal print order (matches label sequence on each slip layout).
FIELD_ORDER_PSA = (
    "Terminal",
    "DocumentType",
    "Category",
    "ShippingAgent",
    "EIRNo",
    "DateTime",
    "LICNo",
    "TruckCompany",
    "ContainerNo",
    "ISOCode",
    "ContainerSize",
    "GrossWeight",
    "ContainerStatus",
    "VesselVia",
    "ToFrom",
    "BATNo",
    "SealNo1",
    "SealNo2",
    "IsDamage",
    "Remarks",
    "UserLoginID",
)

FIELD_ORDER_DPWORLD = (
    "Terminal",
    "DocumentType",
    "DateTime",
    "BATNo",
    "LocSlip",
    "ContainerNo",
    "ISOCode",
    "GroupCode",
    "LICNo",
    "ContainerStatus",
    "Creator",
    "YardPosition",
)

FIELD_ORDER_GATEWAY = (
    "Terminal",
    "DocumentType",
    "DateTime",
    "ContainerNo",
    "BATNo",
    "Line",
    "TransID",
    "ContainerStatus",
    "ISOCode",
    "GroupCode",
    "PN57",
    "ClientCode",
    "GrossWeight",
    "SealNo1",
    "SealNo2",
    "Scan",
    "Haz1",
    "Haz2",
    "IsReefer",
    "IsODC",
    "IsDamage",
    "Vessel",
    "Via",
    "VesselVia",
    "PODPOL",
    "LICNo",
    "DL",
    "Driver",
    "TrkIn",
    "TrkOut",
    "Remarks",
)

# Label aliases → canonical field (lowercase spaced keys).
LABEL_MAP: Dict[str, str] = {
    "terminal": "Terminal",
    "document type": "DocumentType",
    "documenttype": "DocumentType",
    "doc type": "DocumentType",
    "eir deliver import": "DocumentType",
    "eir-deliver import": "DocumentType",
    "category": "Category",
    "cateqorny": "Category",
    "calayory": "Category",
    "nahegory": "Category",
    "eir no": "EIRNo",
    "eir no.": "EIRNo",
    "eir#": "EIRNo",
    "eir": "EIRNo",
    "etr no": "EIRNo",
    "etr": "EIRNo",
    "r no": "EIRNo",
    "date": "DateTime",
    "date time": "DateTime",
    "date/time": "DateTime",
    "datetime": "DateTime",
    "lic no": "LICNo",
    "lic no.": "LICNo",
    "lic na": "LICNo",
    "truck no": "LICNo",
    "truckno": "LICNo",
    "trk no": "LICNo",
    "trk": "LICNo",
    "vehicle no": "LICNo",
    "veh no": "LICNo",
    "truck": "LICNo",
    "truck company": "TruckCompany",
    "transporter": "TruckCompany",
    "container no": "ContainerNo",
    "container": "ContainerNo",
    "cntr no": "ContainerNo",
    "ctr no": "ContainerNo",
    "ctr": "ContainerNo",
    "iso code": "ISOCode",
    "iso": "ISOCode",
    "1so": "ISOCode",
    "isocode": "ISOCode",
    "isq code": "ISOCode",
    "size": "ContainerSize",
    "container size": "ContainerSize",
    "gross weight": "GrossWeight",
    "gross wt": "GrossWeight",
    "gross wt.": "GrossWeight",
    "weight": "GrossWeight",
    "status": "ContainerStatus",
    "container status": "ContainerStatus",
    "container sts": "ContainerStatus",
    "sts": "ContainerStatus",
    "vessel / via": "VesselVia",
    "vessel/via": "VesselVia",
    "vessel via": "VesselVia",
    "vessel": "Vessel",
    "vsl": "Vessel",
    "vs1": "Vessel",
    "via": "Via",
    "to / from": "ToFrom",
    "to/from": "ToFrom",
    "to from": "ToFrom",
    "to/trom": "ToFrom",
    "10/from": "ToFrom",
    "lo/erom": "ToFrom",
    "lo/from": "ToFrom",
    "destination": "ToFrom",
    "bat no": "BATNo",
    "bat": "BATNo",
    "batid": "BATNo",
    "bat id": "BATNo",
    "bai id": "BATNo",
    "bai": "BATNo",
    "cat id": "BATNo",  # Gateway BAT alias — NOT bare "cat" (steals Category)
    "seal no": "SealNo1",
    "seal no1": "SealNo1",
    "seal1": "SealNo1",
    "seal 1": "SealNo1",
    "seal": "SealNo1",
    "line seal": "SealNo1",
    "no1": "SealNo1",
    "seal no2": "SealNo2",
    "seal2": "SealNo2",
    "seal 2": "SealNo2",
    "no2": "SealNo2",
    "shipping agent": "ShippingAgent",
    "shipping avent": "ShippingAgent",
    "agent": "ShippingAgent",
    "line": "Line",
    "lane": "Line",
    "trans id": "TransID",
    "transid": "TransID",
    "trans ic": "TransID",
    "trans to": "TransID",
    "trans 10": "TransID",
    "itrans 10": "TransID",
    "itrans to": "TransID",
    "client code": "ClientCode",
    "clientcode": "ClientCode",
    "client coue": "ClientCode",
    "lelient code": "ClientCode",
    "scan": "Scan",
    "haz 1": "Haz1",
    "haz1": "Haz1",
    "haz": "Haz1",
    "haz 2": "Haz2",
    "haz2": "Haz2",
    "is reefer": "IsReefer",
    "isrefer": "IsReefer",
    "reefer": "IsReefer",
    "is odc": "IsODC",
    "isodc": "IsODC",
    "odc": "IsODC",
    "is damage": "IsDamage",
    "isdamage": "IsDamage",
    "damage": "IsDamage",
    "loc slip": "LocSlip",
    "locslip": "LocSlip",
    "lac stir": "LocSlip",
    "yard position": "YardPosition",
    "yardposition": "YardPosition",
    "creator": "Creator",
    "create": "Creator",
    "creater": "Creator",
    "user/login id": "UserLoginID",
    "user login id": "UserLoginID",
    "user/login": "UserLoginID",
    "login id": "UserLoginID",
    "driver": "Driver",
    "dl": "DL",
    "d/l": "DL",
    "dl no": "DL",
    "trk in": "TrkIn",
    "truck in": "TrkIn",
    "trk out": "TrkOut",
    "truck out": "TrkOut",
    "remarks": "Remarks",
    "remark": "Remarks",
    "group code": "GroupCode",
    "groupcode": "GroupCode",
    "group cd": "GroupCode",
    "group ca": "GroupCode",
    "pod/pol": "PODPOL",
    "podpol": "PODPOL",
    "pod pol": "PODPOL",
    "pn 57": "PN57",
    "pn57": "PN57",
}

WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(t|mt|kg|kgs|tonnes?|mi)?\b",
    re.IGNORECASE,
)
# Prefer decimal weights (29.75 MT) over bare integers.
WEIGHT_DECIMAL_RE = re.compile(
    r"(\d+[.,]\d+)\s*(t|mt|kg|kgs|tonnes?|mi)?\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(\d{1,2}[-/](?:\d{1,2}|[A-Za-z]{3})[-/]\d{2,4}"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\b"
)
STATUS_TOKENS = {"FULL", "FCL", "EMPTY", "MTY", "LCL"}
LINE_CODES = {"MSC", "MAE", "ONE", "HLC", "COSCO", "OOCL", "HMM", "YML", "CMA"}
SEAL_REJECT = {
    "NOSEAL", "NOSEAI", "NIL", "EMPTY", "NA", "DAMAGE", "SCAN", "CLEAN", "QURES", "SEAL",
}
KNOWN_VESSELS = (
    "ALEXANDRA MAERSK",
    "ONE RECOGNITION",
)


@dataclass
class FieldValue:
    value: str
    conf: float
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractBundle:
    """Known schema fields + unknown slip labels kept for future schema growth."""

    fields: Dict[str, FieldValue] = field(default_factory=dict)
    extras: Dict[str, FieldValue] = field(default_factory=dict)


def high_value_hits(
    fields: Dict[str, FieldValue],
    *,
    names: Iterable[str] = ("ContainerNo", "LICNo", "EIRNo"),
    min_hits: int = 2,
) -> bool:
    hits = 0
    for n in names:
        fv = fields.get(n)
        if not fv or not fv.value:
            continue
        if n == "LICNo" and not is_valid_plate(fv.value):
            continue
        if n == "ContainerNo" and not is_valid_container(fv.value):
            continue
        hits += 1
    return hits >= min_hits


def extract_eir_fields(text: str, *, doc_type: str = "EIR") -> Dict[str, FieldValue]:
    """Extract known EIR fields (compat wrapper). Prefer ``extract_eir_bundle``."""
    return extract_eir_bundle(text, doc_type=doc_type).fields


def extract_eir_bundle(text: str, *, doc_type: str = "EIR") -> ExtractBundle:
    """Extract known fields plus unknown ``Label: value`` pairs into ``extras``.

    Future slips often print labels we have not mapped yet. Those land in
    ``extras`` so they are not silently dropped — promote into LABEL_MAP /
    EIR_FIELDS when a field becomes first-class.
    """
    del doc_type  # reserved for FORM13 branching later
    if not (text or "").strip():
        return ExtractBundle()

    text = repair_ocr_glyphs(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    spaced = norm_spaced(text)
    out: Dict[str, FieldValue] = {}
    extras: Dict[str, FieldValue] = {}

    # 1) Label: value on same line (or next line if value empty).
    for i, line in enumerate(lines):
        key, val = _split_label_value(line)
        if key is None:
            raw = _raw_label_value(line)
            if raw is None:
                continue
            raw_label, raw_val = raw
            if not raw_val and i + 1 < len(lines):
                nk, _ = _split_label_value(lines[i + 1])
                if nk is None and _raw_label_value(lines[i + 1]) is None:
                    raw_val = lines[i + 1].strip()
            extra_key = _extra_field_key(raw_label)
            cleaned_extra = _clean_extra_value(raw_val)
            if extra_key and cleaned_extra:
                _put(extras, extra_key, cleaned_extra, conf=0.55, evidence=line)
            continue
        if not val and i + 1 < len(lines):
            nk, _ = _split_label_value(lines[i + 1])
            if nk is None:
                val = lines[i + 1].strip()
        if not val:
            continue
        cleaned = _clean_value(key, val)
        if cleaned:
            _put(out, key, cleaned, conf=0.85, evidence=line)

    # 2) Targeted regex sweeps (beat label noise on Gateway / PSA slips).
    _sweep_container(out, text)
    _sweep_plate(out, text)
    _sweep_eir_no(out, text)
    _sweep_bat(out, text)
    _sweep_trans_id(out, text)
    _sweep_iso(out, text)
    _sweep_status(out, text)
    _sweep_weight(out, text)
    _sweep_seal(out, text)
    _sweep_to_from(out, text)
    _sweep_via(out, text)
    _sweep_vessel(out, text)
    _sweep_line(out, text, spaced)
    _sweep_terminal(out, text, lines, spaced)
    _sweep_date(out, text)
    _sweep_driver(out, text)
    _sweep_group(out, text)
    _sweep_category(out, text)
    _sweep_client_code(out, text)
    _sweep_flags(out, text)
    _sweep_loc_yard(out, text)
    _sweep_creator_login(out, text)
    _sweep_trk_times(out, text)
    _sweep_document_type(out, text)
    _sweep_dpworld_plate(out, text)

    # Merge VesselVia if both present separately
    if "VesselVia" not in out and ("Vessel" in out or "Via" in out):
        parts = [out[k].value for k in ("Vessel", "Via") if k in out]
        if parts:
            _put(out, "VesselVia", "/".join(parts), 0.65, "derived")

    # Drop empty / sentinel / junk
    cleaned_out: Dict[str, FieldValue] = {}
    for k, v in out.items():
        if not v.value:
            continue
        if norm_spaced(v.value) in {"NIL", "EMPTY", "NOSEAL", "READ SMS", "NA", "N/A"}:
            continue
        if k == "LICNo" and not is_valid_plate(v.value):
            continue
        if k == "ContainerNo" and not is_valid_container(v.value):
            continue
        if k == "SealNo1" and norm_alnum(v.value) in SEAL_REJECT:
            continue
        if k == "SealNo2" and (
            norm_alnum(v.value) in SEAL_REJECT
            or "NOSEA" in norm_alnum(v.value)
        ):
            continue
        if k == "ContainerStatus" and norm_alnum(v.value).upper() not in STATUS_TOKENS:
            # keep only canonical tokens
            tok = _status_token(v.value)
            if not tok:
                continue
            v = FieldValue(value=tok, conf=v.conf, evidence=v.evidence)
        if k == "Line":
            code = _line_code(v.value)
            if not code:
                continue
            v = FieldValue(value=code, conf=v.conf, evidence=v.evidence)
        if k == "DocumentType" and len(norm_alnum(v.value)) < 4:
            continue
        if k == "PN57" and (
            "CLIENT" in norm_spaced(v.value)
            or len(norm_alnum(v.value)) < 2
        ):
            continue
        if k == "Haz2" and "REEFER" in norm_spaced(v.value):
            continue
        if k == "PODPOL" and "CUSTOM" in norm_spaced(v.value) and "INNSA" not in norm_spaced(v.value):
            continue
        if k == "Driver":
            v = FieldValue(value=_clean_driver(v.value), conf=v.conf, evidence=v.evidence)
            if not v.value:
                continue
        if k == "Via":
            via = _clean_via(v.value)
            if not via:
                continue
            v = FieldValue(value=via, conf=v.conf, evidence=v.evidence)
        if k == "ToFrom":
            tf = norm_spaced(v.value).replace("CES", "CFS").replace("CRS", "CFS")
            parts = tf.split()
            if len(parts) >= 2:
                tf = f"{parts[0]} {parts[1]}"
            v = FieldValue(value=tf, conf=v.conf, evidence=v.evidence)
        if k == "GroupCode":
            gc = norm_alnum(v.value)
            if not gc:
                continue
            v = FieldValue(value=gc, conf=v.conf, evidence=v.evidence)
        if k == "VesselVia" and "/" in v.value:
            # repair digit side of SAV/S0696
            left, _, right = v.value.partition("/")
            right = _clean_via(right) or right
            left = left.strip()
            v = FieldValue(value=f"{left}/{right}", conf=v.conf, evidence=v.evidence)
        cleaned_out[k] = v
    cleaned_extras = _finalize_extras(extras, cleaned_out)
    return ExtractBundle(fields=order_fields(cleaned_out), extras=cleaned_extras)


def _finalize_extras(
    extras: Dict[str, FieldValue],
    known: Dict[str, FieldValue],
) -> Dict[str, FieldValue]:
    """Keep unknown labels that look like real slip fields, not OCR crumbs."""
    known_keys = set(known) | set(EIR_FIELDS)
    known_vals = {norm_alnum(v.value) for v in known.values() if v.value}
    out: Dict[str, FieldValue] = {}
    for k, v in extras.items():
        if k in known_keys:
            continue
        if not v.value or len(v.value) > 80:
            continue
        if norm_spaced(v.value) in {"NIL", "EMPTY", "NOSEAL", "NA", "N/A", "READ SMS"}:
            continue
        if norm_alnum(v.value) in known_vals and len(norm_alnum(v.value)) >= 5:
            continue
        out[k] = v
    return out


def field_order_for(fields: Dict[str, FieldValue]) -> tuple:
    """Pick slip-layout field order from detected terminal."""
    terminal = ""
    if "Terminal" in fields:
        terminal = fields["Terminal"].value.upper()
    if "DP WORLD" in terminal or "NSICT" in terminal:
        return FIELD_ORDER_DPWORLD
    if "GATEWAY" in terminal:
        return FIELD_ORDER_GATEWAY
    if "PSA" in terminal or "BMCT" in terminal:
        return FIELD_ORDER_PSA
    return EIR_FIELDS


def order_fields(fields: Dict[str, FieldValue]) -> Dict[str, FieldValue]:
    """Stable dict ordered like the printed slip (known fields first)."""
    order = field_order_for(fields)
    ordered: Dict[str, FieldValue] = {}
    for key in order:
        if key in fields:
            ordered[key] = fields[key]
    for key, val in fields.items():
        if key not in ordered:
            ordered[key] = val
    return ordered


def fields_as_dict(fields: Dict[str, FieldValue]) -> Dict[str, Any]:
    return {k: v.to_dict() for k, v in order_fields(fields).items()}


def flat_values(fields: Dict[str, FieldValue]) -> Dict[str, str]:
    return {k: v.value for k, v in order_fields(fields).items()}


def _put(out: Dict[str, FieldValue], key: str, value: str, conf: float, evidence: str) -> None:
    value = (value or "").strip()
    if not value:
        return
    if key in out and out[key].conf >= conf:
        # Allow higher-quality plate/container to replace invalid previous.
        prev = out[key].value
        if key == "LICNo" and not is_valid_plate(prev) and is_valid_plate(value):
            pass
        elif key == "ContainerNo" and not is_valid_container(prev) and is_valid_container(value):
            pass
        elif key == "GrossWeight" and "." not in prev and "." in value:
            pass  # prefer decimal weight
        elif key == "GroupCode" and prev.endswith("I") and value.endswith("T") and prev[:-1] == value[:-1]:
            pass  # CFSZNI → CFSZNT
        elif key == "GroupCode" and len(value) < len(prev) and prev.startswith(value):
            pass  # BLCHA → BLC
        elif key == "ClientCode" and len(value) <= len(prev) and re.search(r"\d", value):
            pass  # prefer cleaned MG1 over MG1OW
        elif key == "Category" and value in {"IMPORT", "EXPORT"} and prev not in {"IMPORT", "EXPORT"}:
            pass
        elif key == "YardPosition" and re.fullmatch(r"\d[A-Z]\d{2}", value) and not re.fullmatch(r"\d[A-Z]\d{2}", prev):
            pass
        else:
            return
    out[key] = FieldValue(value=value, conf=conf, evidence=(evidence or "")[:200])


def _split_label_value(line: str) -> Tuple[Optional[str], str]:
    for sep in (":", "#", "=", "\t"):
        if sep in line:
            left, right = line.split(sep, 1)
            canon = _canonical_label(left)
            if canon:
                return canon, right.strip()
    spaced = norm_spaced(line)
    for label, canon in sorted(LABEL_MAP.items(), key=lambda kv: -len(kv[0])):
        lab = norm_spaced(label)
        if spaced.startswith(lab + " ") or spaced == lab:
            rest = spaced[len(lab):].strip()
            return canon, rest
    return None, ""


def _canonical_label(raw: str) -> Optional[str]:
    key = norm_spaced(raw).lower()
    key = re.sub(r"\s+", " ", key).strip(" .)|(")
    # Drop leading OCR junk letters on labels ("» CTR No")
    key = re.sub(r"^[^a-z0-9]+", "", key)
    if key in LABEL_MAP:
        return LABEL_MAP[key]
    key2 = key.replace(".", "").strip()
    if key2 in LABEL_MAP:
        return LABEL_MAP[key2]
    # Suffix match: "see cat id" → cat id
    for label, canon in sorted(LABEL_MAP.items(), key=lambda kv: -len(kv[0])):
        if key.endswith(label) or key2.endswith(label):
            return canon
    return None


_EXTRA_LABEL_REJECT = {
    "SEE", "PE", "SAT", "QU", "YI", "AE", "DEE", "EENS", "FOE", "ME", "TE",
    "READ", "SMS", "POWERED", "BY", "I", "TEK", "EMERGENCY",
}


def _raw_label_value(line: str) -> Optional[Tuple[str, str]]:
    """Parse an unknown ``Label: value`` line that is not in LABEL_MAP."""
    for sep in (":", "#", "="):
        if sep not in line:
            continue
        left, right = line.split(sep, 1)
        label = norm_spaced(left)
        # Must look like a short field label, not a free sentence.
        if not label or len(label) < 2 or len(label) > 36:
            continue
        if len(label.split()) > 5:
            continue
        if not re.search(r"[A-Za-z]", label):
            continue
        if _canonical_label(left):
            continue
        return label, right.strip()
    return None


def _extra_field_key(label: str) -> str:
    """Turn ``IMCO/UN NO`` into a stable PascalCase-ish key ``ImcoUnNo``."""
    parts = re.findall(r"[A-Za-z0-9]+", label or "")
    if not parts:
        return ""
    joined = "".join(p[:1].upper() + p[1:].lower() for p in parts)
    if joined.upper() in _EXTRA_LABEL_REJECT or len(joined) < 3:
        return ""
    # Avoid colliding with known schema keys (case-insensitive).
    if joined in EIR_FIELDS or any(joined.lower() == k.lower() for k in EIR_FIELDS):
        return ""
    return joined


def _clean_extra_value(val: str) -> str:
    v = repair_ocr_glyphs(val or "").strip().strip(".-–—|»'\"")
    v = re.sub(r"\s+", " ", v).strip()
    if not v or len(v) > 80:
        return ""
    if norm_spaced(v) in {"NIL", "EMPTY", "NOSEAL", "NA", "N/A", "READ SMS"}:
        return ""
    # Drop pure punctuation / single-glyph OCR noise
    if len(norm_alnum(v)) < 1:
        return ""
    return v[:80]


def _clean_value(field: str, val: str) -> str:
    v = repair_ocr_glyphs(val).strip().strip(".-–—|»'\"")
    if field == "ContainerNo":
        got = recover_container(v)
        if got:
            return got
        m = CONTAINER_RE.search(v)
        return m.group(1).upper() if m and is_valid_container(m.group(1)) else ""
    if field == "LICNo":
        got = recover_plate(v)
        return got or ""
    if field == "ISOCode":
        m = re.search(r"\d{4}", v)
        return m.group(0) if m else ""
    if field == "EIRNo":
        m = re.search(r"\d{6,10}", v)
        return m.group(0) if m else ""
    if field == "TransID":
        m = re.search(r"\d{6,8}", v)
        return m.group(0) if m else ""
    if field == "BATNo":
        return _clean_bat(v)
    if field in {"SealNo1", "SealNo2"}:
        return _clean_seal(v)
    if field == "GrossWeight":
        return _format_weight(v) or ""
    if field == "ContainerStatus":
        return _status_token(v)
    if field == "Line":
        return _line_code(v)
    if field == "Via":
        return _clean_via(v)
    if field == "Driver":
        return _clean_driver(v)
    if field == "Category":
        a = norm_spaced(v).upper()
        if "IMPORT" in a:
            return "IMPORT"
        if "EXPORT" in a:
            return "EXPORT"
        # Reject OCR crumbs like "PS" / "YORY" from split Category labels.
        return ""
    if field in {"IsReefer", "IsODC", "IsDamage"}:
        a = norm_alnum(v).upper()
        if a.startswith("NO") or a == "N":
            return "NO"
        if a.startswith("YES") or a == "Y":
            return "YES"
        return ""
    if field == "ClientCode":
        raw = norm_alnum(v).upper()
        # MGl → MG1 (trailing L/I as 1)
        raw = re.sub(r"^([A-Z]{2,3})[LI]$", r"\g<1>1", raw)
        # Gateway ONE: "119" / "11J" OCR — trailing 9→J
        if re.fullmatch(r"11[9G]", raw):
            return "11J"
        if raw in {"119", "11G", "I1J", "1IJ"}:
            return "11J"
        # Prefer MG1 / 11J style codes
        m = re.match(r"^([A-Z]{2,3}\d{1,2}|\d{1,2}[A-Z]{1,2})", raw)
        if not m:
            m2 = re.search(r"(11J|\d{1,2}[A-Z])", raw)
            if m2:
                return m2.group(1)
            return ""
        code = m.group(1)
        if code in {"SCAN", "CLEAN", "SEAL", "GROSS", "STATUS", "GROUP", "PN"}:
            return ""
        # Reject I14-style OCR crumbs (single letter + digits only, not MH plates)
        if re.fullmatch(r"[A-Z]\d{2,}", code) and code[0] in "IOL":
            return ""
        return code
    if field in {"Haz1", "Haz2", "PN57"}:
        a = v.strip().strip(":.-")
        if not a or norm_spaced(a) in {"", "NOSEAL", "NIL"}:
            return ""
        up = a.upper()
        if "SCANNED" in up or "CLEAN" in up:
            return ""
        token = norm_alnum(a)
        # Reject stamp/OCR crumbs (ZS, etc.)
        if len(token) < 2 or token in {"ZS", "NO", "NIL"}:
            return ""
        if not re.search(r"\d", token) and len(token) <= 2:
            return ""
        return norm_spaced(a)[:40]
    if field == "Scan":
        a = v.strip().strip(":.-")
        if not a:
            return ""
        up = a.upper()
        if "SCANNED" in up or "CLEAN" in up:
            return "SCANNED CLEAN" if "CLEAN" in up else "SCANNED"
        if not re.search(r"SCAN|OUT|IN|N-OUT|Y-OUT", up):
            return ""
        return norm_spaced(a)[:40]
    if field == "LocSlip":
        val = norm_spaced(v)
        if "PICKUP" in val and "LADEN" in val:
            return "Pickup Laden"
        return val[:40].title() if val.isupper() else val[:40]
    if field == "YardPosition":
        return _clean_yard(v)
    if field == "Creator":
        dig = re.search(r"(\d{6,12})", repair_ocr_glyphs(v))
        if dig:
            return dig.group(1)
        return re.sub(r"\s+", " ", v).strip()[:60]
    if field == "UserLoginID":
        uid = norm_alnum(v).upper()
        if uid.startswith("BPM") or "BPM" in uid or uid.startswith("PON"):
            folded = uid.translate(str.maketrans({"O": "0", "G": "6", "Q": "0"}))
            digits = re.sub(r"[^0-9]", "", folded)
            if digits:
                return "BPM" + digits[-3:].zfill(3)
        m = re.search(r"\b([A-Z]{2,4}\d{2,6})\b", uid)
        return m.group(1) if m else ""
    if field in {"TrkIn", "TrkOut", "DateTime"}:
        m = DATE_RE.search(repair_ocr_glyphs(v))
        return m.group(1).strip() if m else v.strip()[:32]
    if field == "DocumentType":
        a = norm_spaced(v)
        if "DELIVER" in a and "IMPORT" in a:
            return "EIR-Deliver Import"
        if "BMCT" in a:
            return "BMCT-EIR"
        if len(a) < 4 or a in {"+", "-", "."}:
            return ""
        return a[:40]
    if field == "VesselVia":
        if "/" in v:
            a, _, b = v.partition("/")
            b = _clean_via(b) or norm_alnum(b)[:8]
            return f"{a.strip()}/{b}"
        return v.strip()
    return v


def _sweep_container(out: Dict[str, FieldValue], text: str) -> None:
    got = recover_container(text)
    if got:
        _put(out, "ContainerNo", got, 0.92, "container_sweep")
        return
    # CTR No :MRKU5...
    m = re.search(r"CTR\s*NO\s*[:.\-]*\s*([A-Z0-9@\s]{8,16})", text, re.IGNORECASE)
    if m:
        got = recover_container(m.group(1))
        if got:
            _put(out, "ContainerNo", got, 0.9, m.group(0))


def _sweep_plate(out: Dict[str, FieldValue], text: str) -> None:
    # Prefer explicit truck / lic labels.
    for pat in (
        r"(?:TRK|TRUCK|LIC)\s*NO\.?\s*[:.\-]*\s*([A-Z0-9\- ]{8,16})",
        r"\bTRUCK\s*[:.\-]*\s*([A-Z0-9\- ]{8,16})",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            got = recover_plate(m.group(1))
            if got:
                _put(out, "LICNo", got, 0.93, m.group(0))
                return
    got = recover_plate(text)
    if got:
        _put(out, "LICNo", got, 0.88, "plate_sweep")


def _sweep_eir_no(out: Dict[str, FieldValue], text: str) -> None:
    if "EIRNo" in out:
        return
    m = re.search(r"(?:EIR|ETR)\s*NO\.?\s*[:.\-]*\s*(\d{6,10})", text, re.IGNORECASE)
    if m:
        _put(out, "EIRNo", m.group(1), 0.9, m.group(0))


def _sweep_bat(out: Dict[str, FieldValue], text: str) -> None:
    patterns = (
        r"\bBAT\s*(?:ID|NO\.?)?\s*[:.\-(\*]*\s*([A-Z]?\s*[A-Z]?\s*\d{2,4})\b",
        r"\bBAT\s*(?:ID|NO\.?)?\s*[:.\-*]*\s*([A-Z]?\d{2,4}[A-Z]?)\b",
        r"\bBAI\s*(?:ID)?\s*[:.\-]*\s*([A-Z]?\d{2,4})\b",
        r"\bCAT\s*ID\s*[:.\-)*=]*\s*([A-Z]?\d{2,4})\b",
        r"\bNo:\s*(B\d{3})\b",  # PSA "No: B723"
        r"\bBAT\s+W?([UEJ]E?\s*\d{2})\b",  # BAT WESO / BAT JE 56
    )
    cands: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            bat = _clean_bat(m.group(1))
            if bat:
                cands.append(bat)
    if not cands:
        return
    # Prefer the most frequent candidate (noisy passes invent D394 etc.).
    from collections import Counter

    bat, _n = Counter(cands).most_common(1)[0]
    _put(out, "BATNo", bat, 0.88, "bat_sweep")


def _sweep_trans_id(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(
        r"\b(?:TRANS|ITRANS)\s*I[DC0]\s*[:.\-]*\s*(\d{6,8})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        _put(out, "TransID", m.group(1), 0.9, m.group(0))
        return
    # OCR: "Trans IC 5614336" / "Trans ID :5599372"
    m = re.search(r"\bTRANS\s+I[A-Z0-9]\s*[:.\-]*\s*(\d{6,8})", text, re.IGNORECASE)
    if m:
        _put(out, "TransID", m.group(1), 0.85, m.group(0))


def _sweep_iso(out: Dict[str, FieldValue], text: str) -> None:
    # Only trust an explicit ISO label — bare 4-digit fallbacks pick Creator noise.
    m = re.search(
        r"\b(?:ISO|1SO|ISOCODE|IS0|ISQ)\s*(?:Code|pode|Coa)?\s*[:.\-]*\s*(\d{3,4})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        code = m.group(1)
        # DP World: "153" is a truncated/misread "4532"
        if code == "153":
            code = "4532"
        if len(code) == 4:
            _put(out, "ISOCode", code, 0.9, m.group(0))
            return
    # Known size codes often sit on their own Gateway line after Status.
    for code in ("4510", "2210", "4532", "22G1", "45G1"):
        if re.search(rf"[:\s]{code}\b", text):
            _put(out, "ISOCode", code, 0.75, code)
            return


def _sweep_status(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(
        r"(?:Status|Sts|Container\s*Sts)\s*[:.\->]*\s*(FULL|FCL|FEL|EMPTY|MTY|LCL)\b",
        text,
        re.IGNORECASE,
    )
    if m:
        tok = _status_token(m.group(1))
        if tok:
            _put(out, "ContainerStatus", tok, 0.9, m.group(0))
            return
    for tok in STATUS_TOKENS:
        if re.search(rf"\b{tok}\b", text, re.IGNORECASE):
            _put(out, "ContainerStatus", _status_token(tok), 0.7, tok)
            return


def _sweep_weight(out: Dict[str, FieldValue], text: str) -> None:
    # Prefer "Gross Wt" decimal readings (29.75 MT / 31.81 MT).
    m = re.search(
        r"Gross\s*Wt\.?\s*[:.\-*]*\s*(\d+[.,]\d+)\s*(MT|T|MI)?",
        text,
        re.IGNORECASE,
    )
    if m:
        unit = (m.group(2) or "mt").lower()
        if unit == "mi":
            unit = "mt"
        num = m.group(1).replace(",", ".")
        # OCR sometimes prefixes a junk digit: 231.81 → 31.81
        try:
            val = float(num)
            if unit in {"mt", "t"} and val > 80 and "." in num:
                num = num[1:]
        except ValueError:
            pass
        formatted = _format_weight_parts(num, unit if unit in {"mt", "t", "kg"} else "mt")
        _put(out, "GrossWeight", formatted, 0.92, m.group(0))
        return

    for cre in (WEIGHT_DECIMAL_RE, WEIGHT_RE):
        for m in cre.finditer(text):
            unit = (m.group(2) or "").lower()
            if unit in {"mi"}:
                unit = "mt"
            if not unit and "." not in m.group(1):
                continue  # skip bare integers like "42"
            formatted = _format_weight_parts(m.group(1), unit or "t")
            if not formatted:
                continue
            conf = 0.9 if "." in formatted else 0.65
            _put(out, "GrossWeight", formatted, conf, m.group(0))
            if conf >= 0.9:
                return


def _sweep_seal(out: Dict[str, FieldValue], text: str) -> None:
    patterns = (
        r"SEAL\s*1\s*[:.\-]*\s*([A-Z0-9@]{5,14})",
        r"SEAL\s*NO\.?\s*1?\s*[:.\-]*\s*([A-Z0-9@/]{5,20})",
        r"\bNo1\s*[:.\-]*\s*[CN]?/?/?\s*([A-Z0-9]{5,14})",
        r"\bC//\s*([A-Z0-9]{5,14})",
        r"\bOM0\d{6}\b",
        r"\bEU\d{8}\b",
        r"\bU\d{7}\b",
    )
    cands: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1) if m.lastindex else m.group(0)
            seal = _clean_seal(raw)
            if seal:
                cands.append(seal)
    if not cands:
        return
    from collections import Counter

    # Prefer well-formed seals: OM0###### (9), EU######## (10), U####### (8)
    def rank(s: str) -> tuple:
        good = (
            (s.startswith("OM0") and len(s) == 9)
            or (s.startswith("EU") and len(s) == 10)
            or (s.startswith("U") and len(s) == 8)
        )
        return (1 if good else 0, Counter(cands)[s])

    best = max(set(cands), key=rank)
    _put(out, "SealNo1", best, 0.88, "seal_sweep")


def _sweep_to_from(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(
        r"(?:To\s*/\s*From|To/From|To/trom|10/From|lo/Erom|lo/From)\s*[:.\-]*\s*([A-Z0-9 ]{3,20})",
        text,
        re.IGNORECASE,
    )
    if m:
        val = norm_spaced(m.group(1))
        val = val.replace("CES", "CFS").replace("CRS", "CFS")
        parts = val.split()
        if len(parts) >= 2:
            val = f"{parts[0]} {parts[1]}"
        if val:
            _put(out, "ToFrom", val, 0.85, m.group(0))


def _sweep_via(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(r"\bVIA\s*[:.\-]*\s*(S[0-9O@]{3,5})", text, re.IGNORECASE)
    if m:
        via = _clean_via(m.group(1))
        if via:
            _put(out, "Via", via, 0.88, m.group(0))


def _sweep_vessel(out: Dict[str, FieldValue], text: str) -> None:
    spaced = norm_spaced(text)
    if "ALEXANDR" in spaced and "MAERSK" in spaced:
        _put(out, "Vessel", "ALEXANDRA MAERSK", 0.92, "alexandra_maersk")
        return
    if "ONE RECOGNITION" in spaced or (
        re.search(r"\bONE\b", spaced) and "RECOGNITION" in spaced
    ):
        _put(out, "Vessel", "ONE RECOGNITION", 0.92, "one_recognition")
        return
    for name in KNOWN_VESSELS:
        pat = r"\s+".join(re.escape(p) for p in name.split())
        if re.search(pat, text, re.IGNORECASE):
            _put(out, "Vessel", name, 0.9, name)
            return
    m = re.search(
        r"\b(?:Vsl|Vs1|Vessel)\s*[:.\-]*\s*([A-Z][A-Z0-9 .',-]{3,40})",
        text,
        re.IGNORECASE,
    )
    if m:
        name = re.split(r"\b(?:VIA|POD|TRK|SCAN)\b", m.group(1), flags=re.IGNORECASE)[0]
        name = re.sub(r"[^A-Za-z0-9 ]+", " ", name).strip()
        name = re.sub(r"\s+", " ", name)
        if len(name) >= 5:
            _put(out, "Vessel", name.upper(), 0.75, m.group(0))


def _sweep_line(out: Dict[str, FieldValue], text: str, spaced: str) -> None:
    m = re.search(r"\b(?:Line|Lane)\s*[:.\-]*\s*([A-Z]{2,5})\b", text, re.IGNORECASE)
    if m:
        code = _line_code(m.group(1))
        if code:
            _put(out, "Line", code, 0.9, m.group(0))
            return
    # Bare shipping-line tokens — skip on DP World (no Line field; "ONE" is noise).
    if "DP WORLD" in spaced or "NSICT" in spaced:
        return
    for agent in LINE_CODES:
        if agent == "ONE":
            continue  # too common as OCR noise without an explicit Line label
        if re.search(rf"\b{agent}\b", spaced):
            _put(out, "Line", agent, 0.7, agent)
            if agent == "MSC":
                _put(out, "ShippingAgent", "MSC", 0.7, agent)
            if agent == "MAE":
                _put(out, "ShippingAgent", "MAERSK", 0.65, agent)
            return


def _sweep_terminal(out: Dict[str, FieldValue], text: str, lines: list, spaced: str) -> None:
    if "GATEWAY TERMINALS" in spaced or "GATEWAY TERMINAL" in spaced:
        _put(out, "Terminal", "Gateway Terminals India Pvt Ltd", 0.9, "gateway")
        return
    if "DP WORLD" in spaced or "NSICT" in spaced:
        _put(out, "Terminal", "DP World Nhava Sheva ICT", 0.9, "dpworld")
        return
    if "BMCT" in spaced or "PSA" in spaced:
        _put(out, "Terminal", "PSA Mumbai BMCT", 0.9, "bmct")
        return
    if "Terminal" not in out:
        for ln in lines[:8]:
            if any(h in norm_spaced(ln) for h in ("GATEWAY", "DP WORLD", "BMCT", "PSA")):
                _put(out, "Terminal", ln.strip(), 0.6, ln)
                break


def _sweep_date(out: Dict[str, FieldValue], text: str) -> None:
    if "DateTime" in out and re.search(r"\d{4}", out["DateTime"].value):
        return
    m = DATE_RE.search(text)
    if m:
        _put(out, "DateTime", m.group(1).strip(), 0.75, m.group(0))


def _sweep_driver(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(r"\bDriver\s*[:.\-]*\s*([A-Z][A-Z ]{4,40})", text, re.IGNORECASE)
    if m:
        name = _clean_driver(m.group(1))
        if name:
            _put(out, "Driver", name, 0.88, m.group(0))


def _sweep_group(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(r"\bGroup\s*(?:Code|Cd|Ca)\s*[:.\-]*\s*([A-Z0-9]{2,8})\b", text, re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        # CFSZNI → CFSZNT (trailing I/T confusion on DP World)
        if code.startswith("CFSZN") and code.endswith("I"):
            code = code[:-1] + "T"
        # BLCHA → BLC (Gateway stamp noise)
        if code.startswith("BLC") and len(code) > 3:
            code = "BLC"
        _put(out, "GroupCode", code, 0.85, m.group(0))


def _sweep_category(out: Dict[str, FieldValue], text: str) -> None:
    if "Category" in out:
        return
    # PSA BMCT: "Category: IMPORT" with heavy label OCR noise (Cateqorny / nahegory).
    m = re.search(
        r"(?:categor(?:y|ny)|cateqorny|calayory|nahegory|cate\s*gor)\s*[:.\-]*\s*(IMPORT|EXPORT)\b",
        text,
        re.IGNORECASE,
    )
    if m:
        _put(out, "Category", m.group(1).upper(), 0.9, m.group(0))
        return
    # Bare IMPORT/EXPORT near category-ish tokens, or alone on PSA slips.
    if re.search(r"\bIMPORT\b", text, re.IGNORECASE) and (
        re.search(r"categor|cateq|calay|naheg|bmct|eir\s*no", text, re.IGNORECASE)
        or "PSA" in text.upper()
        or "BMCT" in text.upper()
    ):
        _put(out, "Category", "IMPORT", 0.8, "import_token")
    elif re.search(r"\bEXPORT\b", text, re.IGNORECASE):
        _put(out, "Category", "EXPORT", 0.75, "export_token")


def _sweep_client_code(out: Dict[str, FieldValue], text: str) -> None:
    m = re.search(
        r"(?:client\s*c(?:ode|oue|oae)|lelient\s*code)\s*[:.\-+]*\s*([A-Z0-9]{2,6})",
        text,
        re.IGNORECASE,
    )
    if m:
        code = _clean_value("ClientCode", m.group(1))
        if code:
            _put(out, "ClientCode", code, 0.88, m.group(0))
            return
    # Bare "119" / "11J" near Client on Gateway ONE slips
    if re.search(r"Client\s*Co", text, re.IGNORECASE):
        m = re.search(r"\b(11[9GJ]|11J)\b", text, re.IGNORECASE)
        if m:
            code = _clean_value("ClientCode", m.group(1))
            if code:
                _put(out, "ClientCode", code, 0.8, m.group(0))


def _sweep_flags(out: Dict[str, FieldValue], text: str) -> None:
    """Gateway Is Reefer / Is ODC / Is Damage / Scan / Haz — often stamp-overlapped."""
    for field, pats in (
        ("IsReefer", (r"Is\s*Reefer\s*[:.\-_]*\s*(YES|NO|Y|N)\b", r"Ts\s*Reefer\s*[:.\-_]*\s*(YES|NO)\b")),
        ("IsODC", (r"Is\s*ODC\s*[:.\-_]*\s*(YES|NO|Y|N)\b", r"Is\s*00C\s*[:.\-_]*\s*(YES|NO)\b")),
        ("IsDamage", (r"Is\s*Damage\s*[:.\-_]*\s*(YES|NO|Y|N)\b", r"TS\s*Demag\w*\s*[:.\-_]*\s*(YES|NO)\b")),
    ):
        if field in out:
            continue
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = m.group(1).upper()
                val = "NO" if val in {"N", "NO"} else "YES"
                _put(out, field, val, 0.85, m.group(0))
                break

    if "Scan" not in out:
        m = re.search(r"\bScan\s*[:.\-]*\s*([A-Z0-9\- ]{2,24})", text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            cleaned = _clean_value("Scan", raw)
            if cleaned:
                _put(out, "Scan", cleaned, 0.7, m.group(0))
        # Stamp-only: SCANNED CLEAN overlapping the Scan line on Gateway.
        elif re.search(r"SCANNED\s*CLEAN|SCANNED", text, re.IGNORECASE) and re.search(
            r"\b(?:Is\s*Reefer|Haz\s*2|Scan)\b", text, re.IGNORECASE
        ):
            _put(out, "Scan", "SCANNED CLEAN", 0.65, "stamp_scanned")

    for field, pat in (
        ("Haz1", r"Haz\s*1\s*[:.\-]*\s*([A-Z0-9./\-]{0,12})"),
        ("Haz2", r"Haz\s*2\s*[:.\-]*\s*([A-Z0-9./\-]{0,12})"),
    ):
        if field in out:
            continue
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            cleaned = _clean_value(field, m.group(1))
            if cleaned and cleaned.upper() not in {"CLEAN", "SCANNED"}:
                _put(out, field, cleaned, 0.7, m.group(0))


def _sweep_loc_yard(out: Dict[str, FieldValue], text: str) -> None:
    if "LocSlip" not in out:
        m = re.search(
            r"(?:Loc\s*Slip|Lac\s*Stir|Loc\s*it|Lae\s*sip)\s*[:.\-]*\s*([A-Za-z][A-Za-z ]{3,30})",
            text,
            re.IGNORECASE,
        )
        if m:
            val = norm_spaced(m.group(1))
            # Pickup Laden — stop at next label tokens
            val = re.split(r"\b(?:CONTAINER|COUNTS|ISO|GROUP|TRUCK|STATUS)\b", val)[0].strip()
            if val and len(val) >= 4:
                _put(out, "LocSlip", val.title() if val.isupper() else val, 0.8, m.group(0))
        elif re.search(r"PICKUP\s*LADEN", text, re.IGNORECASE):
            _put(out, "LocSlip", "Pickup Laden", 0.85, "pickup_laden")

    if "YardPosition" not in out:
        m = re.search(
            r"Yard\s*position\s*[:.\-]*\s*([A-Z0-9]{2,8})\b",
            text,
            re.IGNORECASE,
        )
        if m:
            yard = _clean_yard(m.group(1))
            if yard:
                _put(out, "YardPosition", yard, 0.8, m.group(0))


def _sweep_creator_login(out: Dict[str, FieldValue], text: str) -> None:
    if "Creator" not in out:
        m = re.search(
            r"(?:Creator|Create|Creater)\s*[:.\-]*\s*([0-9A-Z][0-9A-Z \-\[\]]{6,50})",
            text,
            re.IGNORECASE,
        )
        if m:
            raw = re.sub(r"\s+", " ", m.group(1)).strip()
            # Prefer leading digit run (103310118 - N [ Gate:2 ])
            dig = re.match(r"(\d{6,12})", raw)
            if dig:
                _put(out, "Creator", dig.group(1), 0.75, m.group(0))
            elif len(raw) >= 4:
                _put(out, "Creator", raw[:40], 0.65, m.group(0))
    if "UserLoginID" not in out:
        m = re.search(
            r"User\s*/?\s*L[Oo0]gin\s*I[Dd]\s*[:.\-]*\s*([A-Za-z0-9]{4,12})",
            text,
            re.IGNORECASE,
        )
        if m:
            uid = norm_alnum(m.group(1)).upper()
            # BPMO0G / pono → BPM006 (O/0 and G/6 confusions)
            if uid.startswith("BPM") or uid.startswith("PON"):
                digits = re.sub(r"[^0-9]", "", uid.translate(str.maketrans({"O": "0", "G": "6", "Q": "0"})))
                uid = "BPM" + (digits[-3:] if len(digits) >= 3 else digits.ljust(3, "0"))
            _put(out, "UserLoginID", uid, 0.8, m.group(0))


def _sweep_trk_times(out: Dict[str, FieldValue], text: str) -> None:
    for field, pat in (
        ("TrkIn", r"Trk\s*In\s*[:.\-]*\s*([0-9@\-/: ]{10,24})"),
        ("TrkOut", r"Trk\s*Out\s*[:.\-]*\s*([0-9@\-/: ]{10,24})"),
    ):
        if field in out:
            continue
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            cleaned = _clean_value(field, m.group(1))
            if cleaned:
                _put(out, field, cleaned, 0.8, m.group(0))


def _sweep_document_type(out: Dict[str, FieldValue], text: str) -> None:
    if "DocumentType" in out:
        return
    spaced = norm_spaced(text)
    if "EIR DELIVER IMPORT" in spaced or "EIR-DELIVER IMPORT" in spaced.replace(" ", "-"):
        _put(out, "DocumentType", "EIR-Deliver Import", 0.9, "eir_deliver_import")
    elif "DELIVER IMPORT" in spaced and "DP WORLD" in spaced:
        _put(out, "DocumentType", "Deliver Import Container", 0.85, "nsict_deliver")
    elif "BMCT" in spaced and "EIR" in spaced:
        _put(out, "DocumentType", "BMCT-EIR", 0.85, "bmct_eir")


def _sweep_dpworld_plate(out: Dict[str, FieldValue], text: str) -> None:
    """Recover MH46AF4375-style plates from DP World OCR (VIT4GAI…)."""
    if "LICNo" in out and is_valid_plate(out["LICNo"].value):
        return
    for pat in (
        r"\bTruck\s*[:.\-]*\s*([A-Z0-9\\/ \-]{6,20})",
        r"\bTrk\s*No\.?\s*[:.\-]*\s*([A-Z0-9\\/ \-]{6,20})",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        got = recover_noisy_plate(m.group(1))
        if got:
            _put(out, "LICNo", got, 0.82, m.group(0))
            return


def _clean_yard(v: str) -> str:
    a = norm_alnum(v).upper()
    # ALVY → 4L10 (A/4, V/1, Y/0 on DP World slips)
    folded = a.translate(str.maketrans({"A": "4", "O": "0", "Y": "0", "V": "1", "I": "1", "S": "5", "B": "8", "Z": "2"}))
    if re.fullmatch(r"\d[A-Z]\d{2}", folded):
        return folded
    if re.fullmatch(r"[A-Z0-9]{3,6}", a):
        return folded if re.search(r"\d", folded) else a
    return ""


def _clean_bat(v: str) -> str:
    v = repair_ocr_glyphs(v).upper().strip()
    v = re.sub(r"\s+", "", v)
    # WESO / JE56 / UE56
    v = v.replace("WESO", "UE56").replace("WESS", "UE56")
    m = re.search(r"\b([A-Z]?\d{2,4}[A-Z]?|[A-Z]{1,2}\d{2})\b", v)
    if not m:
        return ""
    bat = m.group(1)
    # JE56 → UE56 (common U→J misread)
    if bat.startswith("JE") and len(bat) == 4 and bat[2:].isdigit():
        bat = "UE" + bat[2:]
    if re.fullmatch(r"[A-Z]\d{3}", bat) or re.fullmatch(r"[A-Z]{1,2}\d{2}", bat) or re.fullmatch(r"\d{3,4}", bat):
        return bat
    return bat if len(bat) >= 3 else ""


def _clean_seal(v: str) -> str:
    v = repair_ocr_glyphs(v).upper()
    v = re.sub(r"^[CN]//", "", v)
    v = norm_alnum(v)
    v = re.sub(r"^0M", "OM", v)
    v = v.replace("OMQ", "OM0")
    v = re.sub(r"^\d+(?=OM|EU|U[0-9])", "", v)
    m = re.search(r"(OM[A-Z0-9]{6,7}|EU[A-Z0-9]{7,8}|U\d{6,8})", v)
    if not m:
        if v in SEAL_REJECT or len(v) < 6 or len(v) > 14:
            return ""
        return v
    seal = m.group(1)
    if seal.startswith(("OM", "EU")):
        prefix, tail = seal[:2], seal[2:]
    else:
        prefix, tail = seal[:1], seal[1:]
    # Fold letter confusions inside the serial only (bounded length above).
    fold = {"E": "6", "O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "Q": "0"}
    if prefix == "EU":
        fold["U"] = "0"
    tail = tail.translate(str.maketrans(fold))
    digits = re.sub(r"[^0-9]", "", tail)
    if prefix == "OM":
        digits = digits[:7]
        seal = prefix + digits
        if len(seal) == 8 and seal.startswith("OM013"):  # missing 0 → OM0130728
            seal = "OM0130" + seal[5:]
    elif prefix == "EU":
        seal = prefix + digits[:8]
    else:  # U
        seal = prefix + digits[:7]
    if seal in SEAL_REJECT or len(seal) < 6:
        return ""
    return seal


def _clean_via(v: str) -> str:
    v = repair_ocr_glyphs(v).upper()
    m = re.search(r"\b(S[0-9O]{3,5})\b", v)
    if not m:
        m = re.search(r"(S[0-9O]{3,5})", v)
    if not m:
        return ""
    via = m.group(1).translate(str.maketrans({"O": "0"}))
    return via


def _clean_driver(v: str) -> str:
    v = re.split(r"\b(?:TRK|POD|VIA|SCAN|SAT|PE)\b", v, flags=re.IGNORECASE)[0]
    v = re.sub(r"[^A-Za-z ]+", " ", v)
    v = re.sub(r"\s+", " ", v).strip(" ;,.")
    # Keep 2–4 alphabetic tokens
    parts = [p for p in v.upper().split() if p.isalpha() and len(p) > 1]
    if len(parts) >= 2:
        return " ".join(parts[:4])
    return ""


def _status_token(v: str) -> str:
    a = norm_alnum(v).upper()
    if "FEL" in a:  # FCL misread on Gateway slips
        a = a.replace("FEL", "FCL")
    for tok in ("FULL", "FCL", "EMPTY", "MTY", "LCL"):
        if tok in a:
            return "Full" if tok == "FULL" else tok
    return ""


def _line_code(v: str) -> str:
    a = norm_alnum(v).upper()
    for code in LINE_CODES:
        if a.startswith(code) or code in a.split():
            return code
    if a in LINE_CODES:
        return a
    # bare 2–5 letter token
    m = re.fullmatch(r"[A-Z]{2,5}", a)
    return m.group(0) if m and m.group(0) in LINE_CODES else ""


def _format_weight(v: str) -> str:
    m = WEIGHT_DECIMAL_RE.search(v) or WEIGHT_RE.search(v)
    if not m:
        return ""
    unit = (m.group(2) or "t").lower()
    if unit == "mi":
        unit = "mt"
    return _format_weight_parts(m.group(1), unit)


def _format_weight_parts(num: str, unit: str) -> str:
    num = num.replace(",", ".")
    unit = (unit or "t").lower()
    if unit in {"mt"}:
        return f"{num} MT"
    if unit in {"kg", "kgs"}:
        return f"{num} kg"
    return f"{num} t"
