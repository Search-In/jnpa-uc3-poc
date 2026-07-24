"""Marine Sea-Channel slice tests — parser (pure) + persist/read (DB-gated).

Tier 1 parses the JNPA_Sea_Channels shapefile (zipped from the client bundle) → GeoJSON
records, always run when the client files are present (zero GIS deps). Tier 2 persists
through the shared framework and reads back; skipped when Postgres is unreachable.
"""
from __future__ import annotations

import asyncio
import io
import os
import socket
import zipfile
from pathlib import Path

import pytest

from services.marine.parsers import detect_format, parse_marine

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data"
        / "2-JNPA_Sea_Channels_Bathymetry" / "Sea Channel")))
_BASE = DATA_DIR / "JNPA_Sea_Channels"
_HAVE = (_BASE.with_suffix(".shp")).is_file() and (_BASE.with_suffix(".dbf")).is_file()

pytestmark = pytest.mark.skipif(not _HAVE, reason=f"sea-channel shapefile absent: {DATA_DIR}")


def _zip_bundle() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for ext in (".shp", ".dbf", ".shx", ".prj"):
            p = _BASE.with_suffix(ext)
            if p.is_file():
                z.writestr("JNPA_Sea_Channels" + ext, p.read_bytes())
    return buf.getvalue()


def _parsed():
    return parse_marine(_zip_bundle(), "JNPA_Sea_Channels.zip")


# ---------------------------------------------------------------- Tier 1 — parser
class TestSeaChannelParser:
    def test_zip_detected_as_shp_not_xlsx(self):
        assert detect_format("JNPA_Sea_Channels.zip", _zip_bundle()) == "SHP"
        # bare .shp is also SHP; a real xlsx must remain XLSX (regression)
        assert detect_format("x.shp", _BASE.with_suffix(".shp").read_bytes()) == "SHP"

    def test_parses_all_50_polygons(self):
        res = _parsed()
        assert not res.rejected and not res.errors
        chans = [r for r in res.records if r["_target"] == "sea_channel"]
        assert len(chans) == 50

    def test_geometry_is_geojson_wgs84(self):
        chans = [r for r in _parsed().records if r["_target"] == "sea_channel"]
        for c in chans:
            g = c["geom_geojson"]
            assert g["type"] == "Polygon" and g["coordinates"]
            for ring in g["coordinates"]:
                for lon, lat in ring:
                    # Nhava Sheva WGS84 window — proves UTM43N→WGS84 reprojection ran
                    assert 72.5 < lon < 73.3 and 18.5 < lat < 19.3

    def test_attributes_mapped(self):
        chans = [r for r in _parsed().records if r["_target"] == "sea_channel"]
        assert any(c["name"] == "JNPA Channel" for c in chans)
        assert any(c["name"] == "MbPA Channel" for c in chans)
        witharea = [c for c in chans if c["area_ha"] is not None]
        assert witharea and all(c["length_m"] is not None for c in witharea)

    def test_every_record_hashed(self):
        chans = [r for r in _parsed().records if r["_target"] == "sea_channel"]
        assert all(len(c.get("row_sha256", "")) == 64 for c in chans)


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
class TestSeaChannelPersistAndRead:
    async def _prepare(self):
        from gateway.marine_ext import ensure_marine_schema
        from jnpa_shared.db import get_engine
        from sqlalchemy import text
        await ensure_marine_schema(_DSN)
        async with get_engine(_DSN).begin() as conn:
            await conn.execute(text("TRUNCATE core.sea_channel RESTART IDENTITY CASCADE"))
            await conn.execute(text("TRUNCATE core.marine_import_files RESTART IDENTITY CASCADE"))

    def test_import_persists_50_and_is_idempotent(self):
        async def run():
            await self._prepare()
            from services.marine.upload_service import MarineUploadService
            from services.marine.sea_channel import SeaChannelRepository, SeaChannelService
            svc = MarineUploadService(_DSN)
            content = _zip_bundle()

            r1 = await svc.import_file(content, "JNPA_Sea_Channels.zip", "dev")
            assert r1["status"] in ("SUCCESS", "PARTIAL")
            assert r1["imported"] == 50
            assert r1["document_type"] == "SEA_CHANNEL"

            repo = SeaChannelRepository(_DSN)
            assert await repo.count({}) == 50  # select count(*) from core.sea_channel

            # geojson projection works and returns WGS84 polygons
            fc = await SeaChannelService(_DSN).geojson({}, limit=500)
            assert fc["type"] == "FeatureCollection" and fc["count"] == 50
            assert fc["features"][0]["geometry"]["type"] == "Polygon"

            # identical bytes → file dedup, no growth
            r2 = await svc.import_file(content, "JNPA_Sea_Channels.zip", "dev")
            assert r2["status"] == "SKIPPED_DUPLICATE"
            assert await repo.count({}) == 50

            # byte-different copy → row-hash dedup keeps the count at 50
            r3 = await svc.import_file(content + b" ", "copy.zip", "dev")
            assert await repo.count({}) == 50, "row_sha256 dedup failed"
        _run(run)
