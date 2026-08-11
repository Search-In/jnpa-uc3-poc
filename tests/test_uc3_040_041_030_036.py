"""UC3-040 Auto-LEO · UC3-041 document OCR · UC3-030 e-Challan · UC3-036 carbon.

Pure-function tests of the provenance rules these four tickets turn on. No DB and
no running server, so they hold in CI:

  * UC3-040 — the four-way join reports MATCH / MISMATCH / MISSING per stream,
    and a failed weighbridge raises WEIGHT_MISSING (X4) rather than passing.
  * UC3-041 — MOCK output can never be mistaken for a real read, a failed
    extraction is not labelled MOCK, and verification does not launder mock
    fields into verified ones.
  * UC3-030 — a challan always carries its SIMULATED disclosure, and the on-screen
    and PDF wording come from one source.
  * UC3-036 — every published factor carries a resolvable source and the idle
    delta is a difference in idle MINUTES, not in method.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared"), str(REPO_ROOT / "gate-data"),
          str(REPO_ROOT / "carbon")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gate_data.leo import (  # noqa: E402
    FLAG_ESEAL_TAMPER,
    FLAG_LEO_MISSING,
    FLAG_WEIGHT_MISMATCH,
    FLAG_WEIGHT_MISSING,
    SOURCE_MATCH,
    SOURCE_MISMATCH,
    SOURCE_MISSING,
    customs_alerts,
    reconcile,
)


def _rec(*, container="MEDU1777575", plate="MH43CK1959", declared=29350,
         measured=29600, tamper=False, leo="GRANTED",
         weighbridge=True, eseal=True, icegate=True):
    """One container's four streams, any of which may be absent."""
    return SimpleNamespace(
        container_no=container,
        vehicle_plate=plate,
        eseal=SimpleNamespace(eseal_id="5826371", container_no=container,
                              status="TAMPERED" if tamper else "ARMED",
                              tamper_flag=tamper, captured_at=None) if eseal else None,
        form13=SimpleNamespace(form13_no="16497850", container_no=container,
                               shipping_bill_no=None, cargo_desc=None,
                               gross_wt_kg=declared),
        weighbridge=SimpleNamespace(vehicle_plate=plate, container_no=container,
                                    measured_wt_kg=measured, axle_count=5,
                                    captured_at=None) if weighbridge else None,
        icegate=SimpleNamespace(shipping_bill_no=None, container_no=container,
                                leo_status=leo, igm_no=None,
                                assessment="FACILITATED") if icegate else None,
    )


# ----------------------------------------------------------------- UC3-040
def test_real_form13_case_within_tolerance_is_leo_ready():
    """The ticket's green case: VGM 29,350 vs weighbridge 29,600 = 0.85% < 2%."""
    r = reconcile("MEDU1777575", dataset={"MEDU1777575": _rec()})
    assert r.leo_ready is True
    assert r.customs_flags == []
    assert r.sources == {"eseal": SOURCE_MATCH, "form13": SOURCE_MATCH,
                         "weighbridge": SOURCE_MATCH, "icegate": SOURCE_MATCH}
    assert r.checks["weight_discrepancy_pct"] < 2.0


def test_weight_over_two_percent_raises_weight_mismatch():
    """The ticket's fail case: 31,000 against a 31,860 VGM = 2.70% > 2%."""
    rec = _rec(container="FFAU4770682", declared=31860, measured=31000)
    r = reconcile("FFAU4770682", dataset={"FFAU4770682": rec})
    assert r.leo_ready is False
    assert FLAG_WEIGHT_MISMATCH in r.customs_flags
    assert r.sources["weighbridge"] == SOURCE_MISMATCH
    assert r.checks["weight_discrepancy_pct"] > 2.0


def test_failed_weighbridge_raises_weight_missing_not_weight_mismatch():
    """X4. A weight that was never taken is a different problem from a wrong one,
    and must not be reported as agreement."""
    rec = _rec(container="BMOU5841115", weighbridge=False)
    r = reconcile("BMOU5841115", dataset={"BMOU5841115": rec})
    assert FLAG_WEIGHT_MISSING in r.customs_flags
    assert FLAG_WEIGHT_MISMATCH not in r.customs_flags
    assert r.sources["weighbridge"] == SOURCE_MISSING
    assert r.leo_ready is False
    # Nothing is invented in place of the absent reading.
    assert r.checks["weight_discrepancy_pct"] is None
    assert r.checks["weighbridge_measured_wt_kg"] is None


