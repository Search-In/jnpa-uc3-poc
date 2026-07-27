"""Bathymetry chart PDF -> canonical sounding records. Pure, no DB.

The PDF arm of the bathymetry pipeline. Extraction is VECTOR-ONLY (pdfplumber text +
colour); no OCR, and no new dependency — pdfplumber is already pinned for the berthing
and port-craft parsers.

How a sounding is drawn
-----------------------
The charts state their own convention in the legend::

    WATER DEPTH IN METRES & DECIMETRES BELOW CHART DATUM

so a depth is TWO text glyphs, not one: a larger token carrying metres and a smaller token
carrying decimetres. ``11.8`` is drawn as ``11`` (size ~7.1) plus ``8`` (size ~5.0); there
is no ``.`` glyph and no ``11.8`` token anywhere on the page. The two sizes differ between
chart families (7.1/5.0 on the 6148 series, 6.3/4.8 on the 34_3652 series), so they are
DETECTED per document from the numeric-token font histogram rather than hardcoded.

Colour carries the shoal flag:

    red   (1,0,0)          -> sounding above design depth
    black (0,0,0) / (0,)   -> normal sounding
    green (0,0.588,0)      -> NOT a sounding (design/target-depth annotation) — excluded,
                              and counted in a typed warning so the drop is never silent

Georeferencing
--------------
Charts carry UTM Zone 43N grid tick labels (``2098600N`` down the margin, ``273000E``
along the edge). Several sheets have ``/Rotate 270``, and on those the easting labels are
drawn at 90 deg — their characters carry ``upright=False`` and ``matrix[:4] == (0,1,-1,0)``.
pdfplumber groups words left-to-right, which reverses such a label (``283200E`` read as
``002382E``), so :func:`_oriented_text_runs` re-reads them along the matrix-derived advance
direction FIRST. Nothing is reversed by assumption; the PDF's own transformation metadata
decides the reading order.

The reassembled labels then feed a RANSAC-style consensus fit: two 1-D models (page-axis ->
easting, page-axis -> northing) describe an axis-aligned chart, each needing at least
MIN_CONTROL_POINTS agreeing labels, so a stray token cannot skew the model.

When no fit is possible the soundings are still emitted with easting/northing/lat/lon NULL
and ``georeferenced=false``. That is not a failure: 3 charts in the reference corpus print
no grid ticks at all, and the canonical model, schema and API all accept page-space-only
soundings. Depth data is never dropped for want of coordinates.

Output goes through :func:`bathymetry_model.emit_document` — the SAME call the JSON adapter
makes — so a sounding ingested from a chart and from the JSON API are byte-identical,
including ``row_sha256``.
"""
from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..upload_parsers import ParseResult
from .bathymetry_model import emit_document
from .sea_channel_shp import _utm43n_to_wgs84

#: Retained for the rejection path and for callers that assert on it.
NOT_IMPLEMENTED_CODE = "bathymetry_pdf_not_implemented"
NOT_IMPLEMENTED_DETAIL = (
    "Bathymetry chart PDF extraction produced no soundings for this document."
)

#: Minimum agreeing grid labels per axis before an affine is trusted.
MIN_CONTROL_POINTS = 3
#: Max |residual| in metres for a grid label to count as an inlier of the fitted line.
_FIT_TOLERANCE_M = 5.0
#: Metre-glyph -> decimetre-glyph search window, in points.
_PAIR_DX = (-2.0, 6.0)
_PAIR_DY = 6.0
#: A depth outside this band is not a plausible JNPA sounding.
_DEPTH_MIN, _DEPTH_MAX = 0.0, 60.0

_NUM = re.compile(r"^\d+$")
_NORTHING_LABEL = re.compile(r"^(\d{7})N$")
_EASTING_LABEL = re.compile(r"^(\d{6})E$")
_UTM_EAST_RANGE = (100_000.0, 900_000.0)
_UTM_NORTH_RANGE = (1_000_000.0, 3_000_000.0)


# --------------------------------------------------------------------------- colour
def _rgb(colour: Any) -> tuple[float, float, float]:
    """Normalise a pdfplumber colour to RGB. Greyscale ``(0.0,)`` -> ``(0,0,0)``."""
    if isinstance(colour, (list, tuple)):
        if len(colour) == 1:
            g = float(colour[0])
            return (g, g, g)
        if len(colour) >= 3:
            return (float(colour[0]), float(colour[1]), float(colour[2]))
    return (0.0, 0.0, 0.0)


def _is_red(colour: Any) -> bool:
    r, g, b = _rgb(colour)
    return r > 0.5 and g < 0.4 and b < 0.4


