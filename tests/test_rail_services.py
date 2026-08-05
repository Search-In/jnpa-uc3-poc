"""Rail consumer tests — pure parsers on REAL corpus bytes + the import
seam (sha256 dedup, PDF rejection) + the JNPA sync router flip (Pattern A:
in-memory fakes, no DB, no network).

Covers the Phase-4 guarantees:
  * the FOIS Train Intimation CSV, Form 11 XLSX and both CTO TXT layouts parse
    off the real dump (skipped when the dump is not on this machine);
  * a "no scheduled arrivals" day is a valid empty intimation, not a rejection;
  * re-importing identical bytes is a no-op (SKIPPED_DUPLICATE);
  * an ICD daily-report PDF is REJECTED (reason UNSUPPORTED_FORMAT), never
    crashed on;
  * services.jnpa_sync.JnpaRouter now routes rail-fois / rail-form11-icd to a
    consumer (proved with an injected fake) instead of UNROUTED, while
    edi-messages stays UNROUTED.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.rail.parsers import fois_csv, form11_icd  # noqa: E402
from services.rail.fois_service import RailFoisService  # noqa: E402
from services.rail.form11_icd_service import Form11IcdService  # noqa: E402
from services.jnpa_sync.routing import JnpaRouter, RouteOutcome  # noqa: E402


# --------------------------------------------------------------- real corpus
DUMP = Path("/Users/aniketchopade/Downloads/Digital Twin - Updated/Data")
FOIS_CSV = DUMP / "9-NLDS_FOIS/FOIS/JNPA Train Intimation 22072026_083001.csv"
FOIS_EMPTY = DUMP / "9-NLDS_FOIS/FOIS/JNPA Train Intimation 23062026_083000.csv"
CTO_A = DUMP / "10-Form 11_ICD Rail/CTO/R261076 HTPL.txt"      # dated-first layout
CTO_B = DUMP / "10-Form 11_ICD Rail/CTO/R261628 JKTI.TXT"      # rake-first / empty wagons
FORM11_BMCT = DUMP / "10-Form 11_ICD Rail/Form 11/BMCT FORM 11-1.xlsx"
FORM11_NSICT = DUMP / "10-Form 11_ICD Rail/Form 11/BT NSICT GENERAL-1.xlsx"
ICD_PDF = DUMP / "10-Form 11_ICD Rail/ICD_REPORTS/ICD_DAILY_REPORT_01-JUL-2026.pdf"


def _need(path: Path):
    return pytest.mark.skipif(not path.exists(),
                              reason=f"corpus file absent: {path.name}")


# --------------------------------------------------------------- synthetic bytes
_FOIS_HEADER = ("Eda,Edd,ZoneTo,Last Reporting Station,Units,Station From,"
                "Last Reporting Division,RakeId,RakeName,Station To,ZoneFrom,"
                "Last Reporting Zone,Loaded Empty Flag (L/E),Last Status Time")
SYNTH_FOIS = (_FOIS_HEADER + "\n"
              "22072026:23:52,19072026:09:19,CR,KWV,40,CSTN,SUR,"
              "TICD170723164056,SNF-J-50,JNPT,SC,CR,L,22072026:06:37\n"
              "22072026:13:28,22072026:03:22,CR,AFAS,46,AFAS,BCT,"
              "SMYD130525201712,ADANI-112,JNPT,WR,WR,L,22072026:06:00\n"
              ).encode("utf-8")

SYNTH_CTO = (
    "1,25-05-2026,11:00,TXHT-01,62290803501,CSLU1570675,20,L,OOCL,3.57,"
    "NORFOLK,NORFOLK,MKPP,NSFT,4510\n"
    "2,25-05-2026,11:00,TXHT-01,62290702743,UETU3055159,20,L,MSC,4.63,"
    "VENEZIA,VENEZIA,MKPP,BMCT,162508\n"
).encode("utf-8")

SYNTH_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


# =============================================================== fake repository
class FakeRailRepo:
    """In-memory twin of RailRepository with byte-identical keyword
    signatures for the surface the services touch."""

    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, Any]] = {}      # sha -> ledger envelope
        self.rows: Dict[str, List[Mapping[str, Any]]] = {
            "FOIS": [], "FORM11": [], "CTO": []}
        self.rejections: List[Dict[str, Any]] = []
        self.row_errors: List[Any] = []
        self.partials: List[int] = []
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def find_file_by_sha(self, sha256: str) -> Optional[dict]:
        return self.files.get(sha256)

    async def persist(self, feed: str, records: Sequence[Mapping[str, Any]], *,
                      source_file: str, source_sha256: str,
                      physical_format: str, file_size: Optional[int] = None,
                      uploaded_by: Optional[str] = None,
                      source: str = "API") -> dict:
        if source_sha256 in self.files:
            e = self.files[source_sha256]
            return {"file_id": e["id"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": e["record_count"],
                    "imported_count": e["imported_count"],
                    "error_count": e["error_count"],
                    "duplicate_count": e["duplicate_count"], "duplicate": True}
        fid = self._next_id()
        imported = len(records)
        self.files[source_sha256] = {
            "id": fid, "feed": feed, "record_count": len(records),
            "imported_count": imported, "error_count": 0, "duplicate_count": 0}
        self.rows[feed].extend(records)
        return {"file_id": fid, "import_status": "SUCCESS",
                "record_count": len(records), "imported_count": imported,
                "error_count": 0, "duplicate_count": 0, "duplicate": False}

    async def record_rejected(self, *, feed: str, physical_format: str,
                              source_file: str, source_sha256: str,
                              file_size: Optional[int],
                              uploaded_by: Optional[str], detail: str,
                              reason: str,
                              errors: Sequence[Mapping[str, Any]]
                              ) -> Optional[int]:
        if source_sha256 in self.files:
            return self.files[source_sha256]["id"]
        fid = self._next_id()
        self.files[source_sha256] = {
            "id": fid, "feed": feed, "record_count": 0, "imported_count": 0,
            "error_count": len(errors) or 1, "duplicate_count": 0}
        self.rejections.append({"file_id": fid, "reason": reason,
                                "detail": detail, "feed": feed})
        return fid

    async def add_row_errors(self, file_id: int,
                             errors: Sequence[Mapping[str, Any]]) -> None:
        self.row_errors.extend(errors)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        self.partials.append(file_id)


# =============================================================== parser tests
@_need(FOIS_CSV)
def test_fois_parser_real_corpus():
    res = fois_csv.parse(FOIS_CSV.read_bytes(), FOIS_CSV.name)
    assert res.feed == "FOIS"
    assert not res.rejected
    assert len(res.rows) > 10
    first = res.rows[0]
    assert first["rake_id"] == "TICD170723164056"
    assert first["station_to"] == "JNPT"
    assert first["units"] == 40
    # ETA parsed to an IST-aware datetime.
    assert first["eda"] is not None and first["eda"].tzinfo is not None


@_need(FOIS_EMPTY)
def test_fois_no_arrivals_day_is_valid_not_rejected():
    res = fois_csv.parse(FOIS_EMPTY.read_bytes(), FOIS_EMPTY.name)
    assert res.feed == "FOIS"
    assert res.rejected is False
    assert res.rows == []
    assert res.reason == "no_scheduled_arrivals"


@_need(CTO_A)
def test_cto_parser_dated_first_layout():
    res = form11_icd.parse(CTO_A.read_bytes(), CTO_A.name)
    assert res.feed == "CTO"
    assert len(res.rows) > 10
    r = res.rows[0]
    assert r["cto_code"] == "R261076"          # recovered from filename
    assert r["wagon_no"] == "62290803501"
    assert r["container_no"] == "CSLU1570675"
    assert r["is_empty"] is False
    assert r["event_ts"] is not None


@_need(CTO_B)
def test_cto_parser_rake_first_empty_wagons():
    res = form11_icd.parse(CTO_B.read_bytes(), CTO_B.name)
    assert res.feed == "CTO"
    assert len(res.rows) > 10
    r = res.rows[0]
    assert r["cto_code"] == "R261628"
    assert r["rake_id"] == "TWIL0322062026"    # explicit rake id (variant B)
    assert r["container_no"] is None           # EMPTY WAGON → no container
    assert r["is_empty"] is True


@_need(FORM11_BMCT)
def test_form11_parser_bmct_variant():
    res = form11_icd.parse(FORM11_BMCT.read_bytes(), FORM11_BMCT.name)
    assert res.feed == "FORM11"
    assert not res.rejected
    assert len(res.rows) >= 1
    r = res.rows[0]
    assert r["terminal"] == "BMCT"             # recovered from filename
    assert r["container_no"] == "MSMU2175621"


@_need(FORM11_NSICT)
def test_form11_parser_nsict_general_variant():
    res = form11_icd.parse(FORM11_NSICT.read_bytes(), FORM11_NSICT.name)
    assert res.feed == "FORM11"
    assert len(res.rows) >= 1
    r = res.rows[0]
    assert r["terminal"] == "NSICT"
    assert r["container_no"] == "MSMU6853774"
    assert r["booking_number"] == "EBKG170482021006"


def test_pdf_parser_flags_unsupported_not_crash():
    res = form11_icd.parse(SYNTH_PDF, "ICD_DAILY_REPORT_01-JUL-2026.pdf")
    assert res.feed == "UNSUPPORTED"
    assert res.unsupported is True
    assert res.reason == "UNSUPPORTED_FORMAT"


# =============================================================== service tests
def test_fois_import_success_then_skipped_duplicate():
    repo = FakeRailRepo()
    svc = RailFoisService(repository=repo)
    first = asyncio.run(svc.import_file(SYNTH_FOIS, "train.csv", "jnpa-api"))
    assert first["status"] == "SUCCESS"
    assert first["feed"] == "FOIS"
    assert first["imported"] == 2
    # identical bytes a second time — a no-op, never a second import.
    again = asyncio.run(svc.import_file(SYNTH_FOIS, "train.csv", "jnpa-api"))
    assert again["status"] == "SKIPPED_DUPLICATE"
    assert again["file_id"] == first["file_id"]
    assert len(repo.rows["FOIS"]) == 2


def test_form11_icd_import_cto_success():
    repo = FakeRailRepo()
    svc = Form11IcdService(repository=repo)
    out = asyncio.run(svc.import_file(SYNTH_CTO, "R261076 HTPL.txt", "jnpa-api"))
    assert out["status"] == "SUCCESS"
    assert out["feed"] == "CTO"
    assert out["imported"] == 2
    assert repo.rows["CTO"][0]["cto_code"] == "R261076"


def test_form11_icd_import_pdf_rejected_not_crash():
    repo = FakeRailRepo()
    svc = Form11IcdService(repository=repo)
    out = asyncio.run(svc.import_file(
        SYNTH_PDF, "ICD_DAILY_REPORT_01-JUL-2026.pdf", "jnpa-api"))
    assert out["status"] == "REJECTED"
    assert out["reason"] == "UNSUPPORTED_FORMAT"
    assert out["imported"] == 0
    assert repo.rows["CTO"] == [] and repo.rows["FORM11"] == []
    assert repo.rejections and repo.rejections[0]["reason"] == "UNSUPPORTED_FORMAT"


@_need(ICD_PDF)
def test_form11_icd_import_real_pdf_rejected():
    repo = FakeRailRepo()
    svc = Form11IcdService(repository=repo)
    out = asyncio.run(svc.import_file(ICD_PDF.read_bytes(), ICD_PDF.name,
                                      "jnpa-api"))
    assert out["status"] == "REJECTED"
    assert out["reason"] == "UNSUPPORTED_FORMAT"


# =============================================================== routing tests
class FakeRailService:
    """Records the routed bytes; returns a SUCCESS envelope like the real
    consumer's import_file."""

    def __init__(self, file_id: int = 99) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._file_id = file_id

    async def import_file(self, content: bytes, filename: str,
                          uploaded_by: str) -> Dict[str, Any]:
        self.calls.append({"filename": filename, "bytes": len(content),
                            "uploaded_by": uploaded_by})
        return {"status": "SUCCESS", "file_id": self._file_id, "imported": 5,
                "feed": "FOIS"}


