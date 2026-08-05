"""CANONICAL bathymetry model — the one shape every bathymetry source produces.

This module is the contract between the two planned ingestion arms and the repository::

    PDF  -> bathymetry_pdf.py  --\\
                                  >-- canonical records -> repository -> core.bathymetry_*
    JSON -> bathymetry_json.py --/

Both arms MUST build their records through :func:`emit_document` so a sounding ingested
from a chart PDF and the same sounding ingested from the JSON API are byte-identical —
including ``row_sha256``, which is what makes cross-source re-ingest idempotent rather
than duplicating.

Pure: no DB, no API, no I/O. Mirrors the existing marine parser convention — every record
carries ``_target`` / ``_message`` / ``_source_file`` and nothing is ever silently dropped.

CANONICAL DOCUMENT (the wire model, and what a chart PDF is parsed INTO)::

    {
      "document_type": "BATHYMETRY",
      "survey":   {"drawing_no": "6148-24-SUR-PO-119-JNPA", ...},
      "soundings": [{"depth_m": 11.8, "page_x_pt": 2739.2, "page_y_pt": 51.4,
                     "easting_m": 271805.2, "northing_m": 2087828.9,
                     "lat": 18.869910, "lon": 72.833965, "above_design": true}, ...]
    }

``drawing_no`` — NOT ``survey_id`` — is the join key. ``survey_id`` is a per-database
``GENERATED ALWAYS AS IDENTITY`` surrogate and must never appear on the wire; the
repository resolves it from ``drawing_no``, exactly as the generated seed SQL does with
its ``WITH s AS (SELECT survey_id … WHERE drawing_no = …)`` lookup.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Optional

from ..upload_parsers import ParseResult

DOCUMENT_TYPE = "BATHYMETRY"
MESSAGE = "BATHYMETRY"

#: Persistence target for a sounding record (see repository._KNOWN).
TARGET_SOUNDING = "bathymetry_sounding"

#: Numeric sounding fields, in the column order of core.bathymetry_sounding.
NUMERIC_FIELDS = ("easting_m", "northing_m", "lat", "lon",
                  "depth_m", "page_x_pt", "page_y_pt")

#: The only field a sounding cannot be without — position may be unknown, depth may not.
REQUIRED_FIELDS = ("depth_m",)

#: Survey-header fields carried on the canonical document (Phase 1 reads drawing_no only;
#: the rest are preserved for the Phase 2 survey upsert so no client data is lost).
SURVEY_FIELDS = ("drawing_no", "section_label", "design_depth_m", "survey_start",
                 "survey_end", "survey_vessel", "chart_datum", "georeferenced",
                 "source_file")


def _num(v: Any) -> Optional[float]:
    """Tolerant numeric coercion. None/''/non-numeric -> None (an ABSENT measurement)."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN/inf are not measurements; treat as absent rather than poisoning a numeric column.
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _flag(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "t", "1", "yes", "y", "red"}