def test_weight_missing_alert_carries_the_x4_remedy_and_notifies_customs():
    rec = _rec(container="BMOU5841115", weighbridge=False)
    alerts = customs_alerts(reconcile("BMOU5841115", dataset={"BMOU5841115": rec}))
    wm = [a for a in alerts if a["payload"]["flag"] == FLAG_WEIGHT_MISSING]
    assert wm, "a missing weight must reach the customs feed"
    assert wm[0]["payload"]["remedy"] == "REROUTE_TO_ALTERNATE_WEIGHBRIDGE"
    assert wm[0]["payload"]["customs_notified"] is True


def test_pending_icegate_raises_leo_missing():
    r = reconcile("MEDU1777575", dataset={"MEDU1777575": _rec(leo="PENDING")})
    assert FLAG_LEO_MISSING in r.customs_flags
    assert r.sources["icegate"] == SOURCE_MISMATCH
    assert r.leo_ready is False


def test_tampered_eseal_raises_eseal_tamper():
    r = reconcile("MEDU1777575", dataset={"MEDU1777575": _rec(tamper=True)})
    assert FLAG_ESEAL_TAMPER in r.customs_flags
    assert r.sources["eseal"] == SOURCE_MISMATCH
    assert r.leo_ready is False


def test_absent_stream_is_missing_not_mismatch():
    """MISSING and MISMATCH must stay distinguishable: they have different fixes."""
    r = reconcile("MEDU1777575", dataset={"MEDU1777575": _rec(icegate=False)})
    assert r.sources["icegate"] == SOURCE_MISSING
    r2 = reconcile("MEDU1777575", dataset={"MEDU1777575": _rec(leo="PENDING")})
    assert r2.sources["icegate"] == SOURCE_MISMATCH


def test_unknown_container_reports_all_four_streams_missing():
    r = reconcile("NOSUCH1234567", dataset={})
    assert r.leo_ready is False
    assert set(r.sources.values()) == {SOURCE_MISSING}


# ----------------------------------------------------------------- UC3-041
def test_mock_ocr_values_can_never_be_read_as_real_identifiers():
    """The MOCK rung used to emit MH43BX0417 / MSMU1234567 — shaped exactly like
    the real corpus plate MH43BX1488 and container MSMU1908508. A value that
    looks real will be treated as real once it leaves the badged screen."""
    from gateway.routers.document_ocr import MOCK_VALUE_PREFIX, _mock_fields

    fields = _mock_fields("EIR", "seed")
    assert fields, "the EIR mock must still produce fields"
    for key, value in fields.items():
        assert str(value).startswith(MOCK_VALUE_PREFIX), (
            f"{key}={value!r} is indistinguishable from a real identifier")
    # And specifically not the real corpus values.
    assert fields["LICNo"] != "MH43BX1488"
    assert fields["ContainerNo"] != "MSMU1908508"


def test_mock_rung_reports_the_spec_confidence_and_badge():
    from gateway.routers.document_ocr import _extract_mock

    out = _extract_mock(b"", "EIR")
    assert out["source"] == "MOCK"
    assert out["confidence"] == 0.75  # WS2: deterministic MOCK fallback conf 0.75


def test_mock_extraction_is_deterministic():
    from gateway.routers.document_ocr import _extract_mock

    assert _extract_mock(b"abc", "EIR") == _extract_mock(b"abc", "EIR")


def test_field_provenance_keeps_mock_fields_visible_after_verification():
    """The defect: a verified record kept source='MOCK' with no way to tell an
    operator-corrected value from a mock one that simply survived."""
    from gateway.routers.document_ocr import _field_provenance

    row = {
        "fields": {"EIRNo": "4339869", "LICNo": "MH43BX1488",
                   "ContainerNo": "MOCK-CONTR-0014084"},
        "corrected_fields": ["EIRNo", "LICNo"],
        "source": "MOCK",
    }
    prov = _field_provenance(row)
    assert prov["EIRNo"] == "HUMAN_VERIFIED"
    assert prov["LICNo"] == "HUMAN_VERIFIED"
    assert prov["ContainerNo"] == "MOCK", (
        "an uncorrected mock field must stay visibly MOCK inside a VERIFIED record")


