"""Customs repository integration tests — real Postgres, real customer files.

Opt-in: skipped unless Postgres is reachable on 5433 AND the customer data dir is
present. The test OWNS the customs schema state during its run (it truncates the
jnpa.customs_* tables to a clean slate), so run it against a dev DB.

Covers: atomic + idempotent import of every official file, honest imported-vs-record
accounting (the Shipping Bill sheet's duplicate rows collapse to 15), and the
FAILED-rollback path (a bad row leaves no partial data + records a ledger error).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from pathlib import Path

import pytest

from services.customs.parsers import (
    parse_chpoi03,
    parse_chpoi10,
    parse_chpoi13,
    parse_leo_xlsx,
    parse_rms_txt,
    parse_shipping_bill_xlsx,
)
from services.customs.parsers.common import ParsedMessage
from services.customs.repository import CustomsRepository

DATA_DIR = Path(os.environ.get(
    "CUSTOMS_DATA_DIR", os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs")))


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

pytestmark = [
    pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433"),
    pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}"),
]


def _run_isolated(run) -> None:
    """Run an async test body on a fresh event loop, disposing the cached async
    engines afterwards so the NEXT test's ``asyncio.run`` doesn't reuse a connection
    bound to this now-closed loop (the SQLAlchemy engine is process-cached)."""
    async def _wrapped() -> None:
        from jnpa_shared.db import dispose_all
        try:
            await run()
        finally:
            await dispose_all()
    asyncio.run(_wrapped())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_files() -> list[tuple[Path, object]]:
    files: list[tuple[Path, object]] = []
    files += [(f, parse_chpoi03) for f in sorted((DATA_DIR / "IGM").glob("CHPOI03_*.xml"))]
    files += [(f, parse_chpoi10) for f in sorted((DATA_DIR / "OOC").glob("CHPOI10_*.xml"))]
    files += [(f, parse_chpoi13) for f in sorted((DATA_DIR / "SMTP").glob("CHPOI13_*.xml"))]
    files += [(f, parse_rms_txt) for f in sorted((DATA_DIR / "RMS").glob("*.txt"))]
    files += [(DATA_DIR / "LEO" / "leodetails.xlsx", parse_leo_xlsx)]
    files += [(DATA_DIR / "Shipping Bill" / "shippingbill.xlsx", parse_shipping_bill_xlsx)]
    return files


async def _truncate(dsn: str) -> None:
    from sqlalchemy import text

    from jnpa_shared.db import get_engine
    async with get_engine(dsn).begin() as conn:
        await conn.execute(text("TRUNCATE jnpa.customs_messages RESTART IDENTITY CASCADE"))


async def _table_count(dsn: str, table: str) -> int:
    from sqlalchemy import text

    from jnpa_shared.db import get_engine
    async with get_engine(dsn).connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM jnpa.{table}"))).scalar() or 0)


def test_import_all_files_idempotent_and_accurate():
    async def run() -> None:
        from gateway.customs_ext import ensure_customs_schema
        await ensure_customs_schema(_DSN)
        await _truncate(_DSN)
        repo = CustomsRepository(_DSN)
        files = _all_files()

        # First import: every file SUCCEEDS.
        results = {}
        for path, parser in files:
            pm = parser(str(path))
            r = await repo.persist(pm, source_file=path.name,
                                   source_sha256=_sha(path), file_size=path.stat().st_size)
            assert r["import_status"] == "SUCCESS", (path.name, r)
            results[path.name] = r

        # Honest accounting: most files import every record; the Shipping Bill sheet
        # has 100 rows but only 15 distinct SB numbers -> 15 imported.
        sb = results["shippingbill.xlsx"]
        assert sb["record_count"] == 100 and sb["imported_count"] == 15
        leo = results["leodetails.xlsx"]
        assert leo["record_count"] == 100 and leo["imported_count"] == 100
        for name, r in results.items():
            if name not in ("shippingbill.xlsx",):
                assert r["imported_count"] == r["record_count"], (name, r)

        # DB row counts match the parsed leaf totals exactly.
        assert await _table_count(_DSN, "customs_igm_container") == 4357
        assert await _table_count(_DSN, "customs_smtp_line") == 209
        assert await _table_count(_DSN, "customs_rms_container") == 98
        assert await _table_count(_DSN, "customs_leo") == 100
        assert await _table_count(_DSN, "customs_shipping_bill") == 15
        assert await _table_count(_DSN, "customs_messages") == len(files)

        # Idempotent re-import: every file SKIPPED_DUPLICATE, no row growth.
        before = await _table_count(_DSN, "customs_igm_container")
        for path, parser in files:
            r = await repo.persist(parser(str(path)), source_file=path.name,
                                   source_sha256=_sha(path), file_size=path.stat().st_size)
            assert r["import_status"] == "SKIPPED_DUPLICATE" and r["duplicate"] is True
        assert await _table_count(_DSN, "customs_igm_container") == before
        assert await _table_count(_DSN, "customs_messages") == len(files)

    _run_isolated(run)


def test_reconcile_binds_customs_docs_to_cargo():
    """After importing the real files, an OOC container present in jnpa.cargo becomes
    CLEARED and an RMS-selected container becomes UNDER_INSPECTION; a scan-hold
    notification lands on the existing cargo feed; re-running changes nothing."""
    from sqlalchemy import text

    from gateway.customs_ext import ensure_customs_schema
    from jnpa_shared.db import get_engine
    from services.customs.service import CustomsService

    OOC_CN, RMS_CN = "EOLU8617280", "BWLU9101815"  # from OOC 9352934 / RMS 1191409

    async def run() -> None:
        await ensure_customs_schema(_DSN)
        await _truncate(_DSN)
        repo = CustomsRepository(_DSN)
        for path, parser in _all_files():
            await repo.persist(parser(str(path)), source_file=path.name,
                               source_sha256=_sha(path), file_size=path.stat().st_size)
        # Seed two cargo rows that customs has facts about.
        async with get_engine(_DSN).begin() as conn:
            for cn in (OOC_CN, RMS_CN):
                await conn.execute(text(
                    "INSERT INTO jnpa.cargo (container_number, customs_status) VALUES (:cn,'PENDING') "
                    "ON CONFLICT (container_number) DO UPDATE SET customs_status='PENDING'"), {"cn": cn})
        try:
            svc = CustomsService(_DSN)
            r1 = await svc.reconcile_cargo()
            assert r1["cleared"] >= 1 and r1["under_inspection"] >= 1
            async with get_engine(_DSN).connect() as conn:
                async def stat(cn: str) -> str:
                    return (await conn.execute(text(
                        "SELECT customs_status FROM jnpa.cargo WHERE container_number=:cn"),
                        {"cn": cn})).scalar()
                assert await stat(OOC_CN) == "CLEARED"
                assert await stat(RMS_CN) == "UNDER_INSPECTION"
                notif = (await conn.execute(text(
                    "SELECT count(*) FROM jnpa.cargo_notifications WHERE container_number=:cn "
                    "AND notification_type='CUSTOMS_SCAN_REQUIRED'"), {"cn": RMS_CN})).scalar()
                assert int(notif) >= 1
            # Idempotent: a second reconcile moves nothing.
            r2 = await svc.reconcile_cargo()
            assert r2["cleared"] == 0 and r2["under_inspection"] == 0
        finally:
            async with get_engine(_DSN).begin() as conn:
                await conn.execute(text("DELETE FROM jnpa.cargo WHERE container_number = ANY(:ids)"),
                                   {"ids": [OOC_CN, RMS_CN]})
                await conn.execute(text(
                    "DELETE FROM jnpa.cargo_notifications WHERE container_number = ANY(:ids)"),
                    {"ids": [OOC_CN, RMS_CN]})

    _run_isolated(run)


def test_failed_import_rolls_back_and_records_ledger():
    async def run() -> None:
        from gateway.customs_ext import ensure_customs_schema
        await ensure_customs_schema(_DSN)
        await _truncate(_DSN)
        repo = CustomsRepository(_DSN)

        before_vessels = await _table_count(_DSN, "customs_igm_vessel")
        # A structurally-broken IGM: a vessel with NULL igm_no violates NOT NULL, so
        # the whole transaction must roll back (no partial vessel/line/container rows).
        bad = ParsedMessage(
            message={"message_type": "CHPOI03", "module": "IGM", "primary_ref": None},
            payload={"vessels": [{"igm_no": None, "igm_date": None,
                                  "lines": [{"line_no": 1, "subline_no": 0,
                                             "containers": [{"container_no": "TESU1234567",
                                                             "iso_valid": False}]}]}]},
            record_count=1)
        r = await repo.persist(bad, source_file="broken.xml",
                               source_sha256="deadbeef" * 8, file_size=1)
        assert r["import_status"] == "FAILED"
        assert r["message_id"] is not None      # a FAILED ledger row was recorded
        assert r["imported_count"] == 0
        # Rollback: NO domain rows persisted for the failed message.
        assert await _table_count(_DSN, "customs_igm_vessel") == before_vessels
        assert await _table_count(_DSN, "customs_import_errors") >= 1

    _run_isolated(run)
