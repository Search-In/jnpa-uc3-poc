"""Unit tests for LDB truck search normalize / mock (no upstream HTTP)."""
from __future__ import annotations

from gateway.routers.ldb import (
    _fmt_ist,
    _mock_truck,
    _normalize_truck_payload,
    _normalize_vahan_compliance,
)


def test_mock_truck_has_port_in_out():
    raw = _mock_truck("MH04BH0137")
    assert raw["truckNumber"] == "MH04BH0137"
    names = {e["eventName"] for e in raw["events"]}
    assert "PORT IN" in names and "PORT OUT" in names


def test_normalize_unwraps_nlds_response_body():
    # Real NLDS shape from https://ldb.co.in/api/ldbv2/truck/search
    live = {
        "status": "OK",
        "code": "SUC013",
        "responseBody": {
            "truckNumber": "MH43CQ0554",
            "truckType": "CONTAINERIZED",
            "events": [
                {
                    "eventTime": "1785573715000",
                    "eventName": "PORT OUT",
                    "locName": "Bharat Mumbai Container Terminals (PSA)",
                    "containerNumber": "HMMU4963884",
                    "transportMode": "TRUCK",
                },
                {
                    "eventTime": "1785504450000",
                    "eventName": "PORT IN",
                    "locName": "Bharat Mumbai Container Terminals (PSA)",
                    "containerNumber": "HMMU4963884",
                    "transportMode": "VESSEL",
                },
            ],
        },
    }
    tracking = _normalize_truck_payload(live, "MH43CQ0554")
    assert tracking["truckNumber"] == "MH43CQ0554"
    assert tracking["events"][0]["eventName"] == "PORT OUT"
    assert tracking["events"][0]["eventTimeLabel"] == "01-08-2026 14:11:55 IST"
    assert "HMMU4963884" in (tracking["alert"] or "")
    assert tracking["terminals"][0]["locName"].startswith("Bharat Mumbai")
    # Must not invent DEMO compliance in normalize — only LIVE Vahan fetch fills it.
    assert tracking["compliance"] is None


def test_normalize_vahan_compliance_from_ldb():
    raw = {
        "object": {
            "rcVhClassDesc": "Goods Carrier(HGV)",
            "rcFuelDesc": "DIESEL",
            "rcFitUpto": "19-Aug-2027",
            "rcTaxUpto": "31-Jul-2027",
            "rcInsuranceUpto": "31-Jul-2027",
            "rcPuccUpto": "20-Aug-2026",
            "rcPermitValidUpto": "30-Dec-2030",
            "rcVchCatg": None,
        },
        "responseCode": "200",
        "statusCode": "SUCCESS",
    }
    c = _normalize_vahan_compliance(raw)
    assert c is not None
    assert c["vehicleClass"] == "Goods Carrier(HGV)"
    assert c["fuelType"] == "DIESEL"
    assert c["fitnessValidUpto"] == "19-Aug-2027"
    assert c["taxValidUpto"] == "31-Jul-2027"
    assert c["insuranceValidUpto"] == "31-Jul-2027"
    assert c["pucValidUpto"] == "20-Aug-2026"
    assert c["permitValidUpto"] == "30-Dec-2030"
    assert c["source"] == "LDB_VAHAN"
    assert "owner" not in c
    assert "chassisNumber" not in c


def test_fmt_ist_epoch_millis():
    assert _fmt_ist("1785573715000") == "01-08-2026 14:11:55 IST"
