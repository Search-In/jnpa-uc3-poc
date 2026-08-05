"""Bathymetry validation — PDF extraction measured against the reference dataset.

PDF is the PRIMARY and ONLY ingestion source. ``client-data/seed_bathy_soundings.sql`` is
a REFERENCE DATASET, never an input: it is the original pdfplumber extraction of these same
charts, so its per-survey tallies are the yardstick for whether our extraction agrees with
the one already accepted. Nothing here converts, loads or ingests that SQL.

Two things are locked:

  * ``TestExtractionAccuracy`` — parsed PDF vs the reference tallies, per survey.
  * ``TestCanonicalRoundTrip`` — the JSON API adapter consumes the SAME canonical model the
    PDF parser emits. Serialising PDF output as canonical JSON and re-parsing it must give
    byte-identical records, including ``row_sha256``. That is what lets a chart and an API
    push of the same sounding dedupe against each other instead of duplicating.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.marine.parsers import parse_marine
from services.marine.parsers.bathymetry_model import DOCUMENT_TYPE, TARGET_SOUNDING
from services.marine.parsers.bathymetry_pdf import parse_bathymetry_pdf

_CLIENT = Path(__file__).resolve().parents[2] / "client-data"
_SQL = _CLIENT / "seed_bathy_soundings.sql"
_PDF_DIR = _CLIENT / "2-JNPA_Sea_Channels_Bathymetry" / "Bathymetry Data"

# `-- <drawing_no>: 20890 soundings (6950 above design depth / red)`
_TALLY = re.compile(r"^--\s*(\S.*?):\s*(\d+)\s+soundings\s*\((\d+)\s+above design")

#: Charts whose extraction currently matches the reference EXACTLY. Regressions here are
#: real regressions. The remainder are tracked with a tolerance below.
_EXACT = {
    "34_3652-JNPA-POST-BC-304_-_Post_Dredge_Survey_B-C_Area",
    "34_3652-JNPA-POST-EA-322_-_Post_Dredge_Survey_EA_Area",
    "6148-SUR-PO-083-EF-SWB",
}
#: Charts still over-counting (legend/callout glyphs inside the neatline). Ratchet DOWN as
#: the residual is fixed; never up.
_TOLERANCE_PCT = 3.0
#: Carries no numeric depth glyphs at all — must reject, not silently return zero rows.
_NO_TEXT = "MB-005-25-BMCT-Chart_2k-Model"


def _have_pdfplumber() -> bool:
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


def _reference_tallies() -> dict[str, tuple[int, int]]:
    """Per-survey (soundings, above_design) from the reference SQL's own header lines.

    Read-only, and only the comment lines — the INSERT bodies are never touched. The lines
    are interleaved before each survey's first batch, so the whole file is scanned.
    """
    out: dict[str, tuple[int, int]] = {}
    with _SQL.open(encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("--"):
                m = _TALLY.match(line.rstrip())
                if m:
                    out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


_HAVE = _SQL.is_file() and _PDF_DIR.is_dir() and _have_pdfplumber()
pytestmark = pytest.mark.skipif(
    not _HAVE, reason="reference SQL, chart PDFs or pdfplumber absent")


def _charts() -> list[Path]:
    return sorted(_PDF_DIR.glob("*.pdf"))


@pytest.fixture(scope="module")
def reference() -> dict[str, tuple[int, int]]:
    ref = _reference_tallies()
    assert ref, "reference SQL carried no per-survey tallies"
    return ref


class TestExtractionAccuracy:
    def test_reference_covers_the_charts(self, reference):
        stems = {p.stem for p in _charts()} - {_NO_TEXT}
        missing = stems - set(reference)
        assert not missing, f"charts with no reference tally: {sorted(missing)}"

    @pytest.mark.parametrize("stem", sorted(_EXACT))
    def test_exact_charts_match_the_reference_exactly(self, stem, reference):
        pdf = _PDF_DIR / f"{stem}.pdf"
        if not pdf.is_file():
            pytest.skip(f"chart absent: {pdf.name}")
        gold_n, gold_red = reference[stem]
        res = parse_bathymetry_pdf(pdf.read_bytes(), pdf.name)
        assert not res.rejected
        assert len(res.records) == gold_n, f"{stem}: {len(res.records)} vs reference {gold_n}"
        red = sum(1 for r in res.records if r["above_design"])
        assert red == gold_red, f"{stem}: {red} red vs reference {gold_red}"

    def test_all_charts_are_within_tolerance(self, reference):
        """No chart may drift further than the recorded tolerance, and none may UNDER-count
        by more than a rounding margin — a shortfall means soundings are being lost."""
        problems: list[str] = []
        for pdf in _charts():
            stem = pdf.stem
            if stem == _NO_TEXT or stem not in reference:
                continue
            gold_n, _ = reference[stem]
            res = parse_bathymetry_pdf(pdf.read_bytes(), pdf.name)
            n = len(res.records)
            drift = 100.0 * (n - gold_n) / gold_n
            if drift > _TOLERANCE_PCT:
                problems.append(f"{stem}: +{drift:.2f}% ({n} vs {gold_n})")
            if drift < -0.1:
                problems.append(f"{stem}: LOST soundings {drift:.2f}% ({n} vs {gold_n})")
        assert not problems, "; ".join(problems)

    def test_chart_without_sounding_text_rejects(self):
        pdf = _PDF_DIR / f"{_NO_TEXT}.pdf"
        if not pdf.is_file():
            pytest.skip("MB-005 absent")
        res = parse_bathymetry_pdf(pdf.read_bytes(), pdf.name)
        assert res.rejected and not res.records

    def test_georeferenced_charts_reproject_into_the_jnpa_band(self, reference):
        pdf = _PDF_DIR / "34_3652-JNPA-POST-EA-322_-_Post_Dredge_Survey_EA_Area.pdf"
        if not pdf.is_file():
            pytest.skip("EA-322 absent")
        res = parse_bathymetry_pdf(pdf.read_bytes(), pdf.name)
        geo = [r for r in res.records if r["easting_m"] is not None]
        assert geo, "EA-322 has clean grid labels and must georeference"
        for r in geo[:300]:
            assert 18.0 <= r["lat"] <= 19.5 and 72.0 <= r["lon"] <= 73.5


class TestCanonicalRoundTrip:
    """The JSON API adapter must consume exactly what the PDF parser produces."""

    @staticmethod
    def _pdf_records(limit: int = 400):
        pdf = _PDF_DIR / "34_3652-JNPA-POST-BC-304_-_Post_Dredge_Survey_B-C_Area.pdf"
        if not pdf.is_file():
            pytest.skip("BC-304 absent")
        res = parse_bathymetry_pdf(pdf.read_bytes(), pdf.name)
        return pdf, res.records[:limit]

    def test_pdf_output_serialises_to_canonical_json_and_back_identically(self):
        pdf, recs = self._pdf_records()
        wire = [{k: r[k] for k in ("easting_m", "northing_m", "lat", "lon",
                                   "depth_m", "above_design", "page_x_pt", "page_y_pt")}
                for r in recs]
        doc = {"document_type": DOCUMENT_TYPE,
               "survey": {"drawing_no": recs[0]["drawing_no"]},
               "soundings": wire}
        via_json = parse_marine(json.dumps(doc).encode(), "roundtrip.json").records
        assert len(via_json) == len(recs)
        assert [r["row_sha256"] for r in via_json] == [r["row_sha256"] for r in recs], \
            "PDF and JSON arms must produce identical canonical records"

    def test_both_arms_emit_the_same_target_and_message(self):
        _, recs = self._pdf_records(20)
        assert {r["_target"] for r in recs} == {TARGET_SOUNDING}
        assert {r["_message"] for r in recs} == {DOCUMENT_TYPE}

    def test_survey_id_never_appears_on_the_wire(self):
        _, recs = self._pdf_records(20)
        assert all("survey_id" not in r for r in recs), \
            "survey_id is a per-database surrogate; the repository resolves it from drawing_no"
