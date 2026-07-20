"""Tests for the CFS-ECY Data Upload sub-module (/api/cfs-ecy upload) — module 13.

Mirrors tests/test_shipping_lines_upload.py. Four layers, all runnable without a
live Postgres:

* Pure parser checks — template, alias-driven column mapping, per-row validation
  (bad timestamp / mode / container), in-file duplicate detection, missing-column
  rejection, CSV + XLSX byte readers.
* UploadService orchestration against an in-memory fake repository — validate
  (dry-run), import (SUCCESS / PARTIAL / SKIPPED_DUPLICATE / REJECTED), history.
* Router wiring via Starlette's TestClient with the upload service swapped through
  app.dependency_overrides — template download, 400 on bad facility.
* Schema lock-step — migration 0027 + 0034 objects == gateway.cfs_ecy_ext._DDL.
* A REAL-file parse assertion (skipped when the JNPA data folder is absent):
  968 CFS + 961 ECY rows → 1928 valid, 1 in-file duplicate, 0 invalid.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path fails FAST (the fake bypasses it, and
# the gateway lifespan's best-effort schema-ensure returns immediately instead of
# hanging on a real host). Mirrors tests/test_cfs_ecy.py.
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from jnpa_shared.iso6346 import with_check_digit  # noqa: E402
from services.cfs_ecy import upload_parsers as P  # noqa: E402
from services.cfs_ecy.upload_service import CfsEcyUploadService  # noqa: E402

_DATA_DIR = Path("/Users/pandurangdhage/Downloads/Digital Twin/Data/13-CFS-ECY")
CN_A = with_check_digit("AAAU100000")
CN_B = with_check_digit("BBBU200000")


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ------------------------------------------------------------------ pure parser
def test_template_has_required_and_optional_columns():
    t = P.template_csv()
    header = t.splitlines()[0].split(",")
    assert header == ["Container Number", "Timestamp", "Mode", "Facility"]
    assert "REQUIRED" in t and "OPTIONAL" in t         # guidance line
    assert "ONEU2122848" in t                          # example row


def test_alias_column_mapping():
    # "Container No" / "Event Time" / "Movement" must all map to the canonical fields.
    body = _csv("Container No,Event Time,Movement",
                f"{CN_A},01/07/2026 14:00,In")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="CFS", source_file="x.csv")
    assert not res.rejected and len(res.records) == 1
    r = res.records[0]
    assert r["container_number"] == CN_A and r["mode"] == "IN" and r["facility_type"] == "CFS"
    assert r["event_ts"].utcoffset().total_seconds() == 5.5 * 3600   # IST


def test_missing_required_column_is_friendly_rejection():
    body = _csv("Box,When", "X,Y")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="CFS")
    assert res.rejected
    codes = {e["error_code"] for e in res.errors}
    assert codes == {"missing_column"}
    assert any("Container Number column not found" in e["error_detail"] for e in res.errors)


def test_per_row_validation_and_in_file_dedup():
    body = _csv("Container Number,Timestamp,Mode",
                f"{CN_A},01/07/2026 14:00,In",
                f"{CN_A},01/07/2026 14:00,In",          # exact in-file duplicate
                f"{CN_B},not-a-date,Out",               # bad timestamp
                f"{CN_B},02/07/2026 09:00,Sideways")    # bad mode
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="CFS")
    assert len(res.records) == 1                        # only the first CN_A row
    assert res.invalid_count == 2                       # bad timestamp + bad mode
    assert res.duplicate_count == 1
    assert {e["error_code"] for e in res.errors} == {"invalid_timestamp", "invalid_mode"}


def test_iso6346_invalid_is_warning_not_error():
    # right shape, wrong check digit → imported but flagged as a warning.
    body = _csv("Container Number,Timestamp,Mode", "MAEU6123450,01/07/2026 14:00,In")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="ECY")
    assert len(res.records) == 1 and res.records[0]["iso_valid"] is False
    assert any(w["error_code"] == "container_iso6346_invalid" for w in res.warnings)


def test_facility_column_overrides_selector():
    body = _csv("Container Number,Timestamp,Mode,Facility",
                f"{CN_A},01/07/2026 14:00,In,ECY")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="CFS")          # selector says CFS…
    assert res.records[0]["facility_type"] == "ECY"      # …but the column wins


def test_invalid_facility_column_value_is_error():
    body = _csv("Container Number,Timestamp,Mode,Facility",
                f"{CN_A},01/07/2026 14:00,In,ICD")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, facility="CFS")
    assert len(res.records) == 0 and res.invalid_count == 1
    assert res.errors[0]["error_code"] == "invalid_facility"


def test_unsupported_and_empty_files_raise():
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"whatever", "x.pdf")
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"", "x.csv")


# ------------------------------------------------------- UploadService + fake repo
class FakeUploadRepo:
    """In-memory stand-in for CfsEcyRepository's upload surface. Movements are keyed
    by the DB unique key so ON CONFLICT DO NOTHING semantics are reproduced."""

    def __init__(self) -> None:
        self.files: dict[int, dict] = {}
        self.errors: dict[int, list] = {}
        self.movements: set = set()                      # (facility, cn, ts, mode)
        self._seq = 0
        self._by_sha: dict[str, int] = {}

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def find_file_by_sha(self, sha256):
        fid = self._by_sha.get(sha256)
        return self.files.get(fid) if fid else None

    async def persist(self, records, *, facility_type, source_file, source_sha256,
                      physical_format, file_size=None, uploaded_by=None, source="UPLOAD"):
        if source_sha256 in self._by_sha:
            f = self.files[self._by_sha[source_sha256]]
            return {"file_id": f["id"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": f["record_count"], "imported_count": f["imported_count"],
                    "error_count": f["error_count"], "duplicate_count": f["duplicate_count"],
                    "duplicate": True}
        fid = self._next()
        imported = 0
        for r in records:
            key = (r["facility_type"], r["container_number"], r["event_ts"], r["mode"])
            if key not in self.movements:
                self.movements.add(key)
                imported += 1
        dup = len(records) - imported
        self.files[fid] = {"id": fid, "facility_type": facility_type,
                           "physical_format": physical_format, "source_file": source_file,
                           "record_count": len(records), "imported_count": imported,
                           "error_count": 0, "duplicate_count": dup,
                           "import_status": "SUCCESS", "uploaded_by": uploaded_by,
                           "source": source, "created_at": "2026-07-20T00:00:00"}
        self._by_sha[source_sha256] = fid
        return {"file_id": fid, "import_status": "SUCCESS", "record_count": len(records),
                "imported_count": imported, "error_count": 0, "duplicate_count": dup,
                "duplicate": False}

    async def record_rejected_upload(self, *, facility_type, physical_format, source_file,
                                     source_sha256, file_size, uploaded_by, detail, errors):
        if source_sha256 in self._by_sha:
            return self._by_sha[source_sha256]
        fid = self._next()
        self.files[fid] = {"id": fid, "facility_type": facility_type,
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


def _svc() -> tuple[CfsEcyUploadService, FakeUploadRepo]:
    repo = FakeUploadRepo()
    return CfsEcyUploadService(repository=repo), repo


def test_service_validate_is_dry_run():
    svc, repo = _svc()
    body = _csv("Container Number,Timestamp,Mode", f"{CN_A},01/07/2026 14:00,In")
    out = asyncio.run(svc.validate("CFS", body, "x.csv", "tester"))
    assert out["status"] == "VALIDATED" and out["valid"] is True
    assert out["summary"]["valid"] == 1
    assert not repo.files                                # NOTHING written on validate


def test_service_import_success_then_duplicate_file():
    svc, repo = _svc()
    body = _csv("Container Number,Timestamp,Mode",
                f"{CN_A},01/07/2026 14:00,In", f"{CN_B},01/07/2026 10:00,Out")
    r1 = asyncio.run(svc.import_file("CFS", body, "x.csv", "tester"))
    assert r1["status"] == "SUCCESS" and r1["imported"] == 2 and r1["duplicate_file"] is False
    # Re-uploading the EXACT same bytes is a safe no-op: flagged as a duplicate file
    # (nothing new written — the movements set is unchanged), echoing the prior count.
    before = len(repo.movements)
    r2 = asyncio.run(svc.import_file("CFS", body, "x.csv", "tester"))
    assert r2["status"] == "SKIPPED_DUPLICATE" and r2["duplicate_file"] is True
    assert len(repo.movements) == before          # no new movement rows written


def test_service_import_partial_when_some_rows_invalid():
    svc, repo = _svc()
    body = _csv("Container Number,Timestamp,Mode",
                f"{CN_A},01/07/2026 14:00,In", f"{CN_B},bad,Out")
    r = asyncio.run(svc.import_file("CFS", body, "x.csv", "tester"))
    assert r["status"] == "PARTIAL" and r["imported"] == 1 and r["invalid"] == 1
    assert repo.files[r["file_id"]]["import_status"] == "PARTIAL"


def test_service_import_rejected_on_missing_columns():
    svc, repo = _svc()
    r = asyncio.run(svc.import_file("CFS", _csv("Box,When", "X,Y"), "x.csv", "tester"))
    assert r["status"] == "REJECTED" and r["imported"] == 0
    assert repo.files[r["file_id"]]["import_status"] == "FAILED"      # ledgered as FAILED


def test_service_history_lists_uploads():
    svc, repo = _svc()
    asyncio.run(svc.import_file("CFS", _csv("Container Number,Timestamp,Mode",
                                            f"{CN_A},01/07/2026 14:00,In"), "a.csv", "tester"))
    hist = asyncio.run(svc.list_uploads({}, limit=25, offset=0))
    assert hist["total"] == 1 and hist["items"][0]["source_file"] == "a.csv"


# --------------------------------------------------------------------- router
@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from gateway.main import app
    from gateway.routers import cfs_ecy as router

    svc, _repo = _svc()
    app.dependency_overrides[router.get_upload_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(router.get_upload_service, None)


def test_router_template_download(client):
    r = client.get("/api/cfs-ecy/templates/CFS")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.text.splitlines()[0].startswith("Container Number,Timestamp,Mode")


def test_router_template_bad_facility_400(client):
    assert client.get("/api/cfs-ecy/templates/ICD").status_code == 400


def test_router_validate_endpoint(client):
    body = _csv("Container Number,Timestamp,Mode", f"{CN_A},01/07/2026 14:00,In")
    r = client.post("/api/cfs-ecy/validate", data={"facility": "CFS"},
                    files={"file": ("x.csv", body, "text/csv")})
    assert r.status_code == 200
    assert r.json()["status"] == "VALIDATED"


# --------------------------------------------------------------- schema lock-step
_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(jnpa\.\w+)", re.IGNORECASE)
_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(jnpa\.\w+)", re.IGNORECASE)


def _objects(text: str) -> set[str]:
    return {m.lower() for m in _TABLE.findall(text)} | {m.lower() for m in _VIEW.findall(text)}


def test_migration_and_ext_define_same_objects():
    from gateway.cfs_ecy_ext import _DDL
    mig = ((REPO_ROOT / "infra" / "postgres" / "migrations" / "0027_cfs_ecy_codeco.sql").read_text()
           + "\n"
           + (REPO_ROOT / "infra" / "postgres" / "migrations" / "0034_cfs_ecy_upload.sql").read_text())
    migration_objs = _objects(mig)
    ext_objs = _objects("\n".join(_DDL))
    assert migration_objs == ext_objs, (
        f"schema drift between migrations and cfs_ecy_ext._DDL:\n"
        f"  only in migration: {sorted(migration_objs - ext_objs)}\n"
        f"  only in _DDL:      {sorted(ext_objs - migration_objs)}")


def test_expected_upload_objects_present():
    from gateway.cfs_ecy_ext import _DDL
    objs = _objects("\n".join(_DDL))
    for name in ("jnpa.cfs_ecy_movements", "jnpa.cfs_ecy_import_files",
                 "jnpa.cfs_ecy_import_errors"):
        assert name in objs, f"missing CFS-ECY upload object: {name}"


# --------------------------------------------------- real-file parse (opt-in)
@pytest.mark.skipif(not _DATA_DIR.exists(), reason="JNPA CFS-ECY data folder not present")
def test_real_codeco_files_parse():
    total_valid = 0
    total_dupes = 0
    for fname, fac in (("CFS-CODECO.xlsx", "CFS"), ("ECY-CODECO.xlsx", "ECY")):
        content = (_DATA_DIR / fname).read_bytes()
        header, rows = P.read_rows_from_bytes(content, fname)
        res = P.parse(header, rows, facility=fac, source_file=fname)
        assert res.invalid_count == 0
        total_valid += len(res.records)
        total_dupes += res.duplicate_count
    assert total_valid == 1928        # 967 CFS + 961 ECY (the 1 dup CFS row dropped)
    assert total_dupes == 1