def row_sha256(drawing_no: Optional[str], rec: Mapping[str, Any]) -> str:
    """Stable content identity for one sounding.

    Built from the survey key plus every positional/measured field, so the SAME sounding
    arriving via PDF and via JSON hashes identically and the second import is a no-op.
    Page coordinates are included because they are the only position an ungeoreferenced
    chart has — without them two distinct soundings at equal depth would collide.
    """
    parts = [str(drawing_no or "")]
    for f in NUMERIC_FIELDS:
        v = rec.get(f)
        parts.append("" if v is None else f"{float(v):.4f}")
    parts.append("1" if rec.get("above_design") else "0")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalise_sounding(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce one raw sounding onto the canonical field set. Does NOT validate."""
    out: dict[str, Any] = {f: _num(raw.get(f)) for f in NUMERIC_FIELDS}
    out["above_design"] = _flag(raw.get("above_design"))
    return out


def sounding_errors(rec: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """(error_code, detail) when a normalised sounding is unusable, else None."""
    for f in REQUIRED_FIELDS:
        if rec.get(f) is None:
            return ("missing_depth", f"sounding has no usable {f}")
    if rec.get("lat") is not None and not (-90.0 <= float(rec["lat"]) <= 90.0):
        return ("lat_out_of_range", f"lat {rec['lat']} outside [-90, 90]")
    if rec.get("lon") is not None and not (-180.0 <= float(rec["lon"]) <= 180.0):
        return ("lon_out_of_range", f"lon {rec['lon']} outside [-180, 180]")
    return None


def make_sounding_record(raw: Mapping[str, Any], *, drawing_no: Optional[str],
                         source_file: Optional[str]) -> dict[str, Any]:
    """One canonical, repository-ready sounding record (tagged, hashed, coerced)."""
    rec = normalise_sounding(raw)
    rec.update({
        "_target": TARGET_SOUNDING,
        "_message": MESSAGE,
        "_source_file": source_file,
        # The repository resolves survey_id from this; it is never supplied by a client.
        "drawing_no": drawing_no,
        "row_sha256": row_sha256(drawing_no, rec),
    })
    return rec


def emit_document(survey: Mapping[str, Any], soundings: Iterable[Mapping[str, Any]],
                  *, filename: Optional[str], res: ParseResult) -> ParseResult:
    """Fill ``res`` from one canonical bathymetry document. THE shared entry point.

    Both the PDF parser and the JSON adapter funnel through here, so validation, tagging,
    hashing and the preview are defined exactly once. Never raises: an unusable sounding
    becomes a typed row error and the rest of the document still imports.
    """
    drawing_no = (str(survey.get("drawing_no") or "").strip() or None)
    if drawing_no is None:
        res.rejected = True
        res.err(None, "drawing_no", "missing_drawing_no",
                "survey.drawing_no is required: it is the key that resolves survey_id")
        return res

    rows = list(soundings)
    res.row_count = len(rows)
    if not rows:
        # Explicit: an empty document is REJECTED, never a silent zero-row success.
        res.rejected = True
        res.err(None, "soundings", "no_soundings",
                f"no soundings in the document for drawing_no {drawing_no}")
        return res

    seen: set[str] = set()
    for i, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping):
            res.err(i, None, "bad_sounding", f"sounding {i} is not an object")
            res.invalid_count += 1
            continue
        rec = make_sounding_record(raw, drawing_no=drawing_no, source_file=filename)
        bad = sounding_errors(rec)
        if bad is not None:
            res.err(i, bad[0].split("_")[0], bad[0], bad[1])
            res.invalid_count += 1
            continue
        if rec["row_sha256"] in seen:
            res.duplicate_count += 1
            res.warn(i, None, "duplicate_in_file",
                     f"sounding {i} repeats an identical sounding earlier in this file")
            continue
        seen.add(rec["row_sha256"])
        res.records.append(rec)

    georef = sum(1 for r in res.records if r.get("easting_m") is not None)
    res.preview = [{
        "Drawing": drawing_no,
        "Depth (m)": r.get("depth_m"),
        "Above design": r.get("above_design"),
        "Easting": r.get("easting_m"),
        "Northing": r.get("northing_m"),
        "Page X": r.get("page_x_pt"),
        "Page Y": r.get("page_y_pt"),
    } for r in res.records[:20]]
    if res.records and georef == 0:
        # Not an error: 3 of the 11 charts in the existing corpus are page-space only.
        res.warn(None, None, "not_georeferenced",
                 f"{drawing_no}: no sounding carries easting/northing — page coordinates only")
    return res


__all__ = [
    "DOCUMENT_TYPE", "MESSAGE", "TARGET_SOUNDING",
    "NUMERIC_FIELDS", "REQUIRED_FIELDS", "SURVEY_FIELDS",
    "row_sha256", "normalise_sounding", "sounding_errors",
    "make_sounding_record", "emit_document",
]
