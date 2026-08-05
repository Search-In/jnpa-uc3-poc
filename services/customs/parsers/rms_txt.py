"""RMS (Container Scanning Division selection list) parser — the risk-management
scanning selection customs produces per IGM.

Format is a fixed-header plain-text report:

    CONTAINER SCANNING DIVISION
    Cus.HouseJNCHNhavaSheva-INNSA1
    Shipping Line  : ...
    Shipping Agent : ...
    IGM No.        : 1191409/2026        IGM Date : 02/2026/5 00:05:00
    Processing End Date : 2026-06-09
    Vessel Name    : ...
    Subject        : SCANNING LIST For IGM NO. 1191409
    ----
    Sl.No.   Container No.            CFS Name.        Goods Desc.
    1        BWLU9101815(D-INNSA1RSDT02)  <CFS name>  <goods desc>
    ...

A file may instead declare "No container selected for scanning" (``any_selected``
False, no rows).

Reliably extracted per row: sl_no, container_no, scan_machine (M/F/D) and
scan_location from the ``(D-INNSA1RSDT02)`` token. CFS name vs goods description is
split on the first run of 2+ spaces (correct for the space-padded rows; on the
occasional unpadded row the CFS name absorbs the leading goods text — an
informational-only imperfection that never affects the container/scan fields).
Goods-description continuation lines (no leading Sl.No + container) append to the
previous row.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from jnpa_shared.iso6346 import is_valid_container_no

from .common import CustomsParseError, ParsedMessage, clean, parse_iso_date

# Sl.No  CONTAINER(<machine>-<location>)  <rest = cfs + goods>
_ROW = re.compile(
    r"^\s*(\d+)\s+([A-Za-z]{4}\d{5,7})\(([A-Za-z])-([^)]*)\)\s*(.*)$")


def _field(lines: list[str], label: str) -> Optional[str]:
    """Value after ``<label> : ...`` on the first matching line (before any 2nd ':' field)."""
    for ln in lines:
        if ln.strip().startswith(label):
            after = ln.split(":", 1)[1] if ":" in ln else ""
            return clean(after)
    return None


def parse_rms_txt(path: str) -> ParsedMessage:
    """Parse an RMS scanning-list ``.txt`` into a :class:`ParsedMessage`.

    ``payload = {"scanlist": {header...}, "containers": [ {row...} ]}``.
    ``record_count`` = number of selected containers."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        raise CustomsParseError(f"unreadable RMS file {path}: {exc}") from exc

    lines = raw.splitlines()
    if not lines or "CONTAINER SCANNING DIVISION" not in (lines[0] if lines else ""):
        raise CustomsParseError(f"not an RMS scanning list: {path}")

    # --- header ---------------------------------------------------------------
    customs_house = None
    for ln in lines:
        if ln.startswith("Cus.House"):
            customs_house = clean(ln.rsplit("-", 1)[-1]) if "-" in ln else clean(ln)
            break

    igm_no: Optional[str] = None
    igm_date_raw: Optional[str] = None
    for ln in lines:
        if ln.strip().startswith("IGM No."):
            # "IGM No. : 1191409/2026   IGM Date : 02/2026/5 00:05:00"
            m = re.search(r"IGM No\.\s*:\s*([0-9]+)", ln)
            if m:
                igm_no = m.group(1)
            dm = re.search(r"IGM Date\s*:\s*(.+)$", ln)
            if dm:
                igm_date_raw = clean(dm.group(1))
            break

    scanlist = {
        "customs_house": customs_house,
        "shipping_line": _field(lines, "Shipping Line"),
        "shipping_agent": _field(lines, "Shipping Agent"),
        "igm_no": igm_no,
        "igm_date": None,          # raw format is ambiguous (DD/YYYY/M); keep raw only
        "igm_date_raw": igm_date_raw,
        "processing_end_date": parse_iso_date(_field(lines, "Processing End Date")),
        "vessel_name": _field(lines, "Vessel Name"),
        "subject": _field(lines, "Subject"),
    }

    # --- container rows -------------------------------------------------------
    containers: list[dict[str, Any]] = []
    for ln in lines:
        m = _ROW.match(ln)
        if m:
            sl_no, cn, machine, location, rest = m.groups()
            parts = re.split(r"\s{2,}", rest.strip(), maxsplit=1)
            cfs_name = clean(parts[0]) if parts else None
            goods_desc = clean(parts[1]) if len(parts) > 1 else None
            containers.append({
                "sl_no": int(sl_no),
                "container_no": cn,
                "iso_valid": is_valid_container_no(cn),
                "scan_machine": machine.upper(),
                "scan_location": clean(location),
                "cfs_name": cfs_name,
                "goods_desc": goods_desc,
                "igm_no": igm_no,
            })
        elif containers and ln.strip() and not ln.strip().startswith((
                "M -", "F -", "D -", "No Container", "No container", "-", "Sl.No")):
            # goods-description continuation for the previous row
            tail = clean(ln)
            if tail:
                prev = containers[-1]
                prev["goods_desc"] = " ".join(x for x in (prev.get("goods_desc"), tail) if x)

    scanlist["any_selected"] = bool(containers)
    scanlist["selected_count"] = len(containers)

    message = {
        "message_type": "RMS",
        "module": "RMS",
        "primary_ref": igm_no,
    }
    return ParsedMessage(message=message,
                         payload={"scanlist": scanlist, "containers": containers},
                         record_count=len(containers))
