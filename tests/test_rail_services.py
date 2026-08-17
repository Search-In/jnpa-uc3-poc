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
# The corpus folder has been renamed at least once ("Digital Twin - Updated" ->
# "Digital Twin Data Corpus - Updated"). A single hardcoded path meant that when
# it moved, every corpus-backed case in this file quietly turned into a SKIP and
# the suite still reported green — so the parsers went unexercised against the
# real files for as long as the stale path survived. Resolve against the known
# locations instead, newest name first, and allow JNPA_CORPUS_DIR to override.
_CORPUS_CANDIDATES = (
    os.environ.get("JNPA_CORPUS_DIR", ""),
    "/Users/aniketchopade/Downloads/Digital Twin Data Corpus - Updated/Data",
    "/Users/aniketchopade/Downloads/Digital Twin - Updated/Data",
)
DUMP = next(
    (Path(c) for c in _CORPUS_CANDIDATES if c and Path(c).is_dir()),
    Path(_CORPUS_CANDIDATES[1]),
)
FOIS_CSV = DUMP / "9-NLDS_FOIS/FOIS/JNPA Train Intimation 22072026_083001.csv"
FOIS_EMPTY = DUMP / "9-NLDS_FOIS/FOIS/JNPA Train Intimation 23062026_083000.csv"
CTO_A = DUMP / "10-Form 11_ICD Rail/CTO/R261076 HTPL.txt"      # dated-first layout
CTO_B = DUMP / "10-Form 11_ICD Rail/CTO/R261628 JKTI.TXT"      # rake-first / empty wagons
FORM11_BMCT = DUMP / "10-Form 11_ICD Rail/Form 11/BMCT FORM 11-1.xlsx"
FORM11_NSICT = DUMP / "10-Form 11_ICD Rail/Form 11/BT NSICT GENERAL-1.xlsx"
FORM11_NSIGT = DUMP / "10-Form 11_ICD Rail/Form 11/BT NSIGT GENERAL-1.xlsx"
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
            "FOIS": [], "FORM11": [], "CTO": [], "ICD_REPORT": []}
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
    # This sheet names the vessel visit `VIA` and states the depot outright.
    assert r["via"] == "S0651"
    assert r["icd_location"] == "TKD"


@_need(FORM11_NSICT)
def test_form11_parser_nsict_general_variant():
    res = form11_icd.parse(FORM11_NSICT.read_bytes(), FORM11_NSICT.name)
    assert res.feed == "FORM11"
    assert len(res.rows) >= 1
    r = res.rows[0]
    assert r["terminal"] == "NSICT"
    assert r["container_no"] == "MSMU6853774"
    assert r["booking_number"] == "EBKG170482021006"

    # GAP-RAIL-01. The DP-World layout names the vessel visit
    # TERMINAL_VISIT_NUMBER, not VIA. Only VIA was aliased, so this row carried
    # no vessel at all and the rail hop could not reach a call.
    assert r["via"] == "AGMS0654"

    # And the ICD is PORT_OF_ORIGIN, not LOCATION. `LOCATION` is INNSA1 — JNPA
    # itself, the port this pre-advice is lodged AGAINST — and a generic
    # `location` alias was reading it as the inland origin, so every rail box
    # appeared to start at the port it was travelling to.
    assert r["icd_location"] == "INBLR"
    assert r["icd_location"] != "INNSA1"
    # the raw columns are still inspectable rather than discarded
    assert r["extra"]["LOCATION"] == "INNSA1"
    assert r["extra"]["SPNAME"] == "INNSA1NSI1"


@_need(FORM11_NSIGT)
def test_form11_rail_hop_reaches_its_vessel_call():
    """The documented rail hop: GLDU9466140 -> SRES0711 -> MSC SARA ELENA.

    `SRES0711` is the join key to core.berthing_report_vessel.via_no. Without it
    the whole rail leg of the export story is unreachable, which is what
    GAP-RAIL-01 was.
    """
    res = form11_icd.parse(FORM11_NSIGT.read_bytes(), FORM11_NSIGT.name)
    r = next(x for x in res.rows if x["container_no"] == "GLDU9466140")
    assert r["via"] == "SRES0711"
    assert r["icd_location"] == "INTKD"        # Tughlakabad, not INNSA1
    assert r["pod"] == "DEHAM"


def test_form11_does_not_invent_an_icd_for_a_road_origin():
    """PORT_OF_ORIGIN is only the ICD when the move actually came in by rail.

    On a road-origin pre-advice the same column holds a port, so reading it as
    an inland depot would fabricate a rail leg that never happened. An unstated
    origin has to stay unstated.
    """
    road = {"portoforigin": "INNSA1", "origintype": "Road", "arrivalmode": "T"}
    assert form11_icd._icd_location(road) is None

    rail = {"portoforigin": "INTKD", "origintype": "ICD Rail", "arrivalmode": "R"}
    assert form11_icd._icd_location(rail) == "INTKD"

    # An explicit column always wins over the inference.
    both = {"icdlocation": "TKD", "portoforigin": "INBLR", "origintype": "ICD Rail"}
    assert form11_icd._icd_location(both) == "TKD"