def test_field_provenance_reports_the_rung_when_nothing_was_corrected():
    from gateway.routers.document_ocr import _field_provenance

    prov = _field_provenance({"fields": {"EIRNo": "4339869"},
                              "corrected_fields": None, "source": "OCR"})
    assert prov == {"EIRNo": "OCR"}


# ----------------------------------------------------------------- UC3-030
def test_every_challan_payload_carries_the_simulated_disclosure():
    from gateway import enforcement

    d = enforcement.challan_disclosure()
    assert d["issuance_mode"] == "SIMULATED"
    assert d["badge"] == "SIMULATED"
    assert d["is_legal_instrument"] is False
    assert d["assumption_ref"] == "A5"
    assert "JNPA/RTO" in d["authority_note"]


def test_badge_challan_attaches_the_disclosure_and_leaves_none_alone():
    from gateway import enforcement

    assert enforcement.badge_challan(None) is None
    badged = enforcement.badge_challan({"challan_no": "ECH-2026-001001"})
    assert badged["challan_no"] == "ECH-2026-001001"
    assert badged["badge"] == "SIMULATED"


def test_screen_and_pdf_disclosure_are_literally_the_same_strings():
    """The badge must not be able to say one thing on screen and another on paper."""
    from gateway import enforcement
    from gateway.routers import reports

    assert enforcement.CHALLAN_DISCLOSURE is reports.CHALLAN_DISCLOSURE
    assert enforcement.CHALLAN_AUTHORITY_NOTE is reports.CHALLAN_AUTHORITY_NOTE


def test_exported_report_html_carries_the_simulated_ribbon():
    from gateway.routers import reports

    html = reports._render_html([{
        "id": "INC-1", "kind": "WRONG_WAY", "ts": "2026-06-12T10:00:00Z",
        "plate": "MH43CK1959", "severity": "critical",
        "challan": {"action": "Wrong-way driving", "section": "MVA 184",
                    "fine_inr": 5000},
        "rc": {}, "evidence": {},
    }])
    assert "SIMULATED" in html
    assert reports.CHALLAN_AUTHORITY_NOTE in html
    assert reports.CHALLAN_DISCLOSURE in html


# ----------------------------------------------------------------- UC3-036
def test_every_published_factor_names_a_resolvable_source():
    """UC3-036's two-click rule needs the source to be resolvable, not a hint."""
    import factors

    m = factors.method()
    for block in (m["idle"], m["moving"]):
        assert block["factors"], "a method block with no factors discloses nothing"
        for f in block["factors"]:
            assert f["source"] in m["sources"], f"{f['source']} has no citation"
            assert f["derivation"], "a factor without its arithmetic is a magic number"


def test_carbon_method_declares_published_factors_and_simulated_activity():
    import factors

    m = factors.method()
    assert m["assumption_ref"] == "A-06"
    assert m["factors_are_published"] is True
    assert m["activity_data_is_simulated"] is True
    assert m["idle"]["activity_data"]["provenance"] == "SIMULATED"


def test_idle_factors_match_the_constants_the_calculator_applies():
    """The method panel must not be able to advertise a factor the service does
    not use — that is the drift this single source exists to prevent."""
    import factors

    for f in factors.idle_method()["factors"]:
        assert f["value"] == factors.GCO2E_PER_IDLE_MINUTE[f["vehicle_class"]]
    for f in factors.moving_method()["factors"]:
        assert f["value"] == factors.GCO2E_PER_TONNE_KM[f["vehicle_class"]]


def test_idle_delta_is_a_difference_in_minutes_not_in_method():
    """Both arms use the same factor, so the delta reflects idle time alone."""
    # calculator uses a package-relative import, so it is loaded as part of the
    # `carbon` package rather than as a bare module (unlike factors, which the
    # other tests import directly).
    from carbon import calculator, factors

    baseline = calculator.idle_emissions_kg(3582, "HGV")
    scenario = calculator.idle_emissions_kg(2687, "HGV")
    factor = factors.idle_minute_factor("HGV")

    assert baseline == round(3582 * factor / 1000.0, 3)
    assert scenario == round(2687 * factor / 1000.0, 3)
    assert scenario < baseline
    # The Monsoon Friday arc quoted in the ticket: ~480 kg -> ~360 kg.
    assert 470 <= baseline <= 490
    assert 350 <= scenario <= 370
