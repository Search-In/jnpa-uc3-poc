"""Vehicle 360 aggregation — shaping rules, in-process (no DB, no infra).

``vehicle_360()`` splits into an I/O half (six concurrent lookups) and a pure
shaping half (``_shape_360`` / ``_build_360_timeline``). These tests drive the
pure half with representative rows so the operator-visible contract — which card
is populated, what "Not Available" means, how a blacklist resolves and what the
lifecycle timeline contains — is pinned without a Postgres.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway import vehicle_intel as vi  # noqa: E402

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=365)).isoformat()
SOON = (TODAY + timedelta(days=10)).isoformat()
PAST = (TODAY - timedelta(days=5)).isoformat()

PLATE = "MH04QA9911"

RC = {
    "plate": PLATE,
    "vehicle_class": "HGV",
    "fuel_type": "Diesel",
    "blacklist_status": "CLEAR",
    "registration_date": "2019-05-12",
    "insurance_valid_to": FUTURE,
    "puc_valid_to": SOON,
    "fitness_valid_to": PAST,
    "rto_code": "MH04",
    "state": "MH",
    "owner_name_masked": "R**** S****",
}
INTEL = {"vehicle_number": PLATE, "rc": RC, "tracking": [], "violations": [],
         "challans": [], "alerts": [{"kind": "OVERSPEED", "severity": "HIGH",
                                     "ts": "2026-08-01T10:00:00+00:00"}],
         "verification_history": []}
VEH = {"vehicle_id": "TRK-000123", "vehicle_no": PLATE, "vehicle_type": "Container Truck",
       "status": "ACTIVE", "chassis_number": "CH123", "rfid_fastag_id": "FT9",
       "created_at": "2026-01-01T00:00:00+00:00"}
DRV = {"driver_id": "DRV-77", "name": "A. Kumar", "license_no": "MH0420110012345",
       "mobile": "98******21", "vehicle_no": PLATE, "status": "ACTIVE",
       "photo_url": "/media/drv77.jpg", "enrolled_at": "2026-02-01T00:00:00+00:00"}
TRN = {"transporter_id": 7, "company_name": "Konkan Logistics", "code": "KL-07",
       "status": "ACTIVE", "gstin": "27AAA", "contact": "022-1234",
       "mapped_at": "2026-01-15T00:00:00+00:00", "blacklisted_at": None,
       "blacklist_reason": None, "blacklist_severity": None}
LIC = {"licence_number": "MH0420110012345", "licence_type": "HMV",
       "licence_valid_to": FUTURE, "latest_pdp_number": "PDP-9", "pdp_active": True,
       "pdp_valid_until": FUTURE, "date_of_birth": "1985-03-04",
       "driver_name": "A. Kumar", "company_name": "Konkan Logistics",
       "master_status": "ACTIVE", "transporter_id": 7,
       "transporter_name": "Konkan Logistics", "transporter_code": "KL-07",
       "transporter_status": "ACTIVE"}
ENR = {"status": "ACTIVE", "consent": True, "submitted_at": "2026-01-20T00:00:00+00:00"}
VER = {"decision": "VERIFIED", "score": 0.94, "ts": "2026-07-01T09:00:00+00:00"}
JOBS = [{"id": 5, "container_number": "MSCU1234567", "move_type": "IMPORT_PICK",
         "status": "AT_GATE", "terminal": "BMCT", "assigned_at": "2026-08-01T06:00:00+00:00",
         "accepted_at": "2026-08-01T06:30:00+00:00", "completed_at": None,
         "vehicle_no": PLATE, "vehicle_id": "TRK-000123"}]
GATES = [{"ts": "2026-08-01T07:00:00+00:00", "gate_id": "G1", "event_type": "GATE_IN",
          "trip_id": "T-1"}]


def shape(**over):
    args = {"plate": PLATE, "intel": INTEL, "veh": VEH, "drv": DRV, "trn": TRN,
            "gates": GATES, "jobs": JOBS, "licence": LIC, "enrollment_row": ENR,
            "verification": VER}
    args.update(over)
    return vi._shape_360(args["plate"], args["intel"], args["veh"], args["drv"],
                         args["trn"], args["gates"], args["jobs"], args["licence"],
                         args["enrollment_row"], args["verification"])


# --------------------------------------------------------------- happy path
def test_full_profile_populates_every_card():
    r = shape()
    assert r["found"] is True
    assert r["vehicle"] == {
        "number": PLATE, "id": "TRK-000123", "status": "ACTIVE", "class": "HGV",
        "fuel": "Diesel", "type": "Container Truck", "chassis_number": "CH123",
        "rfid_fastag_id": "FT9", "registered_at": "2026-01-01T00:00:00+00:00",
        "assignment_status": "AT_GATE", "in_master": True,
    }
    assert r["driver"]["name"] == "A. Kumar"
    assert r["driver"]["license"]["type"] == "HMV"
    assert r["driver"]["license"]["pdp_status"] == "VALID"
    assert r["driver"]["license"]["verification_status"] == "VERIFIED"
    assert r["transporter"]["name"] == "Konkan Logistics"
    assert r["transporter"]["source"] == "vehicle_mapping"
    assert r["alerts"][0]["severity"] == "HIGH"


def test_compliance_grades_each_document_by_its_own_expiry():
    c = shape()["compliance"]
    assert c["rc"]["status"] == "ON_RECORD"
    assert c["insurance"]["status"] == "VALID"
    assert c["puc"]["status"] == "EXPIRING"
    assert c["fitness"]["status"] == "EXPIRED"
    assert c["blacklist"] == {"status": "CLEAR", "source": "rc", "reason": None}


# ------------------------------------------------------------- degraded data
def test_vehicle_without_driver_leaves_driver_null_not_fabricated():
    r = shape(drv=None, licence=None, enrollment_row=None, verification=None, jobs=[])
    assert r["driver"] is None
    assert r["vehicle"]["assignment_status"] == "UNASSIGNED"
    assert r["found"] is True  # the vehicle itself is still on record


def test_driver_assigned_but_no_open_job():
    r = shape(jobs=[])
    assert r["vehicle"]["assignment_status"] == "DRIVER_ASSIGNED"


def test_completed_job_is_not_an_open_assignment():
    done = [{**JOBS[0], "status": "COMPLETED"}]
    assert shape(jobs=done)["vehicle"]["assignment_status"] == "DRIVER_ASSIGNED"


def test_missing_compliance_reads_not_available_never_valid():
    r = shape(intel={**INTEL, "rc": {}})
    c = r["compliance"]
    assert c["rc"]["status"] == "NOT_AVAILABLE"
    assert c["insurance"]["status"] == "NOT_AVAILABLE"
    assert c["puc"]["status"] == "NOT_AVAILABLE"
    assert c["fitness"]["status"] == "NOT_AVAILABLE"
    assert c["blacklist"]["status"] == "NOT_AVAILABLE"


def test_unknown_vehicle_is_found_false_with_a_stable_shape():
    r = shape(intel={"vehicle_number": None, "rc": None, "alerts": []},
              veh=None, drv=None, trn=None, licence=None,
              enrollment_row=None, verification=None, jobs=[], gates=[])
    assert r["found"] is False
    assert r["vehicle"] is None and r["driver"] is None and r["transporter"] is None
    assert r["alerts"] == [] and r["compliance"]["insurance"]["status"] == "NOT_AVAILABLE"


# ---------------------------------------------------------------- blacklist
def test_transporter_blacklist_overrides_a_clean_rc():
    banned = {**TRN, "blacklisted_at": "2026-06-01T00:00:00+00:00",
              "blacklist_reason": "Repeated overloading", "blacklist_severity": "HIGH"}
    r = shape(trn=banned)
    assert r["compliance"]["blacklist"]["status"] == "BLACKLISTED"
    assert r["compliance"]["blacklist"]["source"] == "transporter"
    assert r["transporter"]["status"] == "BLACKLISTED"
    assert r["transporter"]["blacklisted"] is True


def test_rc_blacklist_flag_alone_is_reported():
    r = shape(trn=None, intel={**INTEL, "rc": {**RC, "blacklist_status": "BLACKLISTED"}})
    assert r["compliance"]["blacklist"] == {"status": "BLACKLISTED", "source": "rc",
                                           "reason": None}


def test_transporter_falls_back_to_the_drivers_employer():
    r = shape(trn=None)
    assert r["transporter"]["name"] == "Konkan Logistics"
    assert r["transporter"]["source"] == "driver_employer"


def test_cancelled_pdp_is_not_reported_as_valid():
    r = shape(licence={**LIC, "pdp_active": False})
    assert r["driver"]["license"]["pdp_status"] == "CANCELLED"


# ----------------------------------------------------------------- timeline
def test_timeline_is_chronological_and_ends_on_current_status():
    tl = shape()["timeline"]
    stages = [e["stage"] for e in tl]
    assert stages[0] == "VEHICLE_REGISTERED"
    assert "DRIVER_ENROLLED" in stages
    assert "TRANSPORTER_MAPPED" in stages
    assert "GATE_EVENT" in stages
    assert "CARGO_MOVEMENT" in stages
    assert stages[-1] == "CURRENT_STATUS"
    dated = [e["ts"] for e in tl if e["ts"]]
    assert dated == sorted(dated)


def test_timeline_omits_stages_with_no_timestamp():
    tl = shape(veh=None, trn=None, gates=[], jobs=[])["timeline"]
    stages = [e["stage"] for e in tl]
    assert "VEHICLE_REGISTERED" not in stages
    assert "GATE_EVENT" not in stages
    assert stages == ["DRIVER_ENROLLED", "CURRENT_STATUS"]


# ------------------------------------------------------------------ no-DSN
def test_no_dsn_returns_the_empty_envelope_rather_than_raising():
    r = asyncio.run(vi.vehicle_360(PLATE, dsn=None))
    assert r["found"] is False
    assert r["plate"] == PLATE
    assert r["timeline"] == [] and r["alerts"] == []
    assert set(r) >= {"vehicle", "driver", "transporter", "compliance", "intel"}


def test_plate_normalisation_ignores_punctuation_and_case():
    assert vi._plate_norm("mh-04 qa 9911") == PLATE
