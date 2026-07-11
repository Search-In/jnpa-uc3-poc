"""Tests for the Cargo CRUD surface (/api/cargo) — POC-3 common backend.

Two layers, mirroring the rest of the suite (test_gateway / test_empty_container):

* Pure validation checks on the ISO-6346 PK + DTO layer (no server, no DB).
* Full router CRUD via Starlette's TestClient with the DB repository swapped for
  an in-memory fake through ``app.dependency_overrides`` — so the router logic
  (201/200/400/404/409, filtering, patch semantics) is exercised deterministically
  with no Postgres. A final integration test hits a REAL DB and is skipped
  automatically when Postgres is unreachable (compose publishes it on host 5433).

Covers the Phase-6 checklist: Create · Get-All · Get-One · Update · Delete ·
Duplicate container · Invalid ISO · Missing record · Invalid payload.
"""
from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path fails fast (the fake bypasses it).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from jnpa_shared.iso6346 import is_valid_container_no, with_check_digit  # noqa: E402
from services.cargo import CargoConflict, CargoNotFound, CargoService  # noqa: E402

# A valid + an invalid ISO-6346 number reused across the tests.
VALID_CN = "MAEU6123458"
BAD_CN = "MAEU6123450"  # right shape, wrong check digit
assert is_valid_container_no(VALID_CN) and not is_valid_container_no(BAD_CN)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_WRITABLE = ("vessel_name", "customs_status", "yard_block", "is_released",
             "vehicle_number", "gate", "camera_id", "eta")


