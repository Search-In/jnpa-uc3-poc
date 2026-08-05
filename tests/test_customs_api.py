"""Customs router tests (/api/customs) — router logic via TestClient with the DB
repository swapped for an in-memory fake through ``app.dependency_overrides``, so
pagination, X-Total-Count, filters and 404s are exercised deterministically with no
Postgres. (Real-data persistence is covered by tests/test_customs_repository.py.)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from services.customs import CustomsService  # noqa: E402


class FakeCustomsRepo:
    """Minimal in-memory stand-in for CustomsRepository — enough for router logic."""

    def __init__(self) -> None:
        self.messages = [
            {"id": 1, "message_type": "CHPOI03", "module": "IGM", "primary_ref": "1194792",
             "source_file": "CHPOI03_1194792.xml", "record_count": 95, "imported_count": 95,
             "error_count": 0, "import_status": "SUCCESS"},
            {"id": 2, "message_type": "SHIPPING_BILL", "module": "SHIPPING_BILL",
             "primary_ref": "4014226", "source_file": "shippingbill.xlsx", "record_count": 100,
             "imported_count": 15, "error_count": 0, "import_status": "SUCCESS"},
        ]

    @staticmethod
    def _match(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        return all(v is None or str(row.get(k)) == str(v) for k, v in filters.items())

    def _page(self, rows, filters, limit, offset):
        sel = [r for r in rows if self._match(r, filters)]
        return sel[offset:offset + limit]

    def _count(self, rows, filters):
        return len([r for r in rows if self._match(r, filters)])

    async def list_messages(self, *, filters, limit, offset):
        return self._page(self.messages, filters, limit, offset)

    async def count_messages(self, *, filters):
        return self._count(self.messages, filters)

    async def get_message(self, message_id):
        return next((m for m in self.messages if m["id"] == message_id), None)

    async def list_message_errors(self, message_id, *, limit, offset):
        return []

    async def list_igm(self, *, filters, limit, offset):
        return self._page([{"id": 1, "igm_no": "1194792", "container_count": 95, "line_count": 30}],
                          filters, limit, offset)

    async def count_igm(self, *, filters):
        return self._count([{"igm_no": "1194792"}], filters)

    async def list_igm_containers(self, *, filters, limit, offset):
        return self._page([{"id": 1, "igm_no": "1194792", "container_no": "CAIU6709422"}],
                          filters, limit, offset)

    async def count_igm_containers(self, *, filters):
        return self._count([{"igm_no": "1194792", "container_no": "CAIU6709422"}], filters)

    async def list_ooc(self, *, filters, limit, offset):
        return self._page([{"id": 1, "bill_of_entry_no": "9352934", "igm_no": "1194193"}],
                          filters, limit, offset)

    async def count_ooc(self, *, filters):
        return self._count([{"bill_of_entry_no": "9352934", "igm_no": "1194193"}], filters)

    async def list_smtp(self, *, filters, limit, offset):
        return self._page([{"id": 1, "smtp_no": "2697414", "bond_no": "2000067135"}],
                          filters, limit, offset)

    async def count_smtp(self, *, filters):
        return self._count([{"smtp_no": "2697414", "bond_no": "2000067135"}], filters)

    async def list_rms(self, *, filters, limit, offset):
        return self._page([{"id": 1, "igm_no": "1191409", "selected_count": 16}], filters, limit, offset)

    async def count_rms(self, *, filters):
        return self._count([{"igm_no": "1191409"}], filters)

    async def list_leo(self, *, filters, limit, offset):
        return self._page([{"id": 1, "sb_no": "2343823"}], filters, limit, offset)

    async def count_leo(self, *, filters):
        return self._count([{"sb_no": "2343823"}], filters)

    async def list_shipping_bills(self, *, filters, limit, offset):
        return self._page([{"id": 1, "sb_no": "4014226"}], filters, limit, offset)

    async def count_shipping_bills(self, *, filters):
        return self._count([{"sb_no": "4014226"}], filters)

    async def container_customs(self, container_no):
        if container_no == "CAIU6709422":
            return {"container_no": container_no,
                    "status": {"container_no": container_no, "declared_igm": True,
                               "rms_selected": False, "ooc_cleared": False, "smtp_bonded": False},
                    "igm": [{"igm_no": "1194792", "container_no": container_no}],
                    "ooc": [], "smtp": [], "rms": []}
        return {"container_no": container_no, "status": None, "igm": [], "ooc": [], "smtp": [], "rms": []}

    async def list_events(self, **kwargs):
        return [{"id": 1, "event": "customs.igm_filed", "module": "IGM", "reference": "1194792"}]

    async def summary(self):
        return {"messages": 2, "igm_containers": 95, "shipping_bills": 15}


@pytest.fixture()
def client():
    from gateway.main import app
    from gateway.routers import customs as customs_router

    fake = CustomsService(repository=FakeCustomsRepo())
    app.dependency_overrides[customs_router.get_service] = lambda: fake
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(customs_router.get_service, None)


def test_summary(client):
    r = client.get("/api/customs/summary")
    assert r.status_code == 200
    assert r.json()["messages"] == 2


def test_messages_pagination_and_total_header(client):
    r = client.get("/api/customs/messages?limit=1&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["count"] == 1 and body["limit"] == 1
    assert r.headers["X-Total-Count"] == "2"


def test_messages_filter_by_module(client):
    r = client.get("/api/customs/messages?module=SHIPPING_BILL")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["imported_count"] == 15


def test_message_detail_and_404(client):
    assert client.get("/api/customs/messages/1").status_code == 200
    r = client.get("/api/customs/messages/999")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "message_not_found"


def test_entity_lists(client):
    for path, key in [("igm", "igm_no"), ("ooc", "bill_of_entry_no"), ("smtp", "smtp_no"),
                      ("rms", "igm_no"), ("leo", "sb_no"), ("shipping-bills", "sb_no")]:
        r = client.get(f"/api/customs/{path}")
        assert r.status_code == 200, path
        assert r.json()["total"] == 1, path
        assert key in r.json()["items"][0], path


def test_igm_containers(client):
    r = client.get("/api/customs/igm/1194792/containers")
    assert r.status_code == 200
    assert r.json()["items"][0]["container_no"] == "CAIU6709422"


def test_container_customs_view_and_404(client):
    r = client.get("/api/customs/containers/CAIU6709422")
    assert r.status_code == 200
    assert r.json()["status"]["declared_igm"] is True
    # lowercase input is normalised to the canonical container id
    assert client.get("/api/customs/containers/caiu6709422").status_code == 200
    assert client.get("/api/customs/containers/ZZZU0000000").status_code == 404


def test_events(client):
    r = client.get("/api/customs/events?module=IGM")
    assert r.status_code == 200
    assert r.json()["items"][0]["event"] == "customs.igm_filed"
