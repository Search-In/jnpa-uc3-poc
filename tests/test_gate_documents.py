"""Gate Document module tests (UC-III: EIR / PIN ticket / Form-13).

Pure-function parser tests (no DB) + router contract tests against a fake
service, mirroring tests/test_cfs_ecy.py. The client-document realities that
must hold are asserted explicitly:

  * EIR TruckIn/TruckOut yields the corpus TAT ground truth (82 / 165 min).
  * A document with NO container number is still ingested (truck-keyed) —
    the MH46AF4375 case that every other module silently drops.
  * A dual-move PIN ticket is two legs sharing one PIN number.
  * Form-13 carries VisitID + in/out gate codes (IGTK01 / OGTK05).
  * Re-parsing the same file is row-hash idempotent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers import gate_documents as R
from services.gate_documents import upload_parsers as P


# --------------------------------------------------------------------- parsers
def _rows(csv_text: str):
    return P.read_rows_from_bytes(csv_text.encode(), "test.csv")


EIR_CSV = (
    "EIR No,Terminal,Container No,Vessel,VIA,Seal No,BAT,Truck No,Truck In,Truck Out,Company,From CFS,Scanner Stamp\n"
    "E1,Gateway (GTI),MRKU5014206,ALEXANDRA MAERSK,S0335,OM0130728,D391,MH43BX1488,"
    "06/06/2026 08:26,06/06/2026 11:11,TRANSTA,,SCANNED CLEAN\n"
    "E2,Gateway (GTI),NYKU4768188,ONE RECOGNITION,S0475,,D391,MH43BX1488,"
    "10/06/2026 14:55,10/06/2026 16:17,TRANSTA,,\n"
)


def test_eir_tat_matches_corpus_ground_truth():
    header, rows = _rows(EIR_CSV)
    res = P.parse(header, rows, doc_type="EIR", source_file="eir.csv")
    assert res.invalid_count == 0 and len(res.records) == 2
    tats = [round((r["truck_out_time"] - r["truck_in_time"]).total_seconds() / 60)
            for r in res.records]
    assert tats == [165, 82]          # the two GTI EIRs from the client document
    assert res.records[0]["bat_lane"] == "D391"
    assert res.records[0]["seal_number"] == "OM0130728"
    assert res.records[0]["via_no"] == "S0335"
    assert "SCANNED CLEAN" in (res.records[0]["scanner_stamp"] or "")


def test_eir_without_container_number_is_kept_truck_keyed():
    """The MH46AF4375 case: no container number on the document or scan."""
    csv_text = ("EIR No,Terminal,Container No,Truck No,Truck In\n"
                "E9,DP World NSICT,,MH46AF4375,12/06/2026 09:00\n")
    header, rows = _rows(csv_text)
    res = P.parse(header, rows, doc_type="EIR", source_file="eir.csv")
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec["container_number"] is None
    assert rec["truck_no"] == "MH46AF4375"
    assert any(w["error_code"] == "no_container_number" for w in res.warnings)
    assert res.invalid_count == 0     # kept, not rejected


def test_eir_requires_truck_and_rejects_out_before_in():
    header, rows = _rows("EIR No,Truck No,Truck In,Truck Out\nE1,,01/07/2026 10:00,01/07/2026 12:00\n")
    res = P.parse(header, rows, doc_type="EIR")
    assert res.invalid_count == 1 and not res.records

    header, rows = _rows("EIR No,Truck No,Truck In,Truck Out\n"
                         "E1,MH43BX1488,01/07/2026 12:00,01/07/2026 10:00\n")
    res = P.parse(header, rows, doc_type="EIR")
    assert res.invalid_count == 1
    assert res.errors[0]["error_code"] == "out_before_in"


def test_eir_missing_required_column_rejects_file():
    header, rows = _rows("EIR No,Container No\nE1,MRKU5014206\n")
    res = P.parse(header, rows, doc_type="EIR")
    assert res.rejected is True
    assert res.errors[0]["error_code"] == "missing_column"


PIN_CSV = (
    "PIN Number,Terminal,Truck No,Company,Container No,Group Code,Yard Location,Gate,Move Type,Leg\n"
    "230283,NSFT,MH43CQ2814,TRANSTAR,OOLU9340457,,2P08D.1,10,IMPORT_PICK,1\n"
    # the BMCT dual-move ticket: export drop + import pick on ONE trip
    "554401,PSA BMCT,MH43CQ0554,TRANSTAR,SEGU5833837,,A0948,2,EXPORT_DROP,1\n"
    "554401,PSA BMCT,MH43CQ0554,TRANSTAR,,CLP/40/F,F0216,2,IMPORT_PICK,2\n"
)


def test_pin_dual_move_is_two_legs_sharing_one_pin():
    header, rows = _rows(PIN_CSV)
    res = P.parse(header, rows, doc_type="PIN", source_file="pin.csv")
    assert res.invalid_count == 0 and len(res.records) == 3
    dual = [r for r in res.records if r["pin_number"] == "554401"]
    assert len(dual) == 2
    assert {r["move_type"] for r in dual} == {"EXPORT_DROP", "IMPORT_PICK"}
    assert sorted(r["leg_seq"] for r in dual) == [1, 2]
    # the empty-by-group leg carries a group code and no container
    by_group = [r for r in dual if r["container_number"] is None][0]
    assert by_group["group_code"] == "CLP/40/F"


def test_pin_keeps_real_yard_location_format():
    """'2P08D.1' must survive verbatim — the cargo yard_block regex cannot hold it."""
    header, rows = _rows(PIN_CSV)
    res = P.parse(header, rows, doc_type="PIN")
    assert res.records[0]["yard_location"] == "2P08D.1"
    assert res.records[0]["gate"] == "10"


FORM13_CSV = (
    "Form13 No,Visit ID,Terminal,Container No,Vehicle No,Transporter,In Gate,Out Gate,Direction\n"
    "F13001,4418958,NSIGT,FFAU4770682,MH43BX1488,Transtar Handling & Warehousing Co,IGTK01,OGTK05,IMPORT\n"
)


def test_form13_carries_visit_id_and_gate_codes():
    header, rows = _rows(FORM13_CSV)
    res = P.parse(header, rows, doc_type="FORM13", source_file="f13.csv")
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec["visit_id"] == "4418958"
    assert (rec["in_gate"], rec["out_gate"]) == ("IGTK01", "OGTK05")
    assert rec["direction"] == "IMPORT"
    assert rec["transporter_name"].startswith("Transtar")


def test_row_hash_is_stable_and_dedupes_within_file():
    header, rows = _rows(EIR_CSV + EIR_CSV.splitlines()[1] + "\n")
    res = P.parse(header, rows, doc_type="EIR")
    # the repeated first row collapses
    assert res.duplicate_count == 1
    assert len({r["row_sha256"] for r in res.records}) == len(res.records)

    again = P.parse(*_rows(EIR_CSV), doc_type="EIR")
    assert ([r["row_sha256"] for r in again.records]
            == [r["row_sha256"] for r in P.parse(*_rows(EIR_CSV), doc_type="EIR").records])


def test_iso6346_invalid_container_is_flagged_not_rejected():
    header, rows = _rows("EIR No,Truck No,Container No\nE1,MH43BX1488,ABCU1234567\n")
    res = P.parse(header, rows, doc_type="EIR")
    assert len(res.records) == 1
    assert res.records[0]["iso_valid"] is False
    assert any(w["error_code"] == "container_iso6346_invalid" for w in res.warnings)


def test_alias_driven_headers_and_templates():
    header, rows = _rows("EIRNO,TRUCK NUMBER,CNTR_NO\nE1,MH 43 BX 1488,MRKU5014206\n")
    res = P.parse(header, rows, doc_type="EIR")
    assert res.records[0]["truck_no"] == "MH43BX1488"       # plate normalised
    assert res.records[0]["container_number"] == "MRKU5014206"
    for dt in P.DOC_TYPES:
        assert "REQUIRED" in P.template_csv(dt)


# ---------------------------------------------------------------------- router
class _FakeService:
    def __init__(self):
        self.imported = []

    async def summary(self):
        return {"eir": 2, "pin_tickets": 2, "pin_legs": 3, "dual_move_tickets": 1,
                "form13": 1, "containerless_docs": 1, "eir_with_tat": 2, "files": 1}

    async def list_docs(self, doc_type, *, filters, limit, offset):
        return {"items": [{"id": 1, "doc_type": doc_type, "filters": filters}],
                "total": 1, "limit": limit, "offset": offset, "count": 1}

    async def docs_for_container(self, cn):
        return {"container_no": cn, "eir": [{"id": 1}], "pin": [], "form13": [], "total": 1}

    async def docs_for_truck(self, truck):
        return {"truck_no": truck, "eir": [{"id": 1, "tat_minutes": 165}], "pin": [],
                "form13": [], "total": 1, "terminals": ["Gateway (GTI)"],
                "tat_samples": [{"tat_minutes": 165}]}

    async def tat_summary(self, *, terminal=None):
        return {"samples": 2, "avg_tat_min": 124, "median_tat_min": 124,
                "min_tat_min": 82, "max_tat_min": 165, "source": "document",
                "by_terminal": [], "terminal_filter": terminal}

    def template(self, dt):
        return P.template_csv(dt)

    async def validate(self, dt, content, filename, uploader):
        return {"doc_type": dt, "status": "VALIDATED", "valid": True,
                "summary": {"rows": 1, "valid": 1}, "preview": [], "errors": [], "warnings": []}

    async def import_file(self, dt, content, filename, uploader):
        self.imported.append((dt, filename, uploader))
        return {"file_id": 7, "status": "SUCCESS", "imported": 1, "skipped": 0,
                "invalid": 0, "duplicate_file": False, "summary": {}, "warnings": []}

    async def list_uploads(self, filters, *, limit, offset):
        return {"items": [{"id": 7, "doc_type": filters.get("doc_type")}], "total": 1,
                "limit": limit, "offset": offset, "count": 1}

    async def get_upload(self, file_id):
        return {"id": file_id, "errors": []} if file_id == 7 else None


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(R.router)
    fake = _FakeService()
    app.dependency_overrides[R.get_service] = lambda: fake
    c = TestClient(app)
    c.fake = fake  # type: ignore[attr-defined]
    return c


def test_router_summary_and_lists(client):
    r = client.get("/api/gate-docs/summary")
    assert r.status_code == 200 and r.json()["dual_move_tickets"] == 1

    for path in ("/api/gate-docs/eir", "/api/gate-docs/pin", "/api/gate-docs/form13"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.json()["count"] == 1
        assert r.headers["X-Total-Count"] == "1"


def test_router_normalises_plate_filter(client):
    r = client.get("/api/gate-docs/eir", params={"truck": "mh 43 bx 1488"})
    assert r.status_code == 200
    assert r.json()["items"][0]["filters"]["truck_no"] == "MH43BX1488"


def test_router_truck_and_container_views(client):
    r = client.get("/api/gate-docs/truck/mh-43-bx-1488")
    assert r.status_code == 200
    body = r.json()
    assert body["truck_no"] == "MH43BX1488"
    assert body["tat_samples"][0]["tat_minutes"] == 165

    r = client.get("/api/gate-docs/container/mrku5014206")
    assert r.status_code == 200 and r.json()["container_no"] == "MRKU5014206"


def test_router_tat_endpoint(client):
    r = client.get("/api/gate-docs/tat")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "document"
    assert (body["min_tat_min"], body["max_tat_min"]) == (82, 165)


def test_router_upload_triad_and_doc_type_validation(client):
    r = client.get("/api/gate-docs/templates/EIR")
    assert r.status_code == 200 and "Truck No" in r.text

    r = client.get("/api/gate-docs/templates/NOPE")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_doc_type"

    files = {"file": ("eir.csv", EIR_CSV.encode(), "text/csv")}
    r = client.post("/api/gate-docs/validate", files=files, data={"doc_type": "eir"})
    assert r.status_code == 200 and r.json()["status"] == "VALIDATED"

    r = client.post("/api/gate-docs/upload", files=files, data={"doc_type": "EIR"})
    assert r.status_code == 200 and r.json()["imported"] == 1
    assert client.fake.imported[0][0] == "EIR"  # type: ignore[attr-defined]

    r = client.post("/api/gate-docs/upload", files={"file": ("x.csv", b"", "text/csv")},
                    data={"doc_type": "EIR"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "empty_file"


def test_router_upload_history(client):
    r = client.get("/api/gate-docs/uploads", params={"doc_type": "PIN"})
    assert r.status_code == 200 and r.json()["items"][0]["doc_type"] == "PIN"

    r = client.get("/api/gate-docs/uploads/7")
    assert r.status_code == 200
    r = client.get("/api/gate-docs/uploads/99")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "upload_not_found"


# ------------------------------------------------------------------ schema lock
def test_boot_ddl_matches_migration_table_set():
    """gateway/gate_docs_ext._DDL must stay in lock-step with migration 0112."""
    from gateway.gate_docs_ext import _DDL

    sql = Path("infra/postgres/v3/0112_gate_documents.sql").read_text()
    pat = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(core\.\w+)", re.I)
    assert set(pat.findall(sql)) == set(pat.findall("\n".join(_DDL)))
