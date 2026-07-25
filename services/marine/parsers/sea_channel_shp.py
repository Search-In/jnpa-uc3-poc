"""JNPA_Sea_Channels ESRI shapefile → core.sea_channel. Pure, no DB, ZERO GIS deps.

The sea-channel geometry is a 50-polygon ESRI shapefile in UTM Zone 43N (EPSG:32643).
pyshp / pyproj / GDAL are NOT available on the gateway, so this module reads the .shp
(polygon geometry) and .dbf (attributes) directly from their documented binary layouts
and reprojects every vertex UTM 43N → WGS84 with the standard inverse Transverse-Mercator
series (sub-metre accuracy). Geometry is emitted as GeoJSON (lon/lat, EPSG:4326).

Upload shape: a ZIP bundling .shp + .dbf (+ .prj), OR a bare .shp (geometry only, no
attributes). Nothing is dropped — the raw DBF attributes are kept per record and the
whole thing is content-hashed for idempotent import.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import zipfile
from typing import Any, Optional

from ..upload_parsers import ParseResult

# --- WGS84 / UTM Zone 43N (EPSG:32643) constants ---
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = 2 * _F - _F * _F
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_FE = 500000.0
_LON0 = math.radians(75.0)   # central meridian of zone 43


def _utm43n_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Inverse Transverse Mercator (UTM 43N → lon/lat degrees). Standard Snyder series."""
    x = easting - _FE
    m = northing / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    c1 = _EP2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = _A / math.sqrt(1 - _E2 * math.sin(phi1) ** 2)
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * _EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * _EP2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = _LON0 + (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * _EP2 + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(phi1)
    return round(math.degrees(lon), 7), round(math.degrees(lat), 7)


def _read_shp_polygons(shp: bytes) -> list[Optional[list[list[tuple[float, float]]]]]:
    """Parse an ESRI .shp into per-record ring lists of (easting, northing). Non-polygon
    records → None (kept positional so the DBF row indexes line up)."""
    if len(shp) < 100 or struct.unpack(">i", shp[0:4])[0] != 9994:
        raise ValueError("not a valid ESRI .shp (bad file code)")
    out: list[Optional[list[list[tuple[float, float]]]]] = []
    off = 100
    n = len(shp)
    while off + 8 <= n:
        _rn, clen = struct.unpack(">ii", shp[off:off + 8])
        off += 8
        body = shp[off:off + clen * 2]
        off += clen * 2
        if len(body) < 4:
            break
        shape_type = struct.unpack("<i", body[0:4])[0]
        if shape_type != 5:  # 5 = Polygon
            out.append(None)
            continue
        nparts, npoints = struct.unpack("<ii", body[36:44])
        parts = list(struct.unpack("<%di" % nparts, body[44:44 + 4 * nparts]))
        pbase = 44 + 4 * nparts
        pts = struct.unpack("<%dd" % (2 * npoints), body[pbase:pbase + 16 * npoints])
        idx = parts + [npoints]
        rings = [[(pts[i * 2], pts[i * 2 + 1]) for i in range(idx[k], idx[k + 1])]
                 for k in range(nparts)]
        out.append(rings)
    return out


def _read_dbf(dbf: bytes) -> list[dict[str, str]]:
    """Parse a .dbf into a list of {field: value} dicts (all text, trimmed)."""
    if len(dbf) < 32:
        return []
    header_size = struct.unpack("<H", dbf[8:10])[0]
    record_size = struct.unpack("<H", dbf[10:12])[0]
    fields: list[tuple[str, int]] = []
    o = 32
    while o < len(dbf) and dbf[o:o + 1] != b"\x0d":
        nm = dbf[o:o + 11].split(b"\x00")[0].decode("latin-1").strip()
        length = dbf[o + 16]
        fields.append((nm, length))
        o += 32
    rows: list[dict[str, str]] = []
    pos = header_size
    while pos + record_size <= len(dbf):
        rec = dbf[pos:pos + record_size]
        pos += record_size
        if rec[:1] == b"\x2a":  # deleted flag
            rows.append({})
            continue
        cur = 1
        row: dict[str, str] = {}
        for nm, length in fields:
            row[nm] = rec[cur:cur + length].decode("latin-1").strip()
            cur += length
        rows.append(row)
    return rows


def _num(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _extract_bundle(content: bytes, filename: Optional[str]) -> tuple[Optional[bytes], Optional[bytes]]:
    """Return (shp_bytes, dbf_bytes). Accepts a ZIP bundle or a bare .shp."""
    if content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            shp = dbf = None
            for nm in z.namelist():
                low = nm.lower()
                if low.endswith(".shp"):
                    shp = z.read(nm)
                elif low.endswith(".dbf"):
                    dbf = z.read(nm)
            return shp, dbf
    # bare .shp upload — geometry only, no attributes
    return content, None


def parse_sea_channel_shp(content: bytes, filename: Optional[str] = None) -> ParseResult:
    res = ParseResult()
    try:
        shp_bytes, dbf_bytes = _extract_bundle(content, filename)
    except Exception as exc:  # noqa: BLE001
        res.rejected = True
        res.err(None, None, "shp_zip_error", f"could not read shapefile bundle: {exc}")
        return res
    if not shp_bytes:
        res.rejected = True
        res.err(None, None, "shp_missing", "no .shp geometry found in the upload")
        return res

    try:
        geoms = _read_shp_polygons(shp_bytes)
    except Exception as exc:  # noqa: BLE001
        res.rejected = True
        res.err(None, None, "shp_parse_error", f"could not parse .shp geometry: {exc}")
        return res

    attrs = _read_dbf(dbf_bytes) if dbf_bytes else []
    res.row_count = len(geoms)
    seen: set[str] = set()

    for i, rings in enumerate(geoms):
        if not rings:
            res.warn(i + 1, None, "non_polygon_record", "shape record is not a polygon; skipped")
            continue
        # Reproject every vertex UTM 43N → WGS84 (lon/lat).
        coords = [[list(_utm43n_to_wgs84(x, y)) for (x, y) in ring] for ring in rings]
        geom = {"type": "Polygon", "coordinates": coords}

        a = attrs[i] if i < len(attrs) else {}
        name = (a.get("NAME") or "").strip() or f"Channel {i + 1}"
        section = (a.get("DESCRIPTIO") or "").strip() or None
        area_ha = _num(a.get("SHAPE_AREA"))     # SHAPE_AREA is hectares (SHAPE_Ar_1 is m²)
        length_m = _num(a.get("SHAPE_Leng"))

        rec: dict[str, Any] = {
            "_target": "sea_channel", "_message": "SEA_CHANNEL", "_source_file": filename,
            "name": name,
            "section_label": section,
            "area_ha": area_ha,
            "length_m": length_m,
            "geom_geojson": geom,
        }
        payload = json.dumps({"n": name, "s": section, "a": area_ha, "l": length_m,
                              "g": geom}, sort_keys=True, default=str)
        rec["row_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
        if rec["row_sha256"] in seen:
            res.duplicate_count += 1
            continue
        seen.add(rec["row_sha256"])
        res.records.append(rec)

    res.preview = [{
        "Name": r["name"], "Section": r.get("section_label") or "—",
        "Area (ha)": r.get("area_ha"), "Length (m)": r.get("length_m"),
        "Vertices": sum(len(ring) for ring in r["geom_geojson"]["coordinates"]),
    } for r in res.records[:20]]
    return res
