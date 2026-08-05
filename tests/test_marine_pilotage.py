"""Marine Pilotage slice tests — three tiers (parser / persist / read).

Tier 1 (pure): parse Pilot_card_data.xlsx → normalized records, always run when the
client file is present. Tier 2+3 (DB): persist through the shared framework and read
back via PilotageRepository; skipped when Postgres is unreachable.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from pathlib import Path

import pytest

from services.marine.parsers import detect_format, parse_marine

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "3- Port Craft & Pilot"),
))
XLSX = DATA_DIR / "Pilot_card_data.xlsx"
_HAVE_XLSX = XLSX.is_file()

pytestmark = pytest.mark.skipif(not _HAVE_XLSX, reason=f"pilot card absent: {XLSX}")


def _parsed():
    return parse_marine(XLSX.read_bytes(), "Pilot_card_data.xlsx")


# ---------------------------------------------------------------- Tier 1 — parser
class TestPilotCardParser:
    def test_detected_as_xlsx(self):
        assert detect_format("Pilot_card_data.xlsx", XLSX.read_bytes()) == "XLSX"
        # magic-byte detection even without the extension
        assert detect_format("blob", XLSX.read_bytes()) == "XLSX"

    def test_yields_pilotage_and_pilot_records(self):
        res = _parsed()
        assert not res.rejected and not res.errors
        pilotages = [r for r in res.records if r["_target"] == "pilotage"]
        pilots = [r for r in res.records if r["_target"] == "pilot"]
        assert len(pilotages) > 300 and len(pilots) > 0

    def test_sheet_maps_to_movement_type(self):
        mv = {r["movement_type"] for r in _parsed().records if r["_target"] == "pilotage"}
        assert mv == {"INWARD", "OUTWARD", "SHIFTING"}

    def test_identity_fields_are_clean_text(self):
        pg = [r for r in _parsed().records if r["_target"] == "pilotage"]
        s = next(r for r in pg if r.get("imo_no"))
        assert isinstance(s["imo_no"], str) and "." not in s["imo_no"]  # 9974292, not 9974292.0
        assert isinstance(s["movement_type"], str)

    def test_every_pilotage_row_has_a_hash(self):
        pg = [r for r in _parsed().records if r["_target"] == "pilotage"]
        assert all(len(r.get("row_sha256", "")) == 64 for r in pg)
        # hashes are unique within the file (in-file dedup already applied)
        hashes = [r["row_sha256"] for r in pg]
        assert len(hashes) == len(set(hashes))

    def test_out_of_range_draft_is_nulled_not_overflowed(self):
        # Regression: a dirty source draft (forward_draft=5075.0, OUTWARD/CAPE SYROS)
        # overflows numeric(5,2). It must be NULLed (raw kept in extras), the row kept,
        # and a typed warning emitted — never remapped, never allowed through.
        res = _parsed()
        pg = [r for r in res.records if r["_target"] == "pilotage"]
        for r in pg:
            for f in ("draft_fwd_m", "draft_aft_m"):
                assert r[f] is None or 0 <= r[f] <= 99.99, f"draft {f}={r[f]} would overflow numeric(5,2)"
        warns = [w for w in res.warnings if w["error_code"] == "draft_out_of_range"]
        assert warns, "the 5075.0 draft was not flagged"
        rescued = [r for r in pg if r.get("extras", {}).get("raw_draft_fwd_m") == 5075.0]
        assert rescued and rescued[0]["draft_fwd_m"] is None, "raw draft not preserved in extras"

    def test_unconsumed_columns_land_in_extras(self):
        pg = [r for r in _parsed().records if r["_target"] == "pilotage"]
        s = next(r for r in pg if r.get("extras"))
        assert "loa" in s["extras"] or "grt" in s["extras"]  # kept, not dropped

    def test_pilots_derived_from_pilot_ids(self):
        res = _parsed()
        pilots = {r["pilot_code"] for r in res.records if r["_target"] == "pilot"}
        used = {r["pilot_code"] for r in res.records
                if r["_target"] == "pilotage" and r.get("pilot_code")}
        assert used <= pilots  # every referenced pilot has a roster record


# ---------------------------------------------------------------- Tier 2+3 — DB
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
class TestPilotagePersistAndRead:
    async def _prepare(self):
        from gateway.marine_ext import ensure_marine_schema
        from jnpa_shared.db import get_engine
        from sqlalchemy import text
        await ensure_marine_schema(_DSN)
        async with get_engine(_DSN).begin() as conn:
            await conn.execute(text("TRUNCATE core.pilotage RESTART IDENTITY CASCADE"))
            await conn.execute(text("TRUNCATE core.pilot CASCADE"))
            await conn.execute(text("TRUNCATE core.marine_import_files RESTART IDENTITY CASCADE"))

    def test_import_persists_pilotage_and_is_idempotent(self):
        async def run():
            await self._prepare()
            from services.marine.upload_service import MarineUploadService
            from services.marine.pilotage import PilotageRepository
            svc = MarineUploadService(_DSN)
            content = XLSX.read_bytes()

            r1 = await svc.import_file(content, "Pilot_card_data.xlsx", "dev")
            assert r1["status"] in ("SUCCESS", "PARTIAL")
            assert r1["imported"] > 300
            assert r1["document_type"] == "PILOTAGE"

            repo = PilotageRepository(_DSN)
            n = await repo.count({})
            assert n > 300
            stats = await repo.stats({})
            assert {m["movement_type"] for m in stats["by_movement"]} == {"INWARD", "OUTWARD", "SHIFTING"}
            assert stats["pilots"] > 0

            # Re-upload identical bytes → file-level dedup (SKIPPED_DUPLICATE), no growth
            r2 = await svc.import_file(content, "Pilot_card_data.xlsx", "dev")
            assert r2["status"] == "SKIPPED_DUPLICATE"
            assert await repo.count({}) == n

            # A byte-different copy re-imported → row-hash dedup keeps the count stable
            r3 = await svc.import_file(content + b" ", "Pilot_card_data_copy.xlsx", "dev")
            assert await repo.count({}) == n, "row_sha256 dedup failed"
        _run(run)

    def test_read_endpoint_shape(self):
        async def run():
            await self._prepare()
            from services.marine.upload_service import MarineUploadService
            from services.marine.pilotage import PilotageService
            await MarineUploadService(_DSN).import_file(XLSX.read_bytes(), "Pilot_card_data.xlsx", "dev")
            page = await PilotageService(_DSN).list_pilotage(
                {"movement_type": "INWARD"}, sort="submitted_at", direction="desc", limit=10, offset=0)
            assert page["total"] > 0 and page["count"] <= 10
            assert all(r["movement_type"] == "INWARD" for r in page["items"])
        _run(run)