def _is_green(colour: Any) -> bool:
    r, g, b = _rgb(colour)
    return g > 0.35 and r < 0.4 and b < 0.4


# --------------------------------------------------------------------------- glyphs
def _glyph_sizes(numeric: Sequence[Mapping[str, Any]]) -> Optional[tuple[float, float]]:
    """(metre_size, decimetre_size) from the numeric-token font histogram.

    The two sounding glyph sizes dominate a chart by orders of magnitude (tens of thousands
    of tokens against a few dozen for labels), so the top two histogram entries are them.
    Detected per document — never hardcoded.
    """
    hist = Counter(round(float(w.get("size") or 0.0), 1) for w in numeric)
    top = [s for s, _ in hist.most_common(2) if s > 0]
    if len(top) < 2:
        return None
    return (max(top), min(top))


def _pair_soundings(numeric: Sequence[Mapping[str, Any]], metre_size: float,
                    deci_size: float) -> tuple[list[dict], int, int]:
    """Pair each metre glyph with its decimetre glyph.

    Returns (pairs, unpaired_metres, unpaired_decimetres). Pairing is nearest-neighbour to
    the RIGHT within a tight window, greedy in reading order; each decimetre glyph is
    consumed at most once so two adjacent soundings cannot share one.
    """
    def _size(w: Mapping[str, Any]) -> float:
        return round(float(w.get("size") or 0.0), 1)

    metres = [w for w in numeric if _size(w) == metre_size]
    decis = [w for w in numeric if _size(w) == deci_size]

    buckets: dict[int, list] = defaultdict(list)
    for d in decis:
        buckets[int(d["top"] // 4)].append(d)

    used: set[int] = set()
    pairs: list[dict] = []
    dx_lo, dx_hi = _PAIR_DX
    for m in sorted(metres, key=lambda w: (w["top"], w["x0"])):
        best = None
        best_dist = 1e9
        row = int(m["top"] // 4)
        for b in range(row - 2, row + 3):
            for d in buckets.get(b, ()):
                if id(d) in used:
                    continue
                dx = float(d["x0"]) - float(m["x1"])
                dy = abs(float(d["top"]) - float(m["top"]))
                if dx_lo <= dx <= dx_hi and dy <= _PAIR_DY:
                    dist = abs(dx) + dy
                    if dist < best_dist:
                        best_dist, best = dist, d
        if best is None:
            continue
        used.add(id(best))
        pairs.append({"metre": m, "deci": best})
    return pairs, len(metres) - len(pairs), len(decis) - len(used)


# --------------------------------------------------------------------------- rotated text
#: A char whose |b| dominates |a| in its matrix is set at 90/270 deg to the page.
_ROTATION_DOMINANCE = 0.7
#: Gap along the advance axis, in multiples of char height, that ends a text run.
_RUN_GAP_FACTOR = 3.0


def _advance_axis(matrix: Any) -> Optional[tuple[int, int]]:
    """Reading direction from a char's transformation matrix.

    A PDF text matrix ``(a, b, c, d, e, f)`` advances horizontal text along the vector
    ``(a, b)`` in USER space, where +y is up — which is DECREASING ``top`` in pdfplumber's
    top-down page space. Returns ``(axis, sign)``: axis 0 = page-x, 1 = page-y; sign +1
    means the text reads in increasing page-coordinate order, -1 decreasing.

    This is what makes rotated grid labels readable WITHOUT hardcoding any reversal: the
    matrix states the direction, so the characters are simply sorted along it. On the 270-deg
    sheets the label chars carry ``matrix[:4] == (0, 1, -1, 0)`` and ``upright=False``, i.e.
    a 90-deg rotation advancing +y, so they read in order of decreasing ``top``.
    """
    if not isinstance(matrix, (list, tuple)) or len(matrix) < 4:
        return None
    a, b = float(matrix[0]), float(matrix[1])
    mag = max(abs(a), abs(b))
    if mag < 1e-6:
        return None
    if abs(b) / mag >= _ROTATION_DOMINANCE:
        return (1, -1 if b > 0 else 1)
    return (0, 1 if a > 0 else -1)


def _run_from(chunk: Sequence[Mapping[str, Any]], axis: int) -> dict[str, Any]:
    """One reconstructed run, shaped like an ``extract_words`` entry."""
    return {
        "text": "".join(str(c["text"]) for c in chunk),
        "x0": min(float(c["x0"]) for c in chunk),
        "x1": max(float(c["x1"]) for c in chunk),
        "top": min(float(c["top"]) for c in chunk),
        "bottom": max(float(c["bottom"]) for c in chunk),
        "_rotated_axis": axis,
    }


def _oriented_text_runs(chars: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble ROTATED text into runs, in true reading order.

    pdfplumber groups words by increasing x then top, which is correct only for upright
    text. A 90-deg-rotated grid label therefore comes out reversed — ``283200E`` read as
    ``002382E``. Here each non-upright char is grouped with neighbours sharing an
    orientation and a perpendicular coordinate, then sorted along the matrix-derived
    advance direction, so the reconstruction is driven by the PDF's own metadata rather
    than by assuming labels are "backwards".
    """
    groups: dict[tuple[int, int, int], list] = defaultdict(list)
    for ch in chars:
        if ch.get("upright", True):
            continue                      # upright text is already correctly ordered
        adv = _advance_axis(ch.get("matrix"))
        if adv is None:
            continue
        axis, sign = adv
        # Perpendicular coordinate keys the run: vertical text shares x, horizontal shares y.
        perp = float(ch["x0"]) if axis == 1 else float(ch["top"])
        groups[(axis, sign, int(round(perp / 2.0)))].append(ch)

    runs: list[dict[str, Any]] = []
    for (axis, sign, _), members in groups.items():
        def _pos(c: Mapping[str, Any], _axis: int = axis) -> float:
            return float(c["top"]) if _axis == 1 else float(c["x0"])

        ordered = sorted(members, key=lambda c: _pos(c) * sign)
        chunk: list[Mapping[str, Any]] = []
        prev: Optional[float] = None
        for c in ordered:
            h = float(c.get("height") or c.get("size") or 4.0) or 4.0
            pos = _pos(c) * sign
            if prev is not None and (pos - prev) > h * _RUN_GAP_FACTOR:
                if chunk:
                    runs.append(_run_from(chunk, axis))
                chunk = []
            chunk.append(c)
            prev = pos
        if chunk:
            runs.append(_run_from(chunk, axis))
    return runs


# --------------------------------------------------------------------------- grid fit
def _label_candidates(words: Sequence[Mapping[str, Any]]) -> tuple[list, list]:
    """Candidate (value, x, y) grid ticks for easting and northing.

    ONE strategy: match the documented label forms (``2098600N`` / ``273000E``) against the
    supplied tokens. Upright labels arrive from ``extract_words``; rotated labels arrive
    already reassembled in true reading order by :func:`_oriented_text_runs`, so no digit
    reversal or fragment permutation is needed — by the time a rotated sheet's labels reach
    this function they are ordinary strings.

    Implausible values are dropped, and the consensus fit downstream discards anything that
    survives but disagrees with the majority.
    """
    east: list[tuple[float, float, float]] = []
    north: list[tuple[float, float, float]] = []

    for w in words:
        t = str(w["text"]).strip()
        m = _NORTHING_LABEL.match(t)
        if m:
            v = float(m.group(1))
            if _UTM_NORTH_RANGE[0] <= v <= _UTM_NORTH_RANGE[1]:
                north.append((v, float(w["x0"]), float(w["top"])))
            continue
        m = _EASTING_LABEL.match(t)
        if m:
            v = float(m.group(1))
            if _UTM_EAST_RANGE[0] <= v <= _UTM_EAST_RANGE[1]:
                east.append((v, float(w["x0"]), float(w["top"])))
    return east, north


def _fit_axis(cands: Sequence[tuple[float, float, float]]) -> Optional[tuple[int, float, float]]:
    """Fit ``value = m * page_axis + c`` by consensus.

    Returns ``(axis_index, m, c)`` where axis_index is 0 for page-x and 1 for page-y, or
    None when fewer than MIN_CONTROL_POINTS agree. Consensus (rather than least-squares over
    everything) is what makes a mis-decoded fragment harmless: it simply fails to join the
    inlier set.
    """
    best: Optional[tuple[int, float, float]] = None
    best_n = 0
    for axis in (0, 1):
        pts = [(c[0], c[1 + axis]) for c in cands]
        # Collapse duplicates: the same tick is often printed on both margins.
        uniq = sorted({(round(v, 3), round(p, 2)) for v, p in pts}, key=lambda t: t[1])
        if len(uniq) < 2:
            continue
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                v1, p1 = uniq[i]
                v2, p2 = uniq[j]
                if abs(p2 - p1) < 1e-6 or abs(v2 - v1) < 1e-6:
                    continue
                m = (v2 - v1) / (p2 - p1)
                c = v1 - m * p1
                n = sum(1 for v, p in uniq if abs((m * p + c) - v) <= _FIT_TOLERANCE_M)
                if n > best_n:
                    best_n, best = n, (axis, m, c)
    if best is None or best_n < MIN_CONTROL_POINTS:
        return None
    # Refine on the inliers (least squares) for a tighter model.
    axis, m, c = best
    pts = [(c0[0], c0[1 + axis]) for c0 in cands if abs((m * c0[1 + axis] + c) - c0[0]) <= _FIT_TOLERANCE_M]
    n = len(pts)
    if n >= 2:
        sx = sum(p for _, p in pts)
        sy = sum(v for v, _ in pts)
        sxx = sum(p * p for _, p in pts)
        sxy = sum(v * p for v, p in pts)
        denom = n * sxx - sx * sx
        if abs(denom) > 1e-9:
            m = (n * sxy - sx * sy) / denom
            c = (sy - m * sx) / n
    return (axis, m, c)


# --------------------------------------------------------------------------- metadata
_META_PATTERNS = {
    "survey_vessel": (
        re.compile(r'SURVEY\s+VESSEL\s*[:"]?\s*"?([A-Z][A-Z0-9 .\-]{2,30})"', re.I),
        re.compile(r"Vessel\s*:\s*([A-Z][A-Z0-9 .\-]{2,30}?)(?:\s+Survey|\s{2,}|$)", re.I),
    ),
    "survey_date": (
        re.compile(r"Survey\s+Date\s*:\s*([0-9]{1,2}[-/. ][A-Za-z0-9]{2,9}[-/. ][0-9]{2,4})", re.I),
        re.compile(r"SURVEY\s*[-–]\s*([A-Z]{3,9}\s+20\d{2})", re.I),
    ),
    "design_depth_m": (
        re.compile(r"Design\s+Depth\s*:\s*([\d.]+)\s*m", re.I),
        re.compile(r"DESIGN\s+DEPTH[^0-9]{0,60}?([\d.]+)\s*m", re.I),
    ),
    "section_label": (
        re.compile(r"Section\s+([A-Z]\s*-\s*[A-Z])", re.I),
        re.compile(r"NAVIGATIONAL\s+CHANNEL\s*\(\s*([A-Z]\s*-\s*[A-Z])\s*\)", re.I),
    ),
    "utm_zone": (
        re.compile(r"ZONE\s*[-:]?\s*(\d{1,2})", re.I),
        re.compile(r"Zone\s+(\d{1,2})\s*\)", re.I),
    ),
    "chainage": (
        re.compile(r"CH(?:AINAGE)?\.?\s*[:=]?\s*([\d+.]{3,15})", re.I),
    ),
}


def _survey_metadata(text: str, filename: Optional[str]) -> dict[str, Any]:
    """Survey header from the chart's title block.

    ``drawing_no`` comes from the FILENAME, deliberately: the most prominent drawing-number
    strings on these sheets are ``Match Line Refers To Drawing No.<X>``, which name the
    ADJACENT sheet, not this one. Using them would mislabel every chart.
    """
    meta: dict[str, Any] = {}
    for key, patterns in _META_PATTERNS.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                meta[key] = " ".join(m.group(1).split())
                break

    if "design_depth_m" in meta:
        try:
            meta["design_depth_m"] = float(meta["design_depth_m"])
        except ValueError:
            meta.pop("design_depth_m", None)

    up = text.upper()
    meta["chart_datum"] = "CD" if "BELOW CHART DATUM" in up else None
    meta["horizontal_datum"] = "WGS84" if "WGS" in up else None
    if "utm_zone" in meta:
        meta["utm_zone"] = f"{meta['utm_zone']}N"

    stem = re.sub(r"\.pdf$", "", filename or "", flags=re.I)
    meta["drawing_no"] = stem or None
    meta["source_file"] = filename
    return meta


# --------------------------------------------------------------------------- parser
def parse_bathymetry_pdf(content: bytes, filename: Optional[str] = None) -> ParseResult:
    """Parse a bathymetry chart PDF into canonical sounding records.

    Never raises: every failure mode becomes a typed error or warning on the ParseResult,
    matching every other marine parser.
    """
    res = ParseResult()

    try:
        import pdfplumber
    except ImportError:
        res.rejected = True
        res.err(None, None, "pdf_support_unavailable",
                "pdfplumber is not installed — bathymetry PDF extraction unavailable")
        return res

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not pdf.pages:
                res.rejected = True
                res.err(None, None, "empty_pdf", "the PDF has no pages")
                return res
            page = pdf.pages[0]
            # TWO extractions, deliberately:
            #  * with extra_attrs — needed for the sounding glyphs (font size selects the
            #    metre/decimetre pair, colour carries above_design);
            #  * plain — needed for the GRID LABELS. extra_attrs splits a word wherever an
            #    attribute changes, and on these charts the 'E'/'N' suffix is set in a
            #    different size from its digits, so `273000E` would fragment into `273000`
            #    and `E` and never match the label pattern.
            words = page.extract_words(extra_attrs=["non_stroking_color", "size"])
            # Upright labels, plus rotated labels reassembled from their char matrices. On
            # a 270-deg sheet the grid eastings are set at 90 deg, and pdfplumber's
            # left-to-right grouping would otherwise yield them reversed.
            label_words = list(page.extract_words()) + _oriented_text_runs(page.chars)
            text = page.extract_text() or ""
    except Exception as exc:  # noqa: BLE001 — a malformed upload must not raise
        res.rejected = True
        res.err(None, None, "pdf_parse_error", f"could not read the PDF: {exc}")
        return res

    meta = _survey_metadata(text, filename)

    numeric = [w for w in words if _NUM.match(w["text"])]
    sizes = _glyph_sizes(numeric)
    if sizes is None:
        res.rejected = True
        res.err(None, None, "no_sounding_glyphs",
                "no numeric depth glyphs found — this chart carries no extractable "
                "sounding text (e.g. a raster/model chart)")
        return res
    metre_size, deci_size = sizes

    pairs, unpaired_m, unpaired_d = _pair_soundings(numeric, metre_size, deci_size)
    if not pairs:
        res.rejected = True
        res.err(None, None, "no_soundings_paired",
                f"found {len(numeric)} numeric glyphs at sizes {metre_size}/{deci_size} "
                "but none could be paired into metre+decimetre soundings")
        return res

    # ---- georeference -------------------------------------------------------
    east_c, north_c = _label_candidates(label_words)
    fit_e = _fit_axis(east_c) if east_c else None
    fit_n = _fit_axis(north_c) if north_c else None
    georeferenced = fit_e is not None and fit_n is not None
    if not georeferenced:
        res.warn(None, None, "not_georeferenced",
                 f"could not fit a page->UTM grid ({len(east_c)} easting / {len(north_c)} "
                 f"northing candidates, need {MIN_CONTROL_POINTS} agreeing per axis); "
                 "soundings are emitted in page space only")

    # ---- build canonical soundings -----------------------------------------
    soundings: list[dict[str, Any]] = []
    green = 0
    out_of_range = 0
    for pr in pairs:
        m, d = pr["metre"], pr["deci"]
        colour = m.get("non_stroking_color")
        if _is_green(colour):
            green += 1
            continue
        try:
            depth = float(f"{int(m['text'])}.{int(d['text']) % 10}")
        except (ValueError, TypeError):
            continue
        if not (_DEPTH_MIN <= depth <= _DEPTH_MAX):
            out_of_range += 1
            continue

        px = float(m["x0"])
        py = float(m["top"])
        rec: dict[str, Any] = {
            "depth_m": depth,
            "above_design": _is_red(colour),
            "page_x_pt": round(px, 2),
            "page_y_pt": round(py, 2),
        }
        if georeferenced:
            axis_e, me, ce = fit_e            # type: ignore[misc]
            axis_n, mn, cn = fit_n            # type: ignore[misc]
            easting = me * (px if axis_e == 0 else py) + ce
            northing = mn * (px if axis_n == 0 else py) + cn
            lon, lat = _utm43n_to_wgs84(easting, northing)
            rec.update({"easting_m": round(easting, 2), "northing_m": round(northing, 2),
                        "lat": lat, "lon": lon})
        soundings.append(rec)

    # ---- typed reporting ----------------------------------------------------
    if green:
        res.warn(None, None, "green_annotation_excluded",
                 f"{green} green glyph pair(s) excluded — green marks design/target-depth "
                 "annotation on these charts, not a measured sounding")
    if unpaired_m or unpaired_d:
        res.warn(None, None, "unpaired_glyphs",
                 f"{unpaired_m} metre glyph(s) and {unpaired_d} decimetre glyph(s) could "
                 "not be paired and were skipped")
    if out_of_range:
        res.warn(None, None, "depth_out_of_range",
                 f"{out_of_range} pair(s) produced a depth outside "
                 f"{_DEPTH_MIN}-{_DEPTH_MAX} m and were skipped")

    survey = {k: v for k, v in meta.items() if v is not None}
    survey["georeferenced"] = georeferenced
    return emit_document(survey, soundings, filename=filename, res=res)


__all__ = ["parse_bathymetry_pdf", "NOT_IMPLEMENTED_CODE", "NOT_IMPLEMENTED_DETAIL",
           "MIN_CONTROL_POINTS"]
