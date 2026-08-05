"""Tests for the Berthing Reports module (/api/berthing) — UC-III module 7.

Mirrors tests/test_cfs_ecy_upload.py. Layers, all runnable without a live Postgres:

* Pure upload-parser checks — template, alias-driven column mapping, per-row validation
  (empty vessel / bad terminal / bad timestamp), in-file duplicate detection,
  missing-column rejection, CSV + XLSX byte readers, status derivation.
* PDF parser — the real per-terminal parsers over synthetic + (opt-in) real files.
* UploadService orchestration against an in-memory fake repository — validate (dry-run),
  import (SUCCESS / PARTIAL / SKIPPED_DUPLICATE / REJECTED), history.
* Router wiring via Starlette's TestClient — template download, 400 on bad terminal,
  validate endpoint.
* RBAC — require_uploader gate (dev-open vs auth-enforced 403).
* Lifecycle events — the pure _events_for derivation.
* Schema lock-step — migration 0036 objects == gateway.berthing_ext._DDL.
* A REAL-file parse assertion (skipped when the JNPA data folder is absent):
  25 files → 429 rows with the expected per-terminal split.
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

# Unroutable DSN so any accidental real-DB path fails FAST (the fake bypasses it).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.berthing import pdf_parsers as PP  # noqa: E402
from services.berthing import upload_parsers as P  # noqa: E402
from services.berthing.repository import BerthingRepository  # noqa: E402
from services.berthing.upload_service import BerthingUploadService  # noqa: E402

_DATA_DIR = Path("/Users/pandurangdhage/Downloads/Digital Twin/Data/7-Berthing Reports")


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ------------------------------------------------------------------ pure parser

# schema-v3: the boot-time DDL is retired (JNPA_RUNTIME_DDL gate); the canonical
# definitions live in infra/postgres/v3/. The drift test now asserts every object
# the ext DDL would create is defined by the v3 runbook (ext subset-of v3).
def _v3_objects() -> set[str]:
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / "infra" / "postgres" / "v3"
    text = "\n".join(p.read_text() for p in sorted(root.glob("*.sql")))
    pat_t = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?((?:core|mart|staging)\.\w+)", re.I)
    pat_v = re.compile(r"CREATE (?:OR REPLACE )?VIEW ((?:core|mart)\.\w+)", re.I)
    pat_a = re.compile(r"ALTER TABLE ((?:core|mart)\.\w+)", re.I)
    return ({m.lower() for m in pat_t.findall(text)}
            | {m.lower() for m in pat_v.findall(text)}
            | {m.lower() for m in pat_a.findall(text)})


def _ext_objects() -> set[str]:
    from gateway.berthing_ext import _DDL
    pat = re.compile(r"CREATE (?:TABLE (?:IF NOT EXISTS )?|(?:OR REPLACE )?VIEW )((?:core|mart)\.\w+)", re.I)
    src = "\n".join(_DDL) if not isinstance(_DDL, str) else _DDL
    return {m.lower() for m in pat.findall(src)}


def test_template_has_required_columns():
    t = P.template_csv()
    header = t.splitlines()[0].split(",")
    assert header[:4] == ["Terminal", "Vessel Name", "IMO Number", "Voyage Number"]
    assert "REQUIRED" in t and "Terminal" in t
    assert "MAERSK SENTOSA" in t                          # example row


def test_alias_column_mapping():
    # "Vessel" / "VIA" / "Berth" must map to the canonical fields.
    body = _csv("Terminal,Vessel,VIA,Berth", "NSICT,EUROPE,S0546,CB05")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, terminal=None, source_file="x.csv")
    assert not res.rejected and len(res.records) == 1
    r = res.records[0]
    assert r["vessel_name"] == "EUROPE" and r["voyage_number"] == "S0546"
    assert r["terminal"] == "NSICT" and r["berth_number"] == "CB05"


def test_missing_required_column_is_friendly_rejection():
    body = _csv("Foo,Bar", "X,Y")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, terminal="NSICT")
    assert res.rejected
    assert {e["error_code"] for e in res.errors} == {"missing_column"}
    assert any("Vessel Name column not found" in e["error_detail"] for e in res.errors)


def test_per_row_validation_and_in_file_dedup():
    body = _csv(
        "Terminal,Vessel Name,Voyage Number,ETA",
        "NSICT,MAERSK SENTOSA,S0488,05/06/2026 16:00",
        "NSICT,MAERSK SENTOSA,S0488,05/06/2026 16:00",   # exact in-file duplicate
        "NSICT,,S0500,05/06/2026 16:00",                 # empty vessel
        "ZZZ,GOOD SHIP,S0501,05/06/2026 16:00",          # bad terminal
        "NSICT,BAD DATE,S0502,not-a-date",               # bad timestamp
    )
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, terminal=None)
    assert len(res.records) == 1                          # only the first row
    assert res.duplicate_count == 1
    assert res.invalid_count == 3                         # empty vessel + bad terminal + bad ts
    assert {e["error_code"] for e in res.errors} == {
        "empty_required", "invalid_terminal", "invalid_timestamp"}


def test_status_derivation_and_selector_default():
    body = _csv("Vessel Name,Voyage Number,Berth Number,ATA,Departure Time",
                "SHIP A,S1,CB01,05/06/2026 09:00,06/06/2026 09:00")
    header, rows = P.read_rows_from_bytes(body, "x.csv")
    res = P.parse(header, rows, terminal="BMCT")          # terminal from selector
    r = res.records[0]
    assert r["terminal"] == "BMCT" and r["status"] == "DEPARTED"   # has a departure


def test_unsupported_and_empty_files_raise():
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"whatever", "x.pdf")      # PDF not via upload parser
    with pytest.raises(ValueError):
        P.read_rows_from_bytes(b"", "x.csv")


def test_xlsx_byte_reader_roundtrip():
    openpyxl = pytest.importorskip("openpyxl")
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Terminal", "Vessel Name", "Voyage Number"])
    ws.append(["NSIGT", "REN JIAN 17", "S0565"])
    buf = io.BytesIO(); wb.save(buf)
    header, rows = P.read_rows_from_bytes(buf.getvalue(), "x.xlsx")
    res = P.parse(header, rows, terminal=None)
    assert len(res.records) == 1 and res.records[0]["terminal"] == "NSIGT"


# ------------------------------------------------------------------ PDF parser
def test_pdf_text_parser_extracts_calls():
    # Synthetic NSICT-shaped text: header + on-berth + expected rows.
    text = (
        "DAILY BERTHING REPORT - NSICT\nDATE: 04/06/2026 7:06\n"
        "VESSELS ON BERTH\n"
        "BERTH VESSEL NAME VIA LOA SERVICE BERTH SIDE IMPORT EXPORT TTL MVS ATA OPS COMMENCE ETC ETD\n"
        "CB04 HONG DA XIN 768 S0603 198.16 ADHOC STB 211 333 04/06/2026 02:55 04/06/2026 03:45 04/06/2026 17:00\n"
        "VESSELS EXPECTED\n"
        "1 AGIOS DIMITRIOS AGMS0655 299.20 INDUS MSC Thu/04/06 16:00 03/1900\n"
    )
    recs = PP.parse_text(text, "NSICT", "CB", filename="BERTHING-CT04062026.pdf")
    assert len(recs) == 2
    by_v = {r["voyage_number"]: r for r in recs}
    assert by_v["S0603"]["vessel_name"] == "HONG DA XIN 768"
    assert by_v["S0603"]["berth_number"] == "CB04"
    assert by_v["S0603"]["status"] == "CARGO_OPERATION"   # ops-commence present
    assert by_v["S0603"]["ata"] is not None
    assert by_v["S0655"]["vessel_name"] == "AGIOS DIMITRIOS"
    assert by_v["S0655"]["status"] in ("EXPECTED", "ARRIVED")


# ------------------------------------------------------- UploadService + fake repo
class FakeRepo:
    """In-memory stand-in for BerthingRepository's upload surface. Calls are keyed by
    (terminal, voyage, vessel) so the upsert (insert vs update) semantics are reproduced."""

    def __init__(self) -> None:
        self.files: dict = {}
        self.errors: dict = {}
        self.calls: dict = {}                             # (terminal, voyage, vessel) -> rec
        self._seq = 0
        self._by_hash: dict = {}

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def find_file_by_hash(self, file_hash):
        fid = self._by_hash.get(file_hash)
        return self.files.get(fid) if fid else None

    async def persist(self, records, *, terminal, filename, file_hash, physical_format,
                      file_size=None, uploaded_by=None, source="UPLOAD"):
        if file_hash in self._by_hash:
            f = self.files[self._by_hash[file_hash]]
            return {"file_id": f["id"], "status": "SKIPPED_DUPLICATE", "inserted": 0,
                    "updated": 0, "success_rows": f["success_rows"], "duplicate_file": True}
        fid = self._next()
        inserted = updated = 0
        for r in records:
            key = (r["terminal"], r["voyage_number"], r["vessel_name"])
            if key in self.calls:
                updated += 1
            else:
                inserted += 1
            self.calls[key] = r
        success = inserted + updated
        self.files[fid] = {"id": fid, "terminal": terminal, "filename": filename,
                           "physical_format": physical_format, "total_rows": len(records),
                           "success_rows": success, "failed_rows": 0, "duplicate_rows": 0,
                           "status": "SUCCESS", "uploaded_by": uploaded_by, "source": source,
                           "created_at": "2026-07-20T00:00:00"}
        self._by_hash[file_hash] = fid
        return {"file_id": fid, "status": "SUCCESS", "inserted": inserted,
                "updated": updated, "success_rows": success, "duplicate_file": False}

    async def record_rejected_upload(self, *, terminal, physical_format, filename, file_hash,
                                     uploaded_by, detail, errors):
        if file_hash in self._by_hash:
            return self._by_hash[file_hash]
        fid = self._next()
        self.files[fid] = {"id": fid, "terminal": terminal, "filename": filename,
                           "physical_format": physical_format, "total_rows": 0,
                           "success_rows": 0, "failed_rows": len(errors), "duplicate_rows": 0,
                           "status": "FAILED", "uploaded_by": uploaded_by, "source": "UPLOAD",
                           "created_at": "2026-07-20T00:00:00", "error_detail": detail}
        self._by_hash[file_hash] = fid
        self.errors[fid] = list(errors)
        return fid

    async def add_row_errors(self, file_id, errors):
        self.errors.setdefault(file_id, []).extend(errors)

    async def mark_partial(self, file_id, *, failed_rows, duplicate_rows=0):
        self.files[file_id]["status"] = "PARTIAL"
        self.files[file_id]["failed_rows"] = failed_rows
        self.files[file_id]["duplicate_rows"] = duplicate_rows

    async def set_duplicates(self, file_id, *, duplicate_rows):
        self.files[file_id]["duplicate_rows"] = duplicate_rows

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
    repo = FakeRepo()
    return BerthingUploadService(repository=repo), repo


def test_service_validate_is_dry_run():
    svc, repo = _svc()
    body = _csv("Terminal,Vessel Name,Voyage Number", "NSICT,EUROPE,S0546")
    out = asyncio.run(svc.validate("NSICT", body, "x.csv", "tester"))
    assert out["status"] == "VALIDATED" and out["valid"] is True
    assert out["summary"]["valid"] == 1
    assert not repo.files                                 # NOTHING written on validate


def test_service_import_success_then_duplicate_file():
    svc, repo = _svc()
    body = _csv("Terminal,Vessel Name,Voyage Number",
                "NSICT,EUROPE,S0546", "NSICT,MAERSK SENTOSA,S0488")
    r1 = asyncio.run(svc.import_file("NSICT", body, "x.csv", "tester"))
    assert r1["status"] == "SUCCESS" and r1["imported"] == 2 and r1["duplicate_file"] is False
    r2 = asyncio.run(svc.import_file("NSICT", body, "x.csv", "tester"))
    assert r2["status"] == "SKIPPED_DUPLICATE" and r2["duplicate_file"] is True


def test_service_import_partial_when_some_rows_invalid():
    svc, repo = _svc()
    body = _csv("Terminal,Vessel Name,Voyage Number,ETA",
                "NSICT,EUROPE,S0546,05/06/2026 16:00", "NSICT,BAD,S0002,not-a-date")
    r = asyncio.run(svc.import_file("NSICT", body, "x.csv", "tester"))
    assert r["status"] == "PARTIAL" and r["imported"] == 1 and r["invalid"] == 1
    assert repo.files[r["file_id"]]["status"] == "PARTIAL"


def test_service_import_rejected_on_missing_columns():
    svc, repo = _svc()
    r = asyncio.run(svc.import_file("NSICT", _csv("Foo,Bar", "X,Y"), "x.csv", "tester"))
    assert r["status"] == "REJECTED" and r["imported"] == 0
    assert repo.files[r["file_id"]]["status"] == "FAILED"


def test_service_history_lists_uploads():
    svc, repo = _svc()
    asyncio.run(svc.import_file("NSICT", _csv("Terminal,Vessel Name,Voyage Number",
                                              "NSICT,EUROPE,S0546"), "a.csv", "tester"))
    hist = asyncio.run(svc.list_uploads({}, limit=25, offset=0))
    assert hist["total"] == 1 and hist["items"][0]["filename"] == "a.csv"


# --------------------------------------------------------------------- router
@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from gateway.main import app
    from gateway.routers import berthing as router

    svc, _repo = _svc()
    app.dependency_overrides[router.get_upload_service] = lambda: svc
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(router.get_upload_service, None)


def test_router_template_download(client):
    r = client.get("/api/berthing/templates/NSICT")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.text.splitlines()[0].startswith("Terminal,Vessel Name")


def test_router_template_bad_terminal_400(client):
    assert client.get("/api/berthing/templates/ZZZ").status_code == 400


def test_router_validate_endpoint(client):
    body = _csv("Terminal,Vessel Name,Voyage Number", "NSICT,EUROPE,S0546")
    r = client.post("/api/berthing/validate", data={"terminal": "NSICT"},
                    files={"file": ("x.csv", body, "text/csv")})
    assert r.status_code == 200
    assert r.json()["status"] == "VALIDATED"


# --------------------------------------------------------------------- RBAC
def test_require_uploader_dev_open(monkeypatch):
    from gateway.routers import berthing
    monkeypatch.setattr(berthing, "auth_enabled", lambda: False)
    import types
    req = types.SimpleNamespace(state=types.SimpleNamespace())
    assert berthing.require_uploader(req) == "dev"


def test_require_uploader_forbidden_without_role(monkeypatch):
    from fastapi import HTTPException
    from gateway.routers import berthing
    monkeypatch.setattr(berthing, "auth_enabled", lambda: True)
    import types
    # No principal → 403.
    req = types.SimpleNamespace(state=types.SimpleNamespace(principal=None))
    with pytest.raises(HTTPException) as exc:
        berthing.require_uploader(req)
    assert exc.value.status_code == 403
    # Principal WITH an allowed role → returns its subject.
    princ = types.SimpleNamespace(role="DTCCC_ADMIN", sub="admin-1")
    req2 = types.SimpleNamespace(state=types.SimpleNamespace(principal=princ))
    assert berthing.require_uploader(req2) == "admin-1"


# --------------------------------------------------------------- lifecycle events
def test_events_derivation_from_timestamps():
    import datetime as dt
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    rec = {"berth_number": "CB04", "status": "DEPARTED",
           "eta": dt.datetime(2026, 6, 4, 16, 0, tzinfo=ist),
           "ata": dt.datetime(2026, 6, 4, 2, 55, tzinfo=ist),
           "berthing_time": dt.datetime(2026, 6, 4, 2, 55, tzinfo=ist),
           "cargo_operation_start": dt.datetime(2026, 6, 4, 3, 45, tzinfo=ist),
           "cargo_operation_end": None,
           "departure_time": dt.datetime(2026, 6, 4, 17, 0, tzinfo=ist)}
    events = BerthingRepository._events_for(rec, 7, "importer")
    types_ = {e["event_type"] for e in events}
    assert {"EXPECTED", "ARRIVED", "BERTH_ASSIGNED", "BERTHING_STARTED",
            "CARGO_OPERATION", "DEPARTED"} <= types_
    assert all(e["berthing_id"] == 7 for e in events)


# --------------------------------------------------------------- PDF upload path
_APM_PDF = _DATA_DIR / "APM Terminals" / "APMT_Berthing_Report_-_04-Jun-2026.pdf"


def test_pdf_detection_and_is_pdf():
    assert P.is_pdf("x.pdf") and not P.is_pdf("x.csv")
    # Terminal auto-detect from report text markers.
    assert PP.detect_terminal("DAILY BERTHING REPORT - NSICT") == ("NSICT", "CB")
    assert PP.detect_terminal("NHAVA SHEVA FREEPORT TERMINAL") == ("NSFT", "NSFT")
    assert PP.detect_terminal("APM Terminals Mumbai") == ("APMT", "APM")
    assert PP.detect_terminal("random text") is None


def test_pdf_invalid_bytes_rejected():
    # A non-PDF / corrupt payload with a .pdf name → structured REJECTED, no crash.
    res = P.parse_pdf(b"%PDF-not-really-a-pdf", "junk.pdf", terminal=None)
    assert res.rejected and res.errors and res.errors[0]["error_code"] == "unreadable_pdf"
    svc, repo = _svc()
    r = asyncio.run(svc.import_file(None, b"%PDF-nope", "junk.pdf", "tester"))
    assert r["status"] == "REJECTED" and r["imported"] == 0
    assert repo.files[r["file_id"]]["physical_format"] == "PDF"


def test_pdf_missing_pdfplumber_degrades(monkeypatch):
    # If pdfplumber is not installed, a PDF upload must degrade to a clean REJECTED
    # (ValueError → error ledger), never a bare ImportError / 500. Simulate absence.
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    res = P.parse_pdf(b"%PDF-1.7 anything", "x.pdf", terminal=None)
    assert res.rejected and res.errors[0]["error_code"] == "unreadable_pdf"


@pytest.mark.skipif(not _APM_PDF.exists(), reason="JNPA Berthing Reports data folder not present")
def test_pdf_upload_validate_real():
    content = _APM_PDF.read_bytes()
    res = P.parse_pdf(content, _APM_PDF.name, terminal=None)
    assert not res.rejected and res.detected_terminal == "APMT" and len(res.records) == 4
    # Preview carries the requested columns.
    assert set(res.preview[0]) >= {"Terminal", "Vessel", "Voyage", "ETA", "Berth", "Status"}
    svc, _ = _svc()
    out = asyncio.run(svc.validate(None, content, _APM_PDF.name, "tester"))
    assert out["status"] == "VALIDATED" and out["valid"] is True and out["terminal"] == "APMT"


@pytest.mark.skipif(not _APM_PDF.exists(), reason="JNPA Berthing Reports data folder not present")
def test_pdf_import_success_then_duplicate_real():
    content = _APM_PDF.read_bytes()
    svc, repo = _svc()
    r1 = asyncio.run(svc.import_file(None, content, _APM_PDF.name, "tester"))
    assert r1["status"] in ("SUCCESS", "PARTIAL") and r1["imported"] == 4
    # History shows the PDF filename + PDF format.
    hist = asyncio.run(svc.list_uploads({}, limit=10, offset=0))
    top = hist["items"][0]
    assert top["filename"] == _APM_PDF.name and top["physical_format"] == "PDF"
    assert top["terminal"] == "APMT"
    # Re-uploading identical bytes → safe no-op.
    r2 = asyncio.run(svc.import_file(None, content, _APM_PDF.name, "tester"))
    assert r2["status"] == "SKIPPED_DUPLICATE" and r2["duplicate_file"] is True


@pytest.mark.skipif(not _APM_PDF.exists(), reason="JNPA Berthing Reports data folder not present")
def test_router_pdf_validate_real(client):
    content = _APM_PDF.read_bytes()
    r = client.post("/api/berthing/validate", data={"terminal": "ALL"},
                    files={"file": (_APM_PDF.name, content, "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "VALIDATED" and body["terminal"] == "APMT"


# --------------------------------------------------------------- schema lock-step
_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+((?:core|mart)\.\w+)", re.IGNORECASE)


def _tables(text: str) -> set:
    return {m.lower() for m in _TABLE.findall(text)}


def test_migration_and_ext_define_same_objects():
    """v3: every object the (retired) boot DDL would create must be defined by the
    canonical infra/postgres/v3 runbook — the ext copy can never drift ahead."""
    v3 = _v3_objects()
    ext_objs = _ext_objects()
    assert ext_objs, "no CREATE TABLE/VIEW found in ext _DDL"
    missing = sorted(ext_objs - v3)
    assert not missing, f"objects in ext _DDL missing from infra/postgres/v3: {missing}"


def test_expected_objects_present():
    from gateway.berthing_ext import _DDL
    objs = _tables("\n".join(_DDL))
    for name in ("core.berthing_record", "core.berthing_record_event",
                 "core.berthing_import_file", "core.berthing_import_error",
                 "core.berthing_report_document", "core.berthing_report_table"):
        assert name in objs, f"missing berthing object: {name}"


# --------------------------------------------------- real-file parse (opt-in)
@pytest.mark.skipif(not _DATA_DIR.exists(), reason="JNPA Berthing Reports data folder not present")
def test_real_pdfs_parse():
    total = 0
    per_terminal = {}
    for folder, (terminal, kind) in PP.TERMINALS.items():
        d = _DATA_DIR / folder
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".pdf"):
                continue
            recs = PP.parse_pdf_bytes((d / fn).read_bytes(), terminal, kind, filename=fn)
            per_terminal[terminal] = per_terminal.get(terminal, 0) + len(recs)
            total += len(recs)
    assert total == 429, f"expected 429 parsed rows, got {total} ({per_terminal})"
    assert per_terminal == {"APMT": 17, "BMCT": 37, "NSFT": 126, "NSICT": 170, "NSIGT": 79}
