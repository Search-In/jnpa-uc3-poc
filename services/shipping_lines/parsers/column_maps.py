"""Alias-driven mapping from heterogeneous per-terminal headers to the canonical
IAL/EAL container line-item dict.

The five terminals export the same business facts under different header names
(``ContainerNbr`` / ``ContainerNo`` / ``Container`` / ``Container No``; weights in
KG or MT; ``Line`` vs ``ContainerOperator`` vs ``Opr``). Rather than a bespoke
parser per file, every tabular parser normalises its headers with
:func:`services.shipping_lines.parsers.common.norm_header` and resolves canonical
fields through :data:`ALIASES` here — so adding a terminal usually means adding an
alias, not a parser.
"""
from __future__ import annotations

from typing import Any, Optional

from .common import (
    clean,
    norm_category,
    norm_container,
    norm_freight_kind,
    norm_header,
    norm_intish,
    resolve_weight,
    to_num,
)

# canonical field -> ordered list of accepted source headers (normalised via
# norm_header: lowercased, non-alphanumerics stripped, so "Container No" ->
# "containerno", "CNTR_NO" -> "cntrno", "Container Number" -> "containernumber").
# Order matters: the first present, non-empty match wins. Broad on purpose so future
# uploads with common header variations map without code changes (dynamic mapping).
ALIASES: dict[str, tuple[str, ...]] = {
    "container_no": ("containerno", "containernumber", "containernbr", "container",
                     "cntrno", "cntrnumber", "cntr", "containerid", "equipmentno",
                     "equipmentnumber", "unitno", "boxno", "containerno1"),
    "iso_code": ("iso", "isocode", "isotype", "isosizetype", "sizetype", "eqptype",
                 "equipmenttype", "containertype", "contisocode", "type"),
    "freight_kind": ("freightkind", "loadstatus", "status", "fclmty", "fullempty",
                     "emptyfull", "ffkind"),
    "category_src": ("category", "cat", "cargocategory", "tradetype", "tradecategory"),
    "shipping_status": ("shippingstatuscode", "shippingstatus", "movementtype", "exim"),
    "pol": ("pol", "portofloading", "loadport", "loadingport"),
    "pod": ("pod", "portofdischarge", "dischargeport", "destinationport"),
    "destination": ("dst", "destination", "finaldestination", "finalportofdischarge",
                    "finalpod"),
    "shipping_line": ("shippinglinecode", "shippingline", "line", "linecode",
                      "containeroperator", "operator", "opr", "carriercode", "carrier",
                      "scac", "mlocode", "mlo"),
    "vessel_visit": ("vslvisit", "vesselvisit", "obvesselvisit", "obvessel", "vesselcode",
                     "vcn", "vesselvoyage", "visit"),
    "voyage": ("voyagenumber", "voyageno", "voyage", "voy"),
    "bill_of_lading": ("billoflading", "billofladingno", "bl", "blno", "blnumber",
                       "mblno", "hblno", "bolno", "bol"),
    "seal_no": ("sealno", "seal", "seal1", "sealnumber", "declaredsealnumber1",
                "sealnumber1", "lineseal"),
    "reefer_status": ("reeferstatus", "reefersts", "reefer"),
    "reefer_temp": ("reefertemp", "temp", "temperature", "settemp"),
    "reefer_uom": ("reefertempunit", "tempunit", "tempuom", "tempunitofmeasure"),
    "imdg_code": ("imdgcode", "imdg", "imdg1", "imo", "imo1", "imcos", "hazcode",
                  "imoclass"),
    "un_number": ("unnumber", "unno", "unno1", "unnbr1", "un1", "unnbr"),
    "group_code": ("groupcode", "group", "grp"),
    "client_code": ("clientcode", "client", "consigneecode"),
    "departure_mode": ("departuremode", "deliverymode", "modeofdeparture", "evacmode",
                       "evacuationmode"),
    "nominated_cfs": ("nominatedcfs", "cfs", "preferredcfsafter48hours", "cfsafter48hrs",
                      "preferredcfs"),
    "iec_code": ("ieccode", "iec", "iecno", "importexportcode"),
    "gst_no": ("gstno", "gst", "gstin", "gstinno"),
    "commodity_code": ("commoditycode", "commodity", "hscode", "cargotype"),
}


def pick(row_norm: dict[str, Any], canonical: str) -> Optional[str]:
    """First present, non-empty source value for a canonical field (via ALIASES)."""
    for src in ALIASES.get(canonical, ()):  # normalised header names
        if src in row_norm:
            v = clean(row_norm[src])
            if v is not None:
                return v
    return None


def map_container_row(
    raw_row: dict[str, Any],
    *,
    list_type: str,
    terminal: str,
) -> Optional[dict[str, Any]]:
    """Map one source row (``{original_header: value}``) to a canonical container dict.

    Returns ``None`` when the row carries no usable container number (blank/footer
    rows), so callers can skip it. ``raw`` preserves the full original row losslessly.
    """
    # Normalised view for alias lookups; keep the original for `raw`.
    row_norm = {norm_header(k): v for k, v in raw_row.items() if norm_header(k)}

    container_no = norm_container(pick(row_norm, "container_no"))
    if not container_no:
        return None

    # Category: explicit column first, else the EX/IM shipping-status flag, else the
    # list direction (an EAL row is an export, an IAL row is an import).
    category = norm_category(pick(row_norm, "category_src"))
    if category is None:
        category = norm_category(pick(row_norm, "shipping_status"))
    if category is None:
        category = "EXPORT" if list_type == "EAL" else "IMPORT"

    gross_kg, uom = resolve_weight(row_norm)

    from .common import container_valid  # local import keeps common dependency-light

    return {
        "list_type": list_type,
        "terminal": terminal,
        "container_no": container_no,
        "container_valid_iso": container_valid(container_no),
        "iso_code": norm_intish(pick(row_norm, "iso_code")),
        "freight_kind": norm_freight_kind(pick(row_norm, "freight_kind")),
        "category": category,
        "gross_weight_kg": gross_kg,
        "weight_source_uom": uom,
        "pol": pick(row_norm, "pol"),
        "pod": pick(row_norm, "pod"),
        "destination": pick(row_norm, "destination"),
        "shipping_line_code": pick(row_norm, "shipping_line"),
        "vessel_visit": pick(row_norm, "vessel_visit"),
        "voyage": pick(row_norm, "voyage"),
        "bill_of_lading": pick(row_norm, "bill_of_lading"),
        "seal_no": pick(row_norm, "seal_no"),
        "reefer_status": pick(row_norm, "reefer_status"),
        "reefer_temp": to_num(pick(row_norm, "reefer_temp")),
        "reefer_uom": pick(row_norm, "reefer_uom"),
        "imdg_code": pick(row_norm, "imdg_code"),
        "un_number": norm_intish(pick(row_norm, "un_number")),
        "group_code": pick(row_norm, "group_code"),
        "client_code": pick(row_norm, "client_code"),
        "departure_mode": pick(row_norm, "departure_mode"),
        "nominated_cfs": pick(row_norm, "nominated_cfs"),
        "iec_code": pick(row_norm, "iec_code"),
        "gst_no": pick(row_norm, "gst_no"),
        "commodity_code": pick(row_norm, "commodity_code"),
        "raw": {str(k): (clean(v) if not isinstance(v, (int, float)) else v)
                for k, v in raw_row.items() if str(k).strip()},
    }
