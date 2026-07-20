"""Tests for the Transporters & Drivers Data Upload sub-module (/api/td-upload).

Mirrors tests/test_cfs_ecy_upload.py. Three layers, all runnable without a live
Postgres:

* Pure parser checks — templates, alias-driven column mapping, per-row validation
  (bad Company ID / missing name / short licence), soft warnings (GST/mobile/date
  format), in-file duplicate detection, missing-column friendly rejection, CSV/XLSX
  byte readers.
* UploadService orchestration against an in-memory fake repository — validate
  (dry-run), import (SUCCESS / PARTIAL / SKIPPED_DUPLICATE / REJECTED), upsert
  create-vs-update counts, history.
* Router wiring via Starlette's TestClient with the upload service swapped through
  app.dependency_overrides — template download, 400 on bad entity, validate endpoint.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path fails FAST (the fake bypasses it).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.transporters_drivers import upload_parsers as P  # noqa: E402
from services.transporters_drivers.upload_service import (  # noqa: E402
    TransportersDriversUploadService,
)

TXP_HDR = "Company ID,Company Name,Transporter Code,GSTIN,Mobile,Status"
DRV_HDR = "Licence Number,Driver Name,Company Name,Licence Valid To,Status"


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ------------------------------------------------------------------ pure parser
def test_transporter_template_shape():
    t = P.template_csv("TRANSPORTER")
    header = t.splitlines()[0].split(",")
    assert header[0] == "Company ID" and "Company Name" in header
    assert "REQUIRED" in t and "100245" in t          # guidance + example


def test_driver_template_shape():
    t = P.template_csv("DRIVER")
    header = t.splitlines()[0].split(",")
    assert header[0] == "Licence Number" and "Driver Name" in header
    assert "REQUIRED" in t and "Suresh Patil" in t


def test_alias_column_mapping_transporter():
    # "Transporter_Name" and "CompanyID" must map to canonical fields.
    body = _csv("CompanyID,Transporter_Name", "555,Acme Logistics")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="TRANSPORTER")
    assert not res.rejected and len(res.records) == 1
    r = res.records[0]
    assert r["source_company_id"] == 555 and r["name"] == "Acme Logistics"


def test_alias_column_mapping_driver():
    # "DL Number" / "Name" must map to licence / name.
    body = _csv("DL Number,Name", "MH0120220001234,Ravi Kumar")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="DRIVER")
    assert not res.rejected and len(res.records) == 1
    r = res.records[0]
    assert r["licence_no_norm"] == "MH0120220001234" and r["name"] == "Ravi Kumar"


def test_missing_required_column_is_friendly_rejection():
    body = _csv("Foo,Bar", "1,2")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="DRIVER")
    assert res.rejected
    assert {e["error_code"] for e in res.errors} == {"missing_column"}
    assert any("Licence Number column not found" in e["error_detail"] for e in res.errors)


def test_transporter_invalid_company_id_and_missing_name():
    body = _csv(TXP_HDR,
                "NOTNUM,Acme,TPT1,,9876543210,ACTIVE",         # non-integer Company ID
                "600,,TPT2,,9876543210,ACTIVE")                # missing Company Name
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="TRANSPORTER")
    assert len(res.records) == 0 and res.invalid_count == 2
    assert {e["error_code"] for e in res.errors} == {"invalid_company_id", "empty_required"}


def test_transporter_soft_warnings_gst_and_mobile():
    body = _csv(TXP_HDR, "700,Acme,TPT7,BADGST,12,ACTIVE")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="TRANSPORTER")
    assert len(res.records) == 1                              # still imported
    codes = {w["error_code"] for w in res.warnings}
    assert "gstin_format_invalid" in codes and "mobile_invalid" in codes
    assert res.records[0]["mobile"] is None                   # bad mobile dropped


def test_transporter_valid_gst_and_mobile_normalised():
    body = _csv(TXP_HDR, "800,Acme,TPT8,27AABCB1234C1ZV,+91 98765 43210,ACTIVE")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="TRANSPORTER")
    r = res.records[0]
    assert r["gstin"] == "27AABCB1234C1ZV" and r["mobile"] == "9876543210"
    assert not res.warnings


def test_transporter_in_file_duplicate():
    body = _csv(TXP_HDR,
                "900,Acme,TPT9,,9876543210,ACTIVE",
                "900,Acme Dup,TPT9B,,9876543210,ACTIVE")       # same Company ID
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="TRANSPORTER")
    assert len(res.records) == 1 and res.duplicate_count == 1
    assert any(w["error_code"] == "duplicate_in_file" for w in res.warnings)


def test_driver_short_licence_and_dup_and_date_warning():
    body = _csv(DRV_HDR,
                "AB,Ravi,Acme,31/12/2027,ACTIVE",              # too short
                "MH0120220001234,Ravi,Acme,not-a-date,ACTIVE",
                "MH0120220001234,Ravi Dup,Acme,31/12/2027,ACTIVE")   # dup licence
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="DRIVER")
    assert len(res.records) == 1 and res.invalid_count == 1 and res.duplicate_count == 1
    assert any(e["error_code"] == "licence_too_short" for e in res.errors)
    assert any(w["error_code"] == "invalid_date" for w in res.warnings)


def test_driver_no_company_warns_missing_mapping():
    body = _csv("Licence Number,Driver Name", "MH0120220001234,Ravi")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, entity="DRIVER")
    assert len(res.records) == 1
    assert any(w["error_code"] == "no_transporter_mapping" for w in res.warnings)


def test_unsupported_and_empty_files_raise():
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"whatever", "x.pdf")
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"", "x.csv")


# ------------------------------------------------------- UploadService + fake repo
class FakeUploadRepo:
    """In-memory stand-in for TransportersDriversRepository's upload surface. Masters
    are keyed dicts so upsert (create-vs-update) semantics are reproduced."""

    def __init__(self) -> None:
        self.files: dict[int, dict] = {}
        self.errors: dict[int, list] = {}
        self.masters: dict[tuple[str, object], dict] = {}     # (entity, key) -> record
        self._seq = 0
        self._by_sha: dict[str, int] = {}

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _key(entity_type, rec):
        return (entity_type,
                rec["source_company_id"] if entity_type == "TRANSPORTER"
                else rec["licence_no_norm"])

    async def find_file_by_sha(self, sha256):
        fid = self._by_sha.get(sha256)
        return self.files.get(fid) if fid else None

    async def persist(self, records, *, entity_type, source_file, source_sha256,
                      physical_format, file_size=None, uploaded_by=None, source="UPLOAD"):
        if source_sha256 in self._by_sha:
            f = self.files[self._by_sha[source_sha256]]
            return {"file_id": f["id"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": f["record_count"], "imported_count": f["imported_count"],
                    "error_count": f["error_count"], "duplicate_count": f["duplicate_count"],
                    "duplicate": True, "created": 0, "updated": 0, "row_errors": []}
        fid = self._next()
        created = updated = 0
        for r in records:
            k = self._key(entity_type, r)
            if k in self.masters:
                updated += 1
            else:
                created += 1
            self.masters[k] = dict(r)
        imported = created + updated
        self.files[fid] = {"id": fid, "entity_type": entity_type,
                           "physical_format": physical_format, "source_file": source_file,
                           "record_count": len(records), "imported_count": imported,
                           "error_count": 0, "duplicate_count": 0, "import_status": "SUCCESS",
                           "uploaded_by": uploaded_by, "source": source,
                           "created_at": "2026-07-20T00:00:00"}
        self._by_sha[source_sha256] = fid
        return {"file_id": fid, "import_status": "SUCCESS", "record_count": len(records),
                "imported_count": imported, "error_count": 0, "duplicate_count": 0,
                "duplicate": False, "created": created, "updated": updated, "row_errors": []}

    async def record_rejected_upload(self, *, entity_type, physical_format, source_file,
                                     source_sha256, file_size, uploaded_by, detail, errors):
        if source_sha256 in self._by_sha:
            return self._by_sha[source_sha256]
        fid = self._next()
        self.files[fid] = {"id": fid, "entity_type": entity_type,
                           "physical_format": physical_format, "source_file": source_file,
                           "record_count": 0, "imported_count": 0, "error_count": len(errors),
                           "duplicate_count": 0, "import_status": "FAILED",
                           "uploaded_by": uploaded_by, "source": "UPLOAD",
                           "created_at": "2026-07-20T00:00:00", "error_detail": detail}
        self._by_sha[source_sha256] = fid
        self.errors[fid] = list(errors)
        return fid

    async def add_row_errors(self, file_id, errors):
        self.errors.setdefault(file_id, []).extend(errors)

    async def mark_partial(self, file_id, *, error_count):
        self.files[file_id]["import_status"] = "PARTIAL"
        self.files[file_id]["error_count"] = error_count

    async def list_files(self, *, filters, limit, offset):
        rows = sorted(self.files.values(), key=lambda f: f["id"], reverse=True)
        return rows[offset:offset + limit]

    async def count_files(self, *, filters):
        return len(self.files)

    async def get_file(self, file_id):
        return self.files.get(file_id)

    async def list_file_errors(self, file_id, *, limit, offset):
        return self.errors.get(file_id, [])[offset:offset + limit]


def _svc():
    repo = FakeUploadRepo()
    return TransportersDriversUploadService(repository=repo), repo


def test_service_validate_is_dry_run():
    svc, repo = _svc()
    body = _csv(TXP_HDR, "100,Acme,TPT1,,9876543210,ACTIVE")
    out = asyncio.run(svc.validate("TRANSPORTER", body, "x.csv", "tester"))
    assert out["status"] == "VALIDATED" and out["valid"] is True
    assert out["summary"]["valid"] == 1
    assert not repo.files and not repo.masters             # NOTHING written on validate


def test_service_import_success_then_duplicate_file():
    svc, repo = _svc()
    body = _csv(TXP_HDR, "100,Acme,TPT1,,9876543210,ACTIVE",
                "101,Bharat,TPT2,,9876500000,ACTIVE")
    r1 = asyncio.run(svc.import_file("TRANSPORTER", body, "x.csv", "tester"))
    assert r1["status"] == "SUCCESS" and r1["imported"] == 2 and r1["created"] == 2
    assert r1["duplicate_file"] is False
    before = len(repo.masters)
    r2 = asyncio.run(svc.import_file("TRANSPORTER", body, "x.csv", "tester"))
    assert r2["status"] == "SKIPPED_DUPLICATE" and r2["duplicate_file"] is True
    assert len(repo.masters) == before                     # no new master rows


def test_service_import_upsert_updates_existing():
    svc, repo = _svc()
    a = _csv(TXP_HDR, "100,Acme,TPT1,,9876543210,ACTIVE")
    b = _csv(TXP_HDR, "100,Acme Renamed,TPT1,,9876543210,ACTIVE")   # same key, new bytes
    asyncio.run(svc.import_file("TRANSPORTER", a, "a.csv", "tester"))
    r = asyncio.run(svc.import_file("TRANSPORTER", b, "b.csv", "tester"))
    assert r["status"] == "SUCCESS" and r["created"] == 0 and r["updated"] == 1
    assert repo.masters[("TRANSPORTER", 100)]["name"] == "Acme Renamed"


def test_service_import_partial_when_some_rows_invalid():
    svc, repo = _svc()
    body = _csv(TXP_HDR, "100,Acme,TPT1,,9876543210,ACTIVE",
                "NOTNUM,Bad,TPT2,,9876500000,ACTIVE")
    r = asyncio.run(svc.import_file("TRANSPORTER", body, "x.csv", "tester"))
    assert r["status"] == "PARTIAL" and r["imported"] == 1 and r["invalid"] == 1
    assert repo.files[r["file_id"]]["import_status"] == "PARTIAL"


def test_service_import_rejected_on_missing_columns():
    svc, repo = _svc()
    r = asyncio.run(svc.import_file("DRIVER", _csv("Foo,Bar", "1,2"), "x.csv", "tester"))
    assert r["status"] == "REJECTED" and r["imported"] == 0
    assert repo.files[r["file_id"]]["import_status"] == "FAILED"


def test_service_driver_import_success():
    svc, repo = _svc()
    body = _csv(DRV_HDR, "MH0120220001234,Ravi,Acme,31/12/2027,ACTIVE")
    r = asyncio.run(svc.import_file("DRIVER", body, "d.csv", "tester"))
    assert r["status"] == "SUCCESS" and r["created"] == 1
    assert ("DRIVER", "MH0120220001234") in repo.masters


def test_service_history_lists_uploads():
    svc, repo = _svc()
    asyncio.run(svc.import_file("TRANSPORTER",
                               _csv(TXP_HDR, "100,Acme,TPT1,,9876543210,ACTIVE"),
                               "a.csv", "tester"))
    hist = asyncio.run(svc.list_uploads({}, limit=25, offset=0))
    assert hist["total"] == 1 and hist["items"][0]["source_file"] == "a.csv"


# --------------------------------------------------------------------- router
@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from gateway.main import app
    from gateway.routers import transporters_drivers_upload as router

    svc, _repo = _svc()
    app.dependency_overrides[router.get_upload_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(router.get_upload_service, None)


def test_router_template_download(client):
    r = client.get("/api/td-upload/templates/TRANSPORTER")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.text.splitlines()[0].startswith("Company ID,Company Name")


def test_router_template_bad_entity_400(client):
    assert client.get("/api/td-upload/templates/VESSEL").status_code == 400


def test_router_validate_endpoint(client):
    body = _csv(DRV_HDR, "MH0120220001234,Ravi,Acme,31/12/2027,ACTIVE")
    r = client.post("/api/td-upload/validate", data={"entity": "DRIVER"},
                    files={"file": ("x.csv", body, "text/csv")})
    assert r.status_code == 200
    assert r.json()["status"] == "VALIDATED"