def test_router_routes_rail_fois_to_consumer():
    fake = FakeRailService(file_id=101)
    router = JnpaRouter(services={"rail_fois": fake})
    outcome = asyncio.run(router.route(
        "rail-fois", filename="JNPA Train Intimation 22072026.csv",
        content=SYNTH_FOIS))
    assert isinstance(outcome, RouteOutcome)
    assert outcome.status == "SUCCESS"          # no longer UNROUTED
    assert outcome.service == "rail_fois"
    assert outcome.file_id == 101
    assert fake.calls and fake.calls[0]["uploaded_by"] == "jnpa-api"


def test_router_routes_rail_form11_icd_to_consumer():
    fake = FakeRailService(file_id=202)
    router = JnpaRouter(services={"rail_form11_icd": fake})
    outcome = asyncio.run(router.route(
        "rail-form11-icd", filename="R261076 HTPL.txt", content=SYNTH_CTO))
    assert outcome.status == "SUCCESS"
    assert outcome.service == "rail_form11_icd"
    assert outcome.file_id == 202
    assert fake.calls


def test_router_edi_messages_still_unrouted():
    router = JnpaRouter(services={})
    outcome = asyncio.run(router.route(
        "edi-messages", filename="CODECO_1.edi", content=b"UNB+..."))
    assert outcome.status == "UNROUTED"
