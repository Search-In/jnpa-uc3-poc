"""ICEGATE Customs Adapter tests (Phase 4).

Pure tests for the feature flag; a DB+data-gated integration test that imports the
real customer files, runs the adapter, and asserts real ICEGATE captures land in
core.gate_capture in the UNCHANGED GateCapture shape — with the ICEGATE provider
badge flipping to LIVE while e-Seal / Form-13 / Weighbridge stay SIM and untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from pathlib import Path

import pytest

DATA_DIR = Path(os.environ.get(
    "CUSTOMS_DATA_DIR", os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs")))

# GateCapture DTO fields the UI/web contract depends on (web/src/lib/types.ts).
_GATECAPTURE_KEYS = {
    "id", "capture_type", "container_no", "vehicle_plate", "gate_id",
    "source_mode", "status", "captured_at", "payload", "created_at",
}


def _pg_reachable(host: str = "127.0.0.1", port: int = 5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_DSN = os.environ.get(
    "CUSTOMS_TEST_DSN",
    os.environ.get("POSTGRES_DSN") if "127.0.0.1:1" not in os.environ.get("POSTGRES_DSN", "")
    else None) or "postgresql+asyncpg://postgres:TempPass123!@127.0.0.1:5433/postgres"


def test_flag_parsing():
    from gate_data import customs_adapter
    for val in ("customs", "1", "true", "on", "LIVE", "Yes"):
        os.environ["GATE_ICEGATE_ADAPTER"] = val
        assert customs_adapter.enabled() is True, val
    for val in ("", "sim", "off", "0", "false"):
        os.environ["GATE_ICEGATE_ADAPTER"] = val
        assert customs_adapter.enabled() is False, val
    os.environ.pop("GATE_ICEGATE_ADAPTER", None)
    assert customs_adapter.enabled() is False


def test_provider_mode_off_by_default():
    """Flag unset → ICEGATE stays SIM (the default, zero-regression behaviour)."""
    from gate_data import providers
    os.environ.pop("GATE_ICEGATE_ADAPTER", None)
    modes = {k: v["mode"] for k, v in providers.providers_status().items()}
    assert modes == {"ESEAL": "sim", "FORM13": "sim", "WEIGHBRIDGE": "sim", "ICEGATE": "sim"}


def test_provider_mode_live_when_enabled():
    from gate_data import providers
    os.environ["GATE_ICEGATE_ADAPTER"] = "customs"
    try:
        st = providers.providers_status()
        assert st["ICEGATE"]["mode"] == "live"
        assert st["ICEGATE"]["adapter"] == "customs"
        # Other sources are unaffected.
        assert st["ESEAL"]["mode"] == "sim"
        assert st["FORM13"]["mode"] == "sim"
        assert st["WEIGHBRIDGE"]["mode"] == "sim"
    finally:
        os.environ.pop("GATE_ICEGATE_ADAPTER", None)


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
@pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}")
def test_adapter_produces_real_icegate_captures():
    async def run() -> None:
        from sqlalchemy import text

        from gateway.customs_ext import ensure_customs_schema
        from gate_data import customs_adapter, persistence
        from jnpa_shared.db import get_engine
        from services.customs.repository import CustomsRepository
        from services.customs.parsers import (
            parse_chpoi03, parse_chpoi10, parse_chpoi13, parse_rms_txt,
            parse_leo_xlsx, parse_shipping_bill_xlsx)

        await ensure_customs_schema(_DSN)
        await persistence.ensure_gate_schema(_DSN)
        # Clean slate for both the customs docs and the ICEGATE gate captures.
        async with get_engine(_DSN).begin() as conn:
            await conn.execute(text("TRUNCATE core.customs_message RESTART IDENTITY CASCADE"))
            await conn.execute(text("DELETE FROM core.gate_capture WHERE capture_type='ICEGATE'"))
            # Seed one synthetic ESEAL row to prove the adapter never touches it.
            await conn.execute(text(
                "INSERT INTO core.gate_capture (capture_type, container_no, source_mode, "
                "status, captured_at) VALUES ('ESEAL','TESTESEAL0','sim','ARMED', now()) "
                "ON CONFLICT DO NOTHING"))

        # Import the real customer files.
        repo = CustomsRepository(_DSN)
        parsers = {"IGM": (parse_chpoi03, "CHPOI03_*.xml"), "OOC": (parse_chpoi10, "CHPOI10_*.xml"),
                   "SMTP": (parse_chpoi13, "CHPOI13_*.xml"), "RMS": (parse_rms_txt, "*.txt")}
        for sub, (parser, glob) in parsers.items():
            for f in sorted((DATA_DIR / sub).glob(glob)):
                await repo.persist(parser(str(f)), source_file=f.name,
                                   source_sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                                   file_size=f.stat().st_size)

        # Run the adapter.
        inserted = await customs_adapter.sync_icegate_captures(_DSN)
        assert inserted > 0

        rows = await persistence.recent_captures(
            capture_type="ICEGATE", container_no=None, limit=50, dsn=_DSN)
        assert rows, "adapter produced no ICEGATE captures"
        for r in rows:
            assert _GATECAPTURE_KEYS.issubset(r.keys())        # UNCHANGED DTO shape
            assert r["capture_type"] == "ICEGATE"
            assert r["source_mode"] == "live"                   # real source
            assert r["status"] in ("GRANTED", "PENDING")
            assert r["payload"].get("source") == "ICEGATE"      # payload shape fidelity
            assert "igm_no" in r["payload"]

        # Idempotent: a second run inserts nothing.
        assert await customs_adapter.sync_icegate_captures(_DSN) == 0

        # The adapter touched ONLY ICEGATE — the ESEAL row is intact.
        async with get_engine(_DSN).connect() as conn:
            eseal = (await conn.execute(text(
                "SELECT count(*) FROM core.gate_capture WHERE capture_type='ESEAL'"))).scalar()
        assert int(eseal) >= 1

        # --- Symmetric rollback: purge_live removes ALL live ICEGATE rows -------------
        async def live_count() -> int:
            async with get_engine(_DSN).connect() as conn:
                return int((await conn.execute(text(
                    "SELECT count(*) FROM core.gate_capture "
                    "WHERE capture_type='ICEGATE' AND source_mode='live'"))).scalar())

        assert await live_count() > 0
        removed = await customs_adapter.purge_live_icegate(_DSN)
        assert removed > 0
        assert await live_count() == 0                     # no stale LIVE rows remain
        # ESEAL still intact after rollback (rollback touches only ICEGATE/live).
        async with get_engine(_DSN).connect() as conn:
            eseal2 = (await conn.execute(text(
                "SELECT count(*) FROM core.gate_capture WHERE capture_type='ESEAL'"))).scalar()
        assert int(eseal2) == int(eseal)

    from jnpa_shared.db import dispose_all

    async def wrapped() -> None:
        # Drop any engine cached by a prior test on a now-closed event loop (e.g. a
        # TestClient app lifespan), so this test's first DB call binds a fresh engine
        # to THIS loop. Dispose again at the end for the next test.
        await dispose_all()
        try:
            await run()
        finally:
            await dispose_all()
    asyncio.run(wrapped())
