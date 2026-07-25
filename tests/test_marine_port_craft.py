"""Marine Port-Craft slice tests — parser (pure) + persist/read (DB-gated).

Tier 1 parses Details_of_Port_Crafts.pdf → normalized records (always run when the
client file + pdfplumber are present). Tier 2 persists through the shared framework and
reads back via PortCraftRepository; skipped when Postgres is unreachable.
"""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest

from services.marine.parsers import detect_format, parse_marine

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "3- Port Craft & Pilot")))
PDF = DATA_DIR / "Details_of_Port_Crafts.pdf"


def _have_pdf() -> bool:
    if not PDF.is_file():
        return False
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _have_pdf(), reason=f"pdf/pdfplumber absent: {PDF}")


def _parsed():
    return parse_marine(PDF.read_bytes(), "Details_of_Port_Crafts.pdf")


# ---------------------------------------------------------------- Tier 1 — parser
class TestPortCraftParser:
    def test_detected_as_pdf(self):
        assert detect_format("Details_of_Port_Crafts.pdf", PDF.read_bytes()) == "PDF"
        assert detect_format("blob", PDF.read_bytes()) == "PDF"  # %PDF magic

    def test_parses_all_18_crafts(self):
        res = _parsed()
        assert not res.rejected
        crafts = [r for r in res.records if r["_target"] == "port_craft"]
        assert len(crafts) == 18
        assert len({c["name"] for c in crafts}) == 18  # names distinct (the key)

    def test_type_and_ownership_split(self):
        crafts = [r for r in _parsed().records if r["_target"] == "port_craft"]
        assert sum(1 for c in crafts if c["craft_type"] == "Tug") == 10
        assert sum(1 for c in crafts if c["owned_or_hired"] == "Owned") == 1
        assert sum(1 for c in crafts if c["owned_or_hired"] == "Hired") == 17

    def test_numeric_particulars_and_engines(self):
        crafts = [r for r in _parsed().records if r["_target"] == "port_craft"]
        div = next(c for c in crafts if c["name"] == "Ocean Divine")
        assert div["loa_m"] == 30.31 and div["breadth_m"] == 12.0 and div["draft_m"] == 4.30
        assert div["bollard_pull_t"] == 50.0 and div["design_speed_kn"] == 12.0
        assert "NIGATA" in div["main_engines"]  # wrapped engine fragment recovered

    def test_owned_craft_parsed(self):
        crafts = [r for r in _parsed().records if r["_target"] == "port_craft"]
        chetak = next(c for c in crafts if c["name"] == "M.L Chetak")
        assert chetak["owned_or_hired"] == "Owned" and chetak["owner_name"] == "JNPA"
        assert chetak["bollard_pull_t"] is None and chetak["design_speed_kn"] == 20.0

    def test_raw_preserved_in_extras(self):
        crafts = [r for r in _parsed().records if r["_target"] == "port_craft"]
        assert all(c["extras"].get("raw") for c in crafts)  # never drop client data

    def test_engine_fragment_never_leaks_into_name(self):
        crafts = [r for r in _parsed().records if r["_target"] == "port_craft"]
        assert not any("NIGATA" in c["name"] or "," in c["name"] for c in crafts)


# ---------------------------------------------------------------- Tier 2 — DB
_DSN = os.environ.get("MARINE_TEST_DSN", os.environ.get("POSTGRES_DSN", ""))


def _pg_reachable() -> bool:
    if not _DSN or "asyncpg" not in _DSN:
        return False
    try:
        hp = _DSN.split("@", 1)[1].split("/", 1)[0]
        host, _, port = hp.partition(":")
        with socket.create_connection((host, int(port or "5432")), timeout=1.5):
            return True
    except Exception:
        return False


def _run(run):
    async def _wrapped():
        from jnpa_shared.db import dispose_all
        try:
            await run()
        finally:
            await dispose_all()
    asyncio.run(_wrapped())


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres unreachable")
class TestPortCraftPersistAndRead:
    async def _prepare(self):
        from gateway.marine_ext import ensure_marine_schema
        from jnpa_shared.db import get_engine
        from sqlalchemy import text
        await ensure_marine_schema(_DSN)
        async with get_engine(_DSN).begin() as conn:
            await conn.execute(text("TRUNCATE core.port_craft RESTART IDENTITY CASCADE"))
            await conn.execute(text("TRUNCATE core.marine_import_files RESTART IDENTITY CASCADE"))

    def test_import_persists_18_and_is_idempotent(self):
        async def run():
            await self._prepare()
            from services.marine.upload_service import MarineUploadService
            from services.marine.port_craft import PortCraftRepository
            svc = MarineUploadService(_DSN)
            content = PDF.read_bytes()

            r1 = await svc.import_file(content, "Details_of_Port_Crafts.pdf", "dev")
            assert r1["status"] in ("SUCCESS", "PARTIAL")
            assert r1["imported"] == 18
            assert r1["document_type"] == "PORT_CRAFT"

            repo = PortCraftRepository(_DSN)
            assert await repo.count({}) == 18
            stats = await repo.stats({})
            assert stats["total"] == 18
            assert any(t["craft_type"] == "Tug" and t["count"] == 10 for t in stats["by_type"])

            # Identical bytes → file-level dedup, no growth
            r2 = await svc.import_file(content, "Details_of_Port_Crafts.pdf", "dev")
            assert r2["status"] == "SKIPPED_DUPLICATE"
            assert await repo.count({}) == 18

            # Byte-different copy → upsert on `name`, count stays 18 (0 updated rows added)
            r3 = await svc.import_file(content + b" ", "copy.pdf", "dev")
            assert await repo.count({}) == 18, "name upsert failed — duplicate crafts"
            assert r3["updated"] == 18 and r3["imported"] == 0
        _run(run)

    def test_read_endpoint_shape(self):
        async def run():
            await self._prepare()
            from services.marine.upload_service import MarineUploadService
            from services.marine.port_craft import PortCraftService
            await MarineUploadService(_DSN).import_file(PDF.read_bytes(), "Details_of_Port_Crafts.pdf", "dev")
            page = await PortCraftService(_DSN).list_craft(
                {"craft_type": "Tug"}, sort="name", direction="asc", limit=50, offset=0)
            assert page["total"] == 10
            assert all(c["craft_type"] == "Tug" for c in page["items"])
        _run(run)