def test_unreadable_pdf_is_rejected_not_crashed():
    """ICD PDFs ARE parsed now (GAP-ETL-04/07), but a PDF with no report in it
    must still come back as a ledgered rejection rather than an exception."""
    res = form11_icd.parse(SYNTH_PDF, "ICD_DAILY_REPORT_01-JUL-2026.pdf")
    assert res.rejected is True
    assert res.reason in ("unreadable_pdf", "no_parsable_blocks")
    assert res.rows == []


@_need(ICD_PDF)
def test_icd_report_pdf_parses_the_pendency_table():
    res = form11_icd.parse(ICD_PDF.read_bytes(), ICD_PDF.name)
    assert res.feed == "ICD_REPORT"
    assert not res.rejected
    pend = [r for r in res.rows if r["kind"] == "PENDENCY"]
    # 7 terminals x 3 carrier series x 30 destination columns.
    assert len(pend) == 630
    assert {r["terminal"] for r in pend} == {
        "NSFT", "NSDT", "NSICT", "NSIGT", "GTICT", "BMCT", "JNPORT"}
    assert {r["series"] for r in pend} == {"CONCOR", "OTHER_CARRIER", "TOTAL"}
    assert all(r["report_date"] == "2026-07-01" for r in pend)


@_need(ICD_PDF)
def test_icd_report_columns_are_not_run_together():
    """The regression this parser exists to avoid.

    Every glyph in these PDFs is separately positioned, so a naive read returns
    `1 3 7 2 5 ...` for a row that means 137, 2, 5 — and a gap-based split merges
    wide neighbouring cells into one impossible number. NSFT's first destination
    on 1 July is 137 TEU; a TEU count with more than five digits is proof the
    columns have been run together.
    """
    res = form11_icd.parse(ICD_PDF.read_bytes(), ICD_PDF.name)
    nsft = {r["fpd_code"]: r["teu"] for r in res.rows
            if r["kind"] == "PENDENCY" and r["terminal"] == "NSFT"
            and r["series"] == "CONCOR"}
    assert nsft["TKD"] == 137
    assert nsft["DER"] == 52
    assert max(r["teu"] for r in res.rows if r["kind"] == "PENDENCY") < 100_000


@_need(ICD_PDF)
def test_icd_report_reconciles_against_its_own_arithmetic():
    """CONCOR + other carriers must equal the printed Total.

    This is the only independent check available on the column reconstruction —
    there is no second copy of these figures. 20 cells across the 14 files do
    NOT reconcile; every one is a defect in the source (the PDD column at
    NSICT/GTICT), and each raises a warning rather than being silently adjusted.
    """
    res = form11_icd.parse(ICD_PDF.read_bytes(), ICD_PDF.name)
    by = {}
    for r in res.rows:
        if r["kind"] == "PENDENCY":
            by.setdefault((r["terminal"], r["series"]), {})[r["fpd_code"]] = r["teu"]
    mismatches = []
    for terminal in {t for t, _ in by}:
        c, o, t = (by[(terminal, s)] for s in ("CONCOR", "OTHER_CARRIER", "TOTAL"))
        mismatches += [(terminal, k) for k, v in t.items()
                       if c.get(k, 0) + o.get(k, 0) != v]
    # Only the known source defect, and it is reported rather than hidden.
    assert all(fpd == "PDD" for _t, fpd in mismatches), mismatches
    warned = [w for w in res.warnings
              if w["error_code"] == "pendency_does_not_reconcile"]
    assert len(warned) == len(mismatches)


@_need(ICD_PDF)
def test_icd_report_reads_rake_placements():
    res = form11_icd.parse(ICD_PDF.read_bytes(), ICD_PDF.name)
    rakes = [r for r in res.rows if r["kind"] == "RAKE"]
    assert rakes, "no rake placement lines found"
    assert all(r["rake_id"].startswith("R") for r in rakes)
    assert all(r["track"] in ("T1", "T2") for r in rakes)
    # Discharge composition is broken out by wagon class, not left as a blob.
    assert any(r["discharge"] for r in rakes)


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
    # A PDF carrying no report is still a REJECTION with a stated reason — the
    # parser now reads real ICD reports, but an unreadable file must never pass.
    assert out["status"] == "REJECTED"
    assert out["reason"] in ("unreadable_pdf", "no_parsable_blocks")
    assert out["imported"] == 0
    assert repo.rows["CTO"] == [] and repo.rows["FORM11"] == []
    assert repo.rejections


@_need(ICD_PDF)
def test_form11_icd_import_real_pdf_now_succeeds():
    """Previously asserted REJECTED/UNSUPPORTED_FORMAT — that was the gap.

    The composite ICD_REPORT feed lands both row families from one file under a
    single ledger entry.
    """
    repo = FakeRailRepo()
    svc = Form11IcdService(repository=repo)
    out = asyncio.run(svc.import_file(ICD_PDF.read_bytes(), ICD_PDF.name,
                                      "jnpa-api"))
    assert out["status"] == "SUCCESS"
    assert out["feed"] == "ICD_REPORT"
    assert out["imported"] == 630 + len(
        [r for r in repo.rows["ICD_REPORT"] if r.get("kind") == "RAKE"])


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
