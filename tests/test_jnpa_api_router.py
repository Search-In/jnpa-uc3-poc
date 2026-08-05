"""Gateway router tests for /api/integrations/jnpa/* and /api/jnpa/files/*
(dependency_overrides + TestClient, no DB, no network — the
tests/test_berthing_upload.py router pattern)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway.routers import jnpa_api  # noqa: E402
from services.jnpa_sync.service import JnpaSyncService  # noqa: E402
from services.jnpa_sync.store import ApiFileStore  # noqa: E402

from test_jnpa_sync_service import FakeSyncRepo, RecordingRouter  # noqa: E402


class _StubClient:
    """Just enough of JnpaPortDataClient for health()."""

    configured = True
    api_url = "http://sim"


@pytest.fixture()
def harness(tmp_path):
    repo = FakeSyncRepo()
    store = ApiFileStore(tmp_path / "store")
    service = JnpaSyncService(client=_StubClient(), repository=repo,
                              store=store, router=RecordingRouter(),
                              api_mode="SIM")
    app = FastAPI()
    app.include_router(jnpa_api.router)
    app.dependency_overrides[jnpa_api.get_sync_service] = lambda: service
    with TestClient(app) as client:
        yield client, service, repo, store
    app.dependency_overrides.pop(jnpa_api.get_sync_service, None)


def test_health_reports_groups_and_mode(harness):
    client, *_ = harness
    resp = client.get("/api/integrations/jnpa/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "SIM"
    assert len(body["groups"]) == 13
    kinds = {g["group"]: g["kind"] for g in body["groups"]}
    assert kinds["bathymetry"] == "static"
    assert kinds["berthing-reports"] == "report"
    assert kinds["customs"] == "indexed"


def test_sync_unknown_group_is_400(harness):
    client, *_ = harness
    resp = client.post("/api/integrations/jnpa/sync",
                       json={"group": "nope"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_group"


def test_sync_static_group_is_skipped(harness):
    client, *_ = harness
    resp = client.post("/api/integrations/jnpa/sync",
                       json={"group": "bathymetry"})
    assert resp.status_code == 200
    assert resp.json()["result"]["status"] == "SKIPPED_STATIC"


def test_records_and_runs_read_back(harness):
    client, service, repo, _ = harness
    asyncio.run(repo.open_run(trigger="TEST", group="customs",
                              api_mode="SIM"))
    asyncio.run(repo.insert_record(
        record_id="rec_X", group="customs", message_type="CHPOI03",
        message_name=None, published_at=None, container_count=None,
        vessel_call=None, summary=None, file_ref="ref_X",
        media_type=None, size_bytes=10, checksum_sha256="ab" * 32,
        ingest_run_id=1, payload={}))
    runs = client.get("/api/integrations/jnpa/runs").json()
    records = client.get("/api/integrations/jnpa/records",
                         params={"group": "customs"}).json()
    assert runs["count"] == 1
    assert records["count"] == 1
    assert records["items"][0]["record_id"] == "rec_X"


def test_defects_markdown_export(harness):
    client, *_ = harness
    resp = client.get("/api/integrations/jnpa/defects",
                      params={"format": "md"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "runtime defect observations" in resp.text


def test_file_store_roundtrip(harness):
    client, service, repo, store = harness
    content = b"<CHPOI03Payload/>"
    sha = hashlib.sha256(content).hexdigest()
    store.save("customs", sha, "CHPOI03_1.xml", content)

    resp = client.get(f"/api/jnpa/files/{sha}")
    assert resp.status_code == 200
    assert resp.content == content
    assert 'filename="CHPOI03_1.xml"' in resp.headers["Content-Disposition"]

    missing = client.get("/api/jnpa/files/" + "0" * 64)
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "file_not_found"


def test_replay_endpoint_validates_group(harness):
    client, *_ = harness
    resp = client.post("/api/integrations/jnpa/replay",
                       json={"group": "nope"})
    assert resp.status_code == 400