def _enum_val(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


class FakeCargoRepo:
    """In-memory stand-in for CargoRepository with identical method contracts."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    async def create(self, row: Mapping[str, Any]) -> dict:
        cn = row["container_number"]
        if cn in self._rows:
            raise CargoConflict(cn)
        rec = {
            "container_number": cn,
            "vessel_name": row.get("vessel_name"),
            "customs_status": _enum_val(row.get("customs_status")) or "PENDING",
            "yard_block": row.get("yard_block"),
            "is_released": bool(row.get("is_released") or False),
            "vehicle_number": row.get("vehicle_number"),
            "gate": row.get("gate"),
            "camera_id": row.get("camera_id"),
            "eta": row.get("eta"),
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        self._rows[cn] = rec
        return dict(rec)

    async def get(self, container_number: str) -> Optional[dict]:
        r = self._rows.get(container_number)
        return dict(r) if r else None

    def _filtered(self, *, container_number=None, customs_status=None,
                  yard_block=None, is_released=None, vehicle_number=None) -> list[dict]:
        rows = list(self._rows.values())
        eq = {"container_number": container_number, "customs_status": customs_status,
              "yard_block": yard_block, "is_released": is_released,
              "vehicle_number": vehicle_number}
        for col, val in eq.items():
            if val is not None:
                rows = [r for r in rows if r[col] == val]
        return rows

    async def list(self, *, limit=100, offset=0, **filters) -> list[dict]:
        rows = self._filtered(**filters)
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def count(self, **filters) -> int:
        return len(self._filtered(**filters))

    async def update(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        if container_number not in self._rows:
            raise CargoNotFound(container_number)
        rec = self._rows[container_number]
        for k, v in fields.items():
            if k in _WRITABLE:
                rec[k] = _enum_val(v) if k == "customs_status" else v
        rec["updated_at"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
        return dict(rec)

    async def delete(self, container_number: str) -> bool:
        return self._rows.pop(container_number, None) is not None


@pytest.fixture()
def client():
    """Fresh app-bound TestClient with the cargo service backed by a fake repo."""
    from gateway.main import app
    from gateway.routers import cargo as cargo_router

    fake_service = CargoService(repository=FakeCargoRepo())
    app.dependency_overrides[cargo_router.get_service] = lambda: fake_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(cargo_router.get_service, None)


def _payload(**over) -> dict:
    base = {
        "container_number": VALID_CN, "vessel_name": "MAERSK SEMBAWANG",
        "customs_status": "PENDING", "yard_block": "A-01", "is_released": False,
        "vehicle_number": "MH04AB1234", "gate": "GATE-1", "camera_id": "CAM-ANPR-01",
        "eta": "2026-07-12T08:30:00Z",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------- pure layer
def test_iso6346_pk_validation():
    assert is_valid_container_no(with_check_digit("MSCU778901"))
    assert not is_valid_container_no("NOTACONTAINER")
    assert not is_valid_container_no(BAD_CN)


# ------------------------------------------------------------------- CRUD (fake)
def test_create_returns_201(client):
    r = client.post("/api/cargo", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["container_number"] == VALID_CN
    assert body["customs_status"] == "PENDING"
    assert body["is_released"] is False
    assert "created_at" in body and "updated_at" in body


def test_get_all(client):
    client.post("/api/cargo", json=_payload())
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            customs_status="CLEARED", is_released=True))
    r = client.get("/api/cargo")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # filter works
    r2 = client.get("/api/cargo", params={"customs_status": "CLEARED"})
    assert [x["container_number"] for x in r2.json()] == ["MSCU7789010"]
    r3 = client.get("/api/cargo", params={"is_released": "true"})
    assert [x["container_number"] for x in r3.json()] == ["MSCU7789010"]
    # X-Total-Count reflects the full (pre-pagination) match count.
    assert r.headers.get("X-Total-Count") == "2"
    assert r2.headers.get("X-Total-Count") == "1"


def test_filters_and_pagination(client):
    client.post("/api/cargo", json=_payload(yard_block="A-01"))
    client.post("/api/cargo", json=_payload(container_number="MSCU7789010",
                                            yard_block="B-02", vehicle_number="MH05CD4567"))
    # yard_block filter
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"yard_block": "B-02"}).json()] == ["MSCU7789010"]
    # vehicle_number filter (normalised — spaces/case)
    assert [x["container_number"] for x in
            client.get("/api/cargo", params={"vehicle_number": "mh05 cd4567"}).json()] == ["MSCU7789010"]
    # container_number exact filter
    assert len(client.get("/api/cargo", params={"container_number": VALID_CN}).json()) == 1
    # bad ISO in the container_number filter -> 400
    assert client.get("/api/cargo", params={"container_number": BAD_CN}).status_code == 400
    # pagination: limit caps the page, X-Total-Count keeps the full size
    page = client.get("/api/cargo", params={"limit": 1, "offset": 0})
    assert len(page.json()) == 1
    assert page.headers.get("X-Total-Count") == "2"


def test_get_one(client):
    client.post("/api/cargo", json=_payload())
    r = client.get(f"/api/cargo/{VALID_CN}")
    assert r.status_code == 200
    assert r.json()["container_number"] == VALID_CN
    # case/space-insensitive lookup on the PK
    r2 = client.get("/api/cargo/maeu 6123458")
    assert r2.status_code == 200


def test_update(client):
    client.post("/api/cargo", json=_payload())
    r = client.put(f"/api/cargo/{VALID_CN}",
                   json={"customs_status": "CLEARED", "is_released": True,
                         "yard_block": "B-09"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customs_status"] == "CLEARED"
    assert body["is_released"] is True
    assert body["yard_block"] == "B-09"
    # unchanged fields preserved
    assert body["vessel_name"] == "MAERSK SEMBAWANG"


def test_delete(client):
    client.post("/api/cargo", json=_payload())
    r = client.delete(f"/api/cargo/{VALID_CN}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # gone now
    assert client.get(f"/api/cargo/{VALID_CN}").status_code == 404


def test_duplicate_container_409(client):
    assert client.post("/api/cargo", json=_payload()).status_code == 201
    r = client.post("/api/cargo", json=_payload())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "duplicate_container"


def test_invalid_iso_400(client):
    r = client.post("/api/cargo", json=_payload(container_number=BAD_CN))
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


def test_missing_record_404(client):
    # A valid, well-formed container that was never created.
    r = client.get("/api/cargo/MSCU7789010")
    assert r.status_code == 404
    assert client.put("/api/cargo/MSCU7789010", json={"is_released": True}).status_code == 404
    assert client.delete("/api/cargo/MSCU7789010").status_code == 404


def test_invalid_payload_400(client):
    # Bad enum value.
    assert client.post("/api/cargo", json=_payload(customs_status="NOT_A_STATUS")).status_code == 400
    # Wrong type for is_released.
    assert client.post("/api/cargo", json=_payload(is_released="banana")).status_code == 400
    # Bad ISO on a PUT path.
    assert client.put(f"/api/cargo/{BAD_CN}", json={"is_released": True}).status_code == 400
    # Missing required container_number.
    p = _payload()
    p.pop("container_number")
    assert client.post("/api/cargo", json=p).status_code == 400


# --------------------------------------------------- real-DB integration (opt-in)
def _pg_reachable(host: str = "127.0.0.1", port: int = 5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_roundtrip():
    """Exercises the REAL raw-SQL repository end-to-end against Postgres."""
    dsn = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/postgres"
    from gateway.main import app
    from gateway.routers import cargo as cargo_router

    app.dependency_overrides[cargo_router.get_service] = lambda: CargoService(dsn=dsn)
    cn = with_check_digit("TESTU999000")  # unique-ish, valid ISO
    try:
        with TestClient(app) as c:
            c.delete(f"/api/cargo/{cn}")  # clean slate
            assert c.post("/api/cargo", json=_payload(container_number=cn)).status_code == 201
            assert c.post("/api/cargo", json=_payload(container_number=cn)).status_code == 409
            assert c.get(f"/api/cargo/{cn}").json()["container_number"] == cn
            up = c.put(f"/api/cargo/{cn}", json={"customs_status": "CLEARED"})
            assert up.status_code == 200 and up.json()["customs_status"] == "CLEARED"
            assert c.delete(f"/api/cargo/{cn}").status_code == 200
            assert c.get(f"/api/cargo/{cn}").status_code == 404
    finally:
        app.dependency_overrides.pop(cargo_router.get_service, None)
