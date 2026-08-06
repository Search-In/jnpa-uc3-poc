"""Marine import pipeline tests (Phase 2) — three tiers:

  1. PURE unit tests — record routing, document_type, preview, resolution keys.
     No DB, always run.
  2. REAL CLIENT FIXTURE tests — parse_marine over client-data/1-NLP Marine and
     assert the normalized records that the persistence layer will route. Skipped
     when the corpus is absent.
  3. DB INTEGRATION tests — end-to-end persist against a live Postgres: VESPRO →
     vessel, CALINF pre-VCN seed, BERMAN promotion, VESARR/VESDEP events, plus the
     reject-if-unresolved path. Skipped when Postgres is unreachable.

Nothing here modifies API contracts; it exercises services.marine only.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from pathlib import Path

import pytest

from services.marine.parsers import parse_marine
from services.marine.upload_service import (
    _document_type,
    _build_preview,
    _physical_format,
)

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine"),
))
_HAVE_DATA = DATA_DIR.is_dir()


def _files(sub: str, pattern: str) -> list[Path]:
    return sorted((DATA_DIR / sub).glob(pattern))


def _first(sub: str, pattern: str) -> bytes:
    return _files(sub, pattern)[0].read_bytes()


# ============================================================ TIER 1 — pure unit tests
class TestRoutingUnit:
    def test_document_type_single_and_mixed(self):
        r = parse_marine(_maybe(), "x.xml") if False else None
        # build a tiny synthetic ParseResult via parse_marine on inline XML
        vespro = b"<VesselProfile><DocumentType>VESPRO</DocumentType>" \
                 b"<VesselProfileDetails><IMONumber>9000001</IMONumber></VesselProfileDetails></VesselProfile>"
        res = parse_marine(vespro, "v.xml")
        assert _document_type(res) == "VESPRO"

    def test_document_type_none_when_empty(self):
        res = parse_marine(b"<Nope><DocumentType>ZZZ</DocumentType></Nope>", "z.xml")
        assert res.records == []
        assert _document_type(res) is None

    def test_physical_format_detection(self):
        assert _physical_format("a.csv", b"VCN\n1\n") == "CSV"
        assert _physical_format("a.xml", b"<VesselProfile/>") == "XML"
        assert _physical_format("a.log", b'{"ReqBody":{"XML":"<VesselArrival/>"}}') == "LOG"

    def test_preview_summarizes_records(self):
        berman = (b"<BerthManagement><DocumentType>BERMAN</DocumentType>"
                  b"<BERMANHeader><VCN>INNSA1NS0R2893</VCN><IMONumber>9339856</IMONumber>"
                  b"<VoyageNumber>IM2603W</VoyageNumber></BERMANHeader></BerthManagement>")
        res = parse_marine(berman, "b.xml")
        pv = _build_preview(res)
        assert pv and pv[0]["Type"] == "BERMAN" and pv[0]["VCN"] == "INNSA1NS0R2893"

    def test_unknown_document_is_typed_error_not_crash(self):
        res = parse_marine(b"<Weird><DocumentType>FOO</DocumentType></Weird>", "w.xml")
        assert any(e["error_code"] == "unsupported_message_type" for e in res.errors)
        assert not res.records


def _maybe():  # pragma: no cover - guard for the disabled branch above
    return b""


# ============================================================ TIER 2 — real fixtures
@pytest.mark.skipif(not _HAVE_DATA, reason=f"marine client data absent: {DATA_DIR}")
class TestRealFixtures:
    def test_vespro_yields_vessel_record(self):
        res = parse_marine(_first("VESPRO", "*.xml"), "vespro.xml")
        vs = [r for r in res.records if r["_target"] == "vessel"]
        assert len(vs) == 1 and vs[0]["imo_no"].isdigit()

    def test_calinf_yields_pre_vcn_call(self):
        res = parse_marine(_first("CALINF", "*.xml"), "calinf.xml")
        cs = [r for r in res.records if r["_target"] == "vessel_call"]
        assert len(cs) == 1 and cs[0]["vcn"] is None and cs[0]["voyage_no"]

    def test_berman_yields_vcn_call(self):
        res = parse_marine(_first("BERMAN", "*.xml"), "berman.xml")
        cs = [r for r in res.records if r["_target"] == "vessel_call"]
        assert len(cs) == 1 and cs[0]["vcn"].startswith("INNSA")

    def test_vesarr_yields_events_with_resolution_keys(self):
        res = parse_marine(_first("VESARR", "*.log"), "vesarr.log")
        evs = [r for r in res.records if r["_target"] == "vessel_call_event"]
        assert evs, "no VESARR events"
        assert all(e["vcn"] and e["event_type"] for e in evs)

    def test_vesdep_yields_departure_events(self):
        res = parse_marine(_first("VESDEP", "*.log"), "vesdep.log")
        evs = [r for r in res.records if r["_target"] == "vessel_call_event"]
        assert all(e["event_type"] in {"PILOT_BOARDED", "DEPARTED", "SAILED"} for e in evs)


# ============================================================ TIER 3 — DB integration
_DSN = os.environ.get("MARINE_TEST_DSN", os.environ.get("POSTGRES_DSN", ""))


def _pg_reachable() -> bool:
    """True if a Postgres TCP port from the DSN accepts a connection quickly."""
    if not _DSN or "asyncpg" not in _DSN:
        return False
    try:
        hostport = _DSN.split("@", 1)[1].split("/", 1)[0]
        host, _, port = hostport.partition(":")
        with socket.create_connection((host, int(port or "5432")), timeout=1.5):
            return True
    except Exception:
        return False


def _run_isolated(run) -> None:
    async def _wrapped() -> None:
        from jnpa_shared.db import dispose_all
        try:
            await run()
        finally:
            await dispose_all()
    asyncio.run(_wrapped())


pytestmark_db = pytest.mark.skipif(
    not (_pg_reachable() and _HAVE_DATA),
    reason="Postgres unreachable or marine data absent",
)


@pytestmark_db
class TestPersistIntegration:
    def _repo(self):
        from services.marine.repository import VesselCallRepository
        return VesselCallRepository(_DSN)

    async def _prepare(self):
        from gateway.marine_ext import ensure_marine_schema
        from jnpa_shared.db import get_engine
        from sqlalchemy import text
        await ensure_marine_schema(_DSN)
        async with get_engine(_DSN).begin() as conn:
            for t in ("vessel_call_event", "vessel_call", "vessel_insurance", "vessel",
                      "marine_import_errors", "marine_import_files"):
                await conn.execute(text(f"TRUNCATE core.{t} RESTART IDENTITY CASCADE"))

    def test_vespro_persists_vessel(self):
        async def run():
            await self._prepare()
            repo = self._repo()
            recs = parse_marine(_first("VESPRO", "*.xml"), "vespro.xml").records
            res = await repo.persist(recs, filename="vespro.xml",
                                     file_hash=hashlib.sha256(b"vespro").hexdigest(),
                                     physical_format="XML", document_type="VESPRO")
            assert res["status"] in ("SUCCESS", "PARTIAL")
            assert res["inserted"] >= 1
        _run_isolated(run)

    def test_calinf_then_berman_promotes_one_call(self):
        async def run():
            await self._prepare()
            repo = self._repo()
            # 1) CALINF seeds a pre-VCN call
            calinf = parse_marine(_first("CALINF", "*.xml"), "calinf.xml").records
            await repo.persist(calinf, filename="calinf.xml",
                               file_hash=hashlib.sha256(b"calinf").hexdigest(),
                               physical_format="XML", document_type="CALINF")
            # 2) A synthetic BERMAN for the SAME (imo, voyage) must PROMOTE, not duplicate
            c = calinf[0]
            berman_rec = dict(c)
            berman_rec.update(_target="vessel_call", _message="BERMAN", vcn="INNSA1TEST0001")
            await repo.persist([berman_rec], filename="berman.xml",
                               file_hash=hashlib.sha256(b"berman").hexdigest(),
                               physical_format="XML", document_type="BERMAN")
            from jnpa_shared.db import get_engine
            from sqlalchemy import text
            async with get_engine(_DSN).connect() as conn:
                n = (await conn.execute(text(
                    "SELECT count(*) FROM core.vessel_call WHERE voyage_no = :v"),
                    {"v": c["voyage_no"]})).scalar()
                promoted = (await conn.execute(text(
                    "SELECT vcn FROM core.vessel_call WHERE voyage_no = :v"),
                    {"v": c["voyage_no"]})).scalar()
            assert n == 1, "BERMAN promotion duplicated the call instead of enriching it"
            assert promoted == "INNSA1TEST0001", "VCN was not stamped onto the seed"
        _run_isolated(run)

    def test_unresolved_event_is_rejected_not_stubbed(self):
        async def run():
            await self._prepare()
            repo = self._repo()
            orphan = {"_target": "vessel_call_event", "_message": "VESARR",
                      "vcn": "INNSA1NOSUCH999", "via_no": "9999",
                      "event_type": "ANCHORED", "event_ts": None}
            import datetime as dt
            orphan["event_ts"] = dt.datetime(2026, 7, 29, 5, 18, tzinfo=dt.timezone.utc)
            res = await repo.persist([orphan], filename="v.log",
                                     file_hash=hashlib.sha256(b"orphan").hexdigest(),
                                     physical_format="LOG", document_type="VESARR")
            assert res["failed"] == 1
            from jnpa_shared.db import get_engine
            from sqlalchemy import text
            async with get_engine(_DSN).connect() as conn:
                calls = (await conn.execute(text("SELECT count(*) FROM core.vessel_call"))).scalar()
                events = (await conn.execute(text("SELECT count(*) FROM core.vessel_call_event"))).scalar()
            assert calls == 0, "an unresolved event created a stub call"
            assert events == 0, "an unresolved event was inserted with a null call"
        _run_isolated(run)


class TestOverrideReImport:
    """Override re-processes a file already in the ledger. It is NOT a delete.

    Static: reads the SQL and the signatures, so no database is needed. The behavioural
    proof is the live check in the session log (SKIPPED_DUPLICATE -> SUCCESS, same file_id).
    """

    def test_normal_import_still_short_circuits_on_a_duplicate(self):
        """The default path must be byte-for-byte unchanged."""
        import inspect
        from services.marine.repository import VesselCallRepository
        src = inspect.getsource(VesselCallRepository.persist)
        assert "if existing is not None and not override:" in src
        assert '"status": "SKIPPED_DUPLICATE"' in src

    def test_override_defaults_to_false_everywhere(self):
        """A caller that does not know about override gets the old behaviour."""
        import inspect
        from services.marine.repository import VesselCallRepository
        from services.marine.upload_service import MarineUploadService
        for fn in (VesselCallRepository.persist, MarineUploadService.import_file):
            assert inspect.signature(fn).parameters["override"].default is False

    def test_override_reuses_the_ledger_row_and_never_deletes_it(self):
        """file_hash is UNIQUE, so a re-import must reopen the SAME row — and the row,
        with its id, must survive so every import_file_id reference stays valid."""
        from services.marine import repository as R
        sql = " ".join(R._FILE_REOPEN.split())
        assert sql.upper().startswith("UPDATE CORE.MARINE_IMPORT_FILES")
        assert "WHERE id = :id" in sql
        assert "DELETE" not in sql.upper()
        assert "status = 'PENDING'" in sql

    def test_override_clears_only_the_previous_run_s_row_errors(self):
        """Scoped to this file — never a table-wide delete."""
        from services.marine import repository as R
        sql = " ".join(R._CLEAR_FILE_ERRORS.split())
        assert sql == "DELETE FROM core.marine_import_errors WHERE import_file_id = :id"

    def test_no_business_table_is_ever_truncated_or_deleted(self):
        """The whole point: override refreshes via UPSERT, it does not wipe anything.

        Matches SQL, not prose — the persist() docstring says the word "truncated" while
        explaining that nothing is, so a bare keyword scan would flag the explanation.
        """
        import re
        from pathlib import Path
        src = Path(R_PATH).read_text(encoding="utf-8")
        for pat, name in ((r"TRUNCATE\s+(?:TABLE\s+)?\w", "TRUNCATE"),
                          (r"DROP\s+TABLE\s+\w", "DROP TABLE")):
            assert not re.search(pat, src, re.I), f"import path must never {name}"
        # The only DELETE permitted is the scoped row-error clear above.
        deletes = re.findall(r"DELETE\s+FROM\s+core\.(\w+)", src, re.I)
        assert deletes == ["marine_import_errors"], f"unexpected DELETE targets: {deletes}"

    def test_failed_row_can_be_retried(self):
        """A FAILED ledger row used to block re-upload forever (its hash was taken).
        The failure insert now upserts on the hash instead.

        Target is (file_hash, data_origin), INFERRED: migration 0120 replaced the named
        constraint uq_marine_import_file_hash with the per-origin unique INDEX
        uq_marine_import_file_hash_origin, which has no constraint name to bind to.
        """
        from services.marine import repository as R
        sql = " ".join(R._FILE_INSERT_FAILED.split())
        assert "ON CONFLICT (file_hash, data_origin) DO UPDATE" in sql
        assert "ON CONFLICT ON CONSTRAINT" not in sql


R_PATH = __import__("pathlib").Path(__file__).resolve().parents[1] / \
    "services/marine/repository.py"
