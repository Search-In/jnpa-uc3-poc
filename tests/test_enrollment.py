"""Driver-enrolment workflow tests — PWA submit -> admin approve (Identity / C2).

Two layers, both in-process and infra-free:

  * the ``gateway.enrollment`` store/state-machine, exercised directly against its
    in-memory backend (no Postgres), and
  * the gateway ``/api/identity`` enrolment HTTP surface, booted with the Starlette
    TestClient + a FakeHttp so every upstream call degrades to the synthetic path.

DPDP posture is asserted (consent required; the audit trail records each event).
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "identity"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unreachable DSN -> the store's schema bootstrap fails fast and pins the
# in-memory backend (the demo/test posture); no Postgres needed.
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway import enrollment as enr  # noqa: E402

IMG = "data:image/jpeg;base64,QUJD"


@pytest.fixture(autouse=True)
def _clean_store():
    enr._MEM.clear()
    enr._MEM_AUDIT.clear()
    yield
    enr._MEM.clear()
    enr._MEM_AUDIT.clear()


# ---------------------------------------------------------------------------
# Store + state machine (in-memory backend, dsn="")
# ---------------------------------------------------------------------------
def test_decode_data_url_handles_data_and_bare_base64():
    assert enr.decode_data_url(IMG) == b"ABC"
    assert enr.decode_data_url("QUJD") == b"ABC"
    assert enr.decode_data_url(None) is None
    assert enr.decode_data_url("") is None


def test_submit_creates_pending_with_consent_timestamp():
    async def _t():
        rec = await enr.submit("", driver_id="DRV-T1", name="Test One",
                               license_no="MH04 X", consent=True, face_images=[IMG, IMG])
        assert rec["status"] == enr.PENDING
        assert rec["photo"]  # list thumbnail derived from the first frame
        # The summary view must NOT leak raw frames.
        assert "face_images" not in rec
        full = await enr.get("", "DRV-T1", include_faces=True)
        assert full and len(full["face_images"]) == 2
        assert full["consent"] is True and full["consent_at"]

    asyncio.run(_t())


def test_list_filters_by_status_and_hides_frames():
    async def _t():
        await enr.submit("", driver_id="DRV-T1", name="A", consent=True, face_images=[IMG])
        await enr.submit("", driver_id="DRV-T2", name="B", consent=True, face_images=[IMG])
        pending = await enr.list_requests("", status=enr.PENDING)
        assert {r["driver_id"] for r in pending} == {"DRV-T1", "DRV-T2"}
        assert all("face_images" not in r for r in pending)
        assert await enr.list_requests("", status=enr.ACTIVE) == []

    asyncio.run(_t())


def test_approve_activates_and_keeps_one_reference_frame():
    async def _t():
        await enr.submit("", driver_id="DRV-T1", name="A", consent=True, face_images=[IMG, IMG])
        updated = await enr.mark_active("", "DRV-T1", actor="admin:1",
                                        photo_url="http://minio/driver-enrolment/DRV-T1.jpg",
                                        reference_image=IMG, template_dim=128, provider="onnx")
        assert updated["status"] == enr.ACTIVE
        assert updated["template_dim"] == 128 and updated["provider"] == "onnx"
        # Pending review frames are cleared on approval; the canonical reference is
        # kept for restart self-heal.
        full = await enr.get("", "DRV-T1", include_faces=True)
        assert full["face_images"] == []
        assert full["reference_image"] == IMG
        assert await enr.list_requests("", status=enr.ACTIVE)

    asyncio.run(_t())


def test_reject_keeps_record_so_driver_can_resubmit():
    async def _t():
        await enr.submit("", driver_id="DRV-T1", name="A", consent=True, face_images=[IMG])
        await enr.set_status("", "DRV-T1", enr.REJECTED, actor="admin:1", reason="blurry frame")
        rec = await enr.get("", "DRV-T1")
        assert rec["status"] == enr.REJECTED and rec["rejection_reason"] == "blurry frame"
        # Re-submitting overwrites the rejected record back to PENDING.
        again = await enr.submit("", driver_id="DRV-T1", name="A", consent=True, face_images=[IMG])
        assert again["status"] == enr.PENDING

    asyncio.run(_t())


def test_driver_faces_store_and_nearest_neighbour():
    """1:N store: persist templates, then nearest-cosine picks the right driver and
    rejects an out-of-gallery probe (in-memory backend, no model needed)."""
    from identity.embeddings import cosine

    async def _t():
        await enr.store_face("", "DRV-A", [1.0, 0.0, 0.0], dim=3, provider="onnx")
        await enr.store_face("", "DRV-B", [0.0, 1.0, 0.0], dim=3, provider="onnx")
        faces = await enr.load_faces("")
        assert {f["driver_id"] for f in faces} == {"DRV-A", "DRV-B"}

        def identify(probe, threshold=0.45):
            best_id, best = None, -1.0
            for f in faces:
                s = cosine(probe, f["embedding"])
                if s > best:
                    best_id, best = f["driver_id"], s
            return (best_id, best) if best >= threshold else (None, best)

        assert identify([0.95, 0.05, 0.0])[0] == "DRV-A"   # nearest A
        assert identify([0.05, 0.95, 0.0])[0] == "DRV-B"   # nearest B
        assert identify([0.0, 0.0, 1.0])[0] is None        # orthogonal -> UNKNOWN

    asyncio.run(_t())


def test_audit_trail_records_each_lifecycle_event():
    async def _t():
        await enr.submit("", driver_id="DRV-T1", name="A", consent=True, face_images=[IMG])
        await enr.mark_active("", "DRV-T1", actor="admin:1", photo_url=None,
                              reference_image=IMG, template_dim=128, provider="synthetic")
        events = [a["event"] for a in enr._MEM_AUDIT if a["driver_id"] == "DRV-T1"]
        assert "SUBMITTED" in events and "APPROVED" in events

    asyncio.run(_t())


# ---------------------------------------------------------------------------
# Gateway HTTP surface (/api/identity enrolment workflow)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("KAFKA_BROKERS", "127.0.0.1:1")
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()
    import gateway.config as cfgmod
    importlib.reload(cfgmod)
    import gateway.main as mainmod
    importlib.reload(mainmod)
    from starlette.testclient import TestClient

    c = TestClient(mainmod.app)
    c.__enter__()

    class FakeHttp:
        async def get(self, url: str, **kw):
            import httpx
            raise httpx.ConnectError(f"down: {url}")

        async def post(self, url: str, **kw):
            import httpx
            raise httpx.ConnectError(f"down: {url}")

        async def aclose(self):
            pass

    mainmod.app.state.gw.http = FakeHttp()
    yield c
    c.__exit__(None, None, None)


def test_enrol_request_requires_consent(client):
    r = client.post("/api/identity/enrol-request",
                    json={"driver_id": "DRV-W1", "name": "No Consent", "images": [IMG]})
    assert r.status_code == 400
    assert "consent" in r.json()["detail"].lower()


def test_enrol_request_requires_an_image(client):
    r = client.post("/api/identity/enrol-request",
                    json={"driver_id": "DRV-W1", "name": "No Image", "consent": True})
    assert r.status_code == 400


def test_full_workflow_submit_list_approve_then_gallery(client):
    # 1. Driver submits an enrolment request.
    sub = client.post("/api/identity/enrol-request", json={
        "driver_id": "DRV-W2", "name": "Workflow Driver", "license_no": "MH04 99",
        "consent": True, "images": [IMG, IMG],
    })
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "PENDING"

    # 2. Admin sees it in the PENDING queue (no raw frames in the summary).
    pending = client.get("/api/identity/enrollments?status=PENDING").json()["enrollments"]
    row = next(e for e in pending if e["driver_id"] == "DRV-W2")
    assert "face_images" not in row and row["photo"]

    # 3. Admin opens the detail view — frames are present for review.
    detail = client.get("/api/identity/enrollments/DRV-W2").json()
    assert len(detail["face_images"]) == 2

    # 4. Approve -> ACTIVE (identity upstream is down, so provider degrades).
    appr = client.post("/api/identity/enrollments/DRV-W2/approve")
    assert appr.status_code == 200, appr.text
    assert appr.json()["approved"] is True
    assert appr.json()["enrollment"]["status"] == "ACTIVE"

    # 5. The approved driver now appears in the verification gallery.
    drivers = client.get("/api/identity/gallery").json()["drivers"]
    enrolled = next(d for d in drivers if d["driver_id"] == "DRV-W2")
    assert enrolled.get("enrolled") is True


def test_reject_flow_marks_rejected(client):
    client.post("/api/identity/enrol-request", json={
        "driver_id": "DRV-W3", "name": "Reject Me", "consent": True, "images": [IMG],
    })
    r = client.post("/api/identity/enrollments/DRV-W3/reject", json={"reason": "spoofed"})
    assert r.status_code == 200 and r.json()["rejected"] is True
    status = client.get("/api/identity/enrol-request/DRV-W3").json()
    assert status["status"] == "REJECTED" and status["rejection_reason"] == "spoofed"
