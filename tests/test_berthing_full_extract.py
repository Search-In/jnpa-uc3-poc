"""Tests for the Berthing FULL-EXTRACT sub-module (module 7) — verbatim PDF capture.

Layers, all runnable without a live Postgres:

* Pure helpers — terminal detection, template-key mapping, report-date parsing, template
  completeness (every template panel has a name + band).
* Router wiring via Starlette's TestClient — /extract (200 on a real PDF, 400 on garbage)
  and /extract/import against an in-memory fake document repository (IMPORTED then
  SKIPPED_DUPLICATE).
* Real-file assertions over ALL 25 PDFs (skipped when the JNPA data folder is absent):
  every expected panel is emitted (no missing core sections), the vessel tables carry no
  ICD/CFS cross-contamination, coverage holds (no crash, uncaptured is a list), and
  re-extracting identical bytes is deterministic.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.berthing import full_extractor as FE  # noqa: E402

_DATA_DIR = Path("/Users/pandurangdhage/Downloads/Digital Twin/Data/7-Berthing Reports")
_TERMS = {
    "APMT": "APM Terminals", "BMCT": "BMCT_PSA", "NSFT": "NSFT",
    "NSICT": "NSICT_DP World", "NSIGT": "NSIGT_DP World",
}
# Core panels that MUST be structured (not raw) for the 04-Jun representative file per terminal.
_CORE = {
    "APMT": {"ON_BERTH_VESSEL", "SAILED_VESSEL", "VESSELS_EXPECTED", "GATE_MOVEMENTS",
             "CFS_PENDENCY", "TRAFFIC_THROUGHPUT", "TIME_TABLE"},
    "BMCT": {"VESSELS_ON_BERTHED", "SAILED_VESSEL", "VESSELS_EXPECTED", "TIDE_TABLE",
             "GATE_MOVEMENTS", "ICD_PENDENCY", "CFS_PENDENCY", "TRAFFIC_THROUGHPUT"},
    "NSFT": {"VESSEL_SAILED_24H", "VESSEL_AT_BERTH", "VESSELS_EXPECTED", "TIDE_TABLE",
             "YARD_INVENTORY", "ICD_PENDANCY"},
    "NSICT": {"VESSELS_ON_BERTH", "SAILED_VESSELS", "VESSELS_EXPECTED", "TIDE_TABLE",
              "YARD_INV", "ICD_PENDENCY", "CFS_PENDENCY", "TRAFFIC_THROUGHPUT"},
    "NSIGT": {"VESSELS_ON_BERTH", "SAILED_VESSELS", "VESSELS_EXPECTED", "TIDE_TABLE",
              "YARD_INV", "ICD_PENDENCY", "CFS_PENDENCY", "TRAFFIC_THROUGHPUT"},
}
_REP = {  # 04-Jun representative file per terminal
    "APMT": "APMT_Berthing_Report_-_04-Jun-2026.pdf",
    "BMCT": "Berthing_Sheet__04_JUN_2026_JNPT.pdf",
    "NSFT": "Daily_Berthing_Report_4_6_2026.pdf",
    "NSICT": "BERTHING-CT04062026.pdf",
    "NSIGT": "BERTHING-GT04062026.pdf",
}


def _read(term: str, fn: str) -> bytes:
    return (_DATA_DIR / _TERMS[term] / fn).read_bytes()


# ------------------------------------------------------------------ pure helpers
def test_template_key_maps_dpworld():
    assert FE.template_key("NSICT") == "DPWORLD"
    assert FE.template_key("NSIGT") == "DPWORLD"
    assert FE.template_key("APMT") == "APMT"
    assert FE.template_key("BMCT") == "BMCT"
    assert FE.template_key("NSFT") == "NSFT"


def test_every_template_panel_is_well_formed():
    for key, panels in FE._TEMPLATES.items():
        names = [p["name"] for p in panels]
        assert len(names) == len(set(names)), f"duplicate panel name in {key}"
        for p in panels:
            assert p["name"] and isinstance(p["band"], tuple) and len(p["band"]) == 2
            assert 0.0 <= p["band"][0] < p["band"][1] <= 1.0


def test_report_date_formats():
    assert FE.parse_report_date("Date : 4-June-26").isoformat() == "2026-06-04"
    assert FE.parse_report_date("DATE: 04/06/2026 7:06").isoformat() == "2026-06-04"
    assert FE.parse_report_date("Tide Table Date 04.06.2026").isoformat() == "2026-06-04"


# ------------------------------------------------------------------ router + fake repo
class FakeDocRepo:
    def __init__(self) -> None:
        self.docs: dict = {}
        self._by_hash: dict = {}
        self._seq = 0

    async def find_by_hash(self, pdf_hash):
        did = self._by_hash.get(pdf_hash)
        return self.docs.get(did) if did else None

    async def persist(self, result, *, pdf_hash, uploaded_by):
        if pdf_hash in self._by_hash:
            d = self.docs[self._by_hash[pdf_hash]]
            return {"document_id": d["id"], "status": "SKIPPED_DUPLICATE", "terminal": d["terminal"],
                    "table_count": d["table_count"], "row_count": d["row_count"], "duplicate": True}
        self._seq += 1
        tables = result.get("tables", [])
        d = {"id": self._seq, "terminal": result.get("terminal"),
             "table_count": len([t for t in tables if t["table_name"] != "UNCAPTURED_TEXT"]),
             "row_count": sum(t["row_count"] for t in tables)}
        self.docs[self._seq] = d
        self._by_hash[pdf_hash] = self._seq
        return {"document_id": d["id"], "status": "IMPORTED", "terminal": d["terminal"],
                "table_count": d["table_count"], "row_count": d["row_count"], "duplicate": False}

    async def list_documents(self, *, terminal, limit, offset):
        return {"items": list(self.docs.values()), "total": len(self.docs), "limit": limit, "offset": offset}


@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from gateway.main import app
    from gateway.routers import berthing as router

    app.dependency_overrides[router.get_doc_repo] = lambda: FakeDocRepo()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(router.get_doc_repo, None)


def test_router_extract_bad_pdf_400(client):
    r = client.post("/api/berthing/extract",
                    files={"file": ("x.pdf", b"not-a-pdf", "application/pdf")})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] in ("unreadable_pdf", "could_not_detect_terminal")


@pytest.mark.skipif(not (_DATA_DIR / _TERMS["APMT"] / _REP["APMT"]).exists(),
                    reason="JNPA Berthing Reports data folder not present")
def test_router_extract_and_import_real(client):
    content = _read("APMT", _REP["APMT"])
    r = client.post("/api/berthing/extract",
                    files={"file": (_REP["APMT"], content, "application/pdf")})
    assert r.status_code == 200
    j = r.json()
    assert j["terminal"] == "APMT" and j["table_count"] >= 8 and j["total_rows"] > 0
    # import twice with the SAME dependency-override repo → IMPORTED then SKIPPED_DUPLICATE.
    from gateway.main import app
    from gateway.routers import berthing as router
    repo = FakeDocRepo()
    app.dependency_overrides[router.get_doc_repo] = lambda: repo
    a = client.post("/api/berthing/extract/import", files={"file": (_REP["APMT"], content, "application/pdf")})
    b = client.post("/api/berthing/extract/import", files={"file": (_REP["APMT"], content, "application/pdf")})
    assert a.json()["status"] == "IMPORTED" and b.json()["status"] == "SKIPPED_DUPLICATE"


# ------------------------------------------------------------------ real-file (opt-in)
@pytest.mark.skipif(not _DATA_DIR.exists(), reason="JNPA Berthing Reports data folder not present")
@pytest.mark.parametrize("term", list(_TERMS))
def test_core_panels_present_and_uncontaminated(term):
    res = FE.extract_tables(_read(term, _REP[term]), _REP[term])
    assert res["terminal"] == term
    names = {t["table_name"] for t in res["tables"]}
    missing = _CORE[term] - names
    assert not missing, f"{term}: core panels absent entirely: {missing}"
    structured = {t["table_name"] for t in res["tables"] if t["extraction_note"] != "section_not_found"}
    assert not (_CORE[term] - structured), f"{term}: core panels not structured: {_CORE[term] - structured}"
    # vessel tables must not contain ICD/CFS tokens (interleave separation)
    for t in res["tables"]:
        if t["table_name"] in ("VESSELS_EXPECTED", "VESSELS_ON_BERTH", "VESSELS_ON_BERTHED"):
            blob = " ".join(str(v) for row in t["rows"] for v in row.values())
            assert "CFS" not in blob and "MVS" not in blob, f"{term}/{t['table_name']} contaminated"


@pytest.mark.skipif(not _DATA_DIR.exists(), reason="JNPA Berthing Reports data folder not present")
def test_all_25_pdfs_extract_with_coverage():
    total = 0
    for term, folder in _TERMS.items():
        d = _DATA_DIR / folder
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".pdf"):
                continue
            total += 1
            res = FE.extract_tables((d / fn).read_bytes(), fn)
            assert res["terminal"] == term
            assert res["table_count"] >= 6, f"{fn}: too few tables ({res['table_count']})"
            assert isinstance(res["uncaptured_lines"], int)   # coverage tracked, never crashes
            assert res["total_rows"] > 0
    assert total == 25, f"expected 25 PDFs, saw {total}"


@pytest.mark.skipif(not (_DATA_DIR / _TERMS["NSICT"] / _REP["NSICT"]).exists(),
                    reason="JNPA Berthing Reports data folder not present")
def test_extraction_is_deterministic():
    content = _read("NSICT", _REP["NSICT"])
    a = FE.extract_tables(content, "x.pdf")
    b = FE.extract_tables(content, "x.pdf")
    assert [t["row_count"] for t in a["tables"]] == [t["row_count"] for t in b["tables"]]
