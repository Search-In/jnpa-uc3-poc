"""Bathymetry chart PDF -> sounding records. PHASE 1 PLACEHOLDER — not yet implemented.

The PDF arm of the bathymetry pipeline. The routing, registry entry, canonical model and
persistence target are all live; only chart EXTRACTION is outstanding, so this module
exists to make a bathymetry PDF fail LOUDLY and correctly rather than be misrouted.

Why a rejecting placeholder rather than no module at all
-------------------------------------------------------
``envelope.detect_format`` classifies every PDF as ``PDF``, and until now the registry
mapped ``PDF`` to exactly one parser — the port-craft fleet register. A bathymetry chart
uploaded without an explicit ``document_type`` was therefore fed to the port-craft regex,
matched nothing, and imported ZERO rows while still reporting ``ok``. A silent misroute is
strictly worse than a rejection, so this module returns an explicit typed rejection that
surfaces in the upload ledger as REJECTED with a clear reason.

Phase 2 will replace :func:`parse_bathymetry_pdf`'s body with real extraction. Its contract
is already fixed and MUST NOT change: read the chart, then hand the survey header and the
sounding list to :func:`bathymetry_model.emit_document` — the same call the JSON adapter
makes. Nothing else in the pipeline needs to move.

Planned extraction (see the Phase 2 design):
  * depth labels + page coordinates  — ``page.extract_words()`` -> ``x0`` / ``top``
  * ``above_design``                 — ``extract_words(extra_attrs=["non_stroking_color"])``,
                                       classifying the red plot colour
  * ``easting_m`` / ``northing_m``   — least-squares affine fitted from the chart's UTM
                                       grid tick labels; degrade to page-space when fewer
                                       than three control points are found
  * ``lat`` / ``lon``                — ``sea_channel_shp._utm43n_to_wgs84()`` (EPSG:32643),
                                       the transform already used for the sea channels
"""
from __future__ import annotations

from typing import Optional

from ..upload_parsers import ParseResult

#: Raised into the ParseResult (never as an exception) so the upload ledger records the
#: attempt with a reason, exactly like any other rejected marine upload.
NOT_IMPLEMENTED_CODE = "bathymetry_pdf_not_implemented"

NOT_IMPLEMENTED_DETAIL = (
    "Bathymetry chart PDF extraction is not implemented yet (Phase 2). The document was "
    "correctly identified as BATHYMETRY and was NOT misrouted to another parser. Use the "
    "canonical bathymetry JSON upload in the meantime."
)


def parse_bathymetry_pdf(content: bytes, filename: Optional[str] = None) -> ParseResult:
    """Placeholder: identify the document, then reject it explicitly.

    Returns a REJECTED :class:`ParseResult` — never raises, never returns a misleading
    empty-but-successful result. Phase 2 replaces the body; the signature is final.
    """
    res = ParseResult()
    res.rejected = True
    res.err(None, None, NOT_IMPLEMENTED_CODE,
            f"{NOT_IMPLEMENTED_DETAIL} (file: {filename or 'upload.pdf'})")
    return res


__all__ = ["parse_bathymetry_pdf", "NOT_IMPLEMENTED_CODE", "NOT_IMPLEMENTED_DETAIL"]
