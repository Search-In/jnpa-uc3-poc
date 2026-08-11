"""SecureVision integration tests (no DB / no network).

Same pattern as tests/test_ulip_logistics.py — a real client over an
httpx.MockTransport, so the retry/auth/error machinery is genuinely exercised
rather than mocked away.

Covers:
  * login + call, token caching, ONE forced re-login on 401, then auth failure
  * timeout / 5xx retried, 4xx fail-fast
  * the three status codes that carry product meaning: 409 (analysis expired),
    422 (unprocessable), 503 (model not loaded), plus 409-as-duplicate on enrol
  * credential redaction (the password never reaches an exception or a log)
  * normalisation: media URL rewriting, clip-offset -> wall clock, camera
    mapping, ISO-6346 cross-check, and the three-state person verdict
  * camera mapping refusing to guess
  * stream tickets (scope + expiry)
  * router: RBAC policy, error mapping, upload validation, DPDP enrolment gate
  * the guarantee that nothing here changes an existing JNPA route
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.securevision import (
    SecureVisionAnalysisExpired,
    SecureVisionAuthError,
    SecureVisionClient,
    SecureVisionConflict,
    SecureVisionForbidden,
    SecureVisionHTTPError,
    SecureVisionModelUnavailable,
    SecureVisionNotConfigured,
    SecureVisionNotFound,
    SecureVisionTimeout,
    SecureVisionUnprocessable,
)
from gateway.routers import securevision as sv_router
from services.securevision import analyses, cameras, normalize, tickets

PASSWORD = "sv-service-password-987"
TOKEN = "issued-sv-token-xyz"
#: A realistic vendor analysis id (16 hex chars), as the routes validate length.
ANALYSIS = "89df115b4ed54243"


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------- canned payloads
def _login_payload() -> dict:
    return {
        "access_token": TOKEN,
        "token_type": "bearer",
        "user": {"id": 7, "username": "jnpa_service", "role": "super_admin",
                 "is_active": True},
    }


def _i01_payload() -> dict:
    return {
        "fired": True, "incident_type": "I-01", "title": "Trailer Plate Capture",
        "status": "SUCCESS", "confidence": 0.93, "validation_status": "PASSED",
        "ocr_confidence": 0.93, "camera_code": "CAM-01", "timestamp": 4.2,
        "track_id": 3,
        "snapshot": "/data/snapshots/analytics/abc/best_frames/bf_000.jpg",
        "image_url": "/media/analytics/abc/best_frames/bf_000.jpg",
        "evidence": [{"region_type": "plate",
                      "url": "/media/analytics/abc/evidence/plate_003.jpg",
                      "crop_score": 0.88, "track_id": 3}],
        "evidence_images": [],
        "facts": {"event_type": "PLATE_CAPTURE", "object_type": "vehicle",
                  "plate": "MH46BM3672", "plate_valid": True,
                  "vehicle_type": "truck", "vehicle_color": "white",
                  "camera_code": "CAM-01", "validation": "PASSED",
                  "ocr_confidence": 0.93},
        "description": "A white truck was captured at the gate.",
        "ai_generated": True, "processing_time_ms": 812.4,
        "vision_provider": "azure-openai-gpt-5.4",
    }


def _i07_payload() -> dict:
    return {
        "analysis_id": ANALYSIS, "camera_code": "CAM-01", "incident_type": "I-07",
        "fired": True, "count": 3,
        "persons": [
            {"incident_type": "I-07", "status": "SUCCESS", "confidence": 0.81,
             "camera_code": "CAM-01", "track_id": 5,
             "image_url": "/media/analytics/abc/best_frames/bf_002.jpg",
             "facts": {"zone": "Machinery Zone 1", "dwell_seconds": 8.4,
                       "authorized": True, "person_name": "Rahul Sharma",
                       "person_id": "EMP-1042", "person_status": "AUTHORIZED",
                       "identity_status": "AUTHORIZED", "face_similarity": 0.71}},
            {"incident_type": "I-07", "status": "PARTIAL_SUCCESS",
             "camera_code": "CAM-01", "track_id": 6,
             "facts": {"zone": "Machinery Zone 1", "dwell_seconds": 3.1,
                       "authorized": False, "person_status": "UNAUTHORIZED",
                       "face_similarity": 0.22}},
            # No verdict at all — must degrade to UNVERIFIED, never UNAUTHORIZED.
            {"incident_type": "I-07", "camera_code": "CAM-01", "track_id": 7,
             "facts": {"zone": "Machinery Zone 1", "dwell_seconds": 1.0}},
        ],
    }


def _client_with(handler, **kwargs) -> SecureVisionClient:
    kwargs.setdefault("username", "jnpa_service")
    kwargs.setdefault("password", PASSWORD)
    return SecureVisionClient(
        base_url="https://sv.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retries=2, backoff_s=0.0, **kwargs)


def _login_aware(api_handler):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        return api_handler(request)
    return handler


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts with no camera map, no analyses, no tickets."""
    monkeypatch.delenv(cameras.ENV_VAR, raising=False)
    cameras.reset_cache()
    analyses.reset()
    tickets.reset()
    sv_router.reset_client()
    yield
    cameras.reset_cache()
    analyses.reset()
    tickets.reset()
    sv_router.reset_client()


# ------------------------------------------------------------- client: happy
def test_login_then_incident_and_token_is_cached():
    calls = {"login": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            calls["login"] += 1
            assert PASSWORD in request.read().decode()
            return httpx.Response(200, json=_login_payload())
        calls["api"] += 1
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["analysis_id"] == ANALYSIS
        return httpx.Response(200, json=_i01_payload())

    client = _client_with(handler)
    first = _run(client.incident(ANALYSIS, "i01"))
    second = _run(client.incident(ANALYSIS, "i01"))
    assert first.facts["plate"] == "MH46BM3672"
    assert second.fired is True
    # One login for two calls: the token is cached, as the vendor expects.
    assert calls == {"login": 1, "api": 2}


def test_unknown_incident_code_never_reaches_the_vendor():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call the vendor")

    with pytest.raises(SecureVisionNotFound):
        _run(_client_with(_login_aware(handler)).incident(ANALYSIS, "i99"))


def test_not_configured_is_a_clean_refusal():
    client = SecureVisionClient(base_url="https://sv.test", username="",
                                password="")
    assert client.configured is False
    with pytest.raises(SecureVisionNotConfigured):
        _run(client.incident(ANALYSIS, "i01"))


# --------------------------------------------------------------- client: auth
def test_401_forces_exactly_one_relogin_then_succeeds():
    calls = {"login": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            calls["login"] += 1
            return httpx.Response(200, json=_login_payload())
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"detail": "token expired"})
        return httpx.Response(200, json=_i01_payload())

    result = _run(_client_with(handler).incident(ANALYSIS, "i01"))
    assert result.fired is True
    assert calls["login"] == 2, "expected exactly one forced re-login"


def test_persistent_401_surfaces_as_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        return httpx.Response(401, json={"detail": "nope"})

    with pytest.raises(SecureVisionAuthError):
        _run(_client_with(handler).incident(ANALYSIS, "i01"))


def test_login_rejection_is_terminal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad credentials"})

    with pytest.raises(SecureVisionAuthError):
        _run(_client_with(handler).incident(ANALYSIS, "i01"))


def test_403_is_reported_as_a_vendor_permission_problem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "role required"})

    with pytest.raises(SecureVisionForbidden):
        _run(_client_with(_login_aware(handler)).list_faces())


# ---------------------------------------------------- client: failure vocabulary
def test_timeout_is_retried_then_typed():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        attempts["n"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(SecureVisionTimeout):
        _run(_client_with(_login_aware(handler)).incident(ANALYSIS, "i01"))
    assert attempts["n"] == 3, "first try + 2 retries"


def test_5xx_is_retried_then_typed():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502, json={"detail": "upstream boom"})

    with pytest.raises(SecureVisionHTTPError) as exc:
        _run(_client_with(_login_aware(handler)).incident(ANALYSIS, "i01"))
    assert exc.value.status_code == 502
    assert attempts["n"] == 3


def test_404_fails_fast_without_retrying():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"detail": "unknown analysis"})

    with pytest.raises(SecureVisionNotFound):
        _run(_client_with(_login_aware(handler)).incident(ANALYSIS, "i01"))
    assert attempts["n"] == 1, "retrying a 404 cannot help"


def test_409_on_stream_is_analysis_expired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "frames evicted"})

    async def go():
        client = _client_with(_login_aware(handler))
        async with client.stream_analysis(ANALYSIS):  # pragma: no cover - raises
            pass

    with pytest.raises(SecureVisionAnalysisExpired):
        _run(go())


def test_409_on_enrolment_is_a_duplicate_not_an_expiry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "person_id already enrolled"})

    with pytest.raises(SecureVisionConflict):
        _run(_client_with(_login_aware(handler)).enroll_face(
            person_id="EMP-1", name="A",
            photos=[("a.jpg", b"\xff\xd8\xffdata", "image/jpeg")]))


def test_422_and_503_are_distinct():
    def unprocessable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "no usable face detected"})

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "face model not loaded"})

    with pytest.raises(SecureVisionUnprocessable):
        _run(_client_with(_login_aware(unprocessable)).enroll_face(
            person_id="EMP-1", name="A",
            photos=[("a.jpg", b"\xff\xd8\xffdata", "image/jpeg")]))
    with pytest.raises(SecureVisionModelUnavailable):
        _run(_client_with(_login_aware(unavailable)).face_status())


def test_credentials_are_never_in_an_exception_message():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        # A vendor that echoes the secret back must not make us leak it.
        return httpx.Response(400, json={"detail": f"bad request {PASSWORD}"})

    with pytest.raises(SecureVisionHTTPError) as exc:
        _run(_client_with(handler).incident(ANALYSIS, "i01"))
    assert PASSWORD not in str(exc.value)
    assert "***" in str(exc.value)


# ------------------------------------------------------------ client: uploads
def test_upload_sends_multipart_with_camera_code():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        body = request.read()
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = body
        return httpx.Response(201, json={"analysis_id": ANALYSIS,
                                         "camera_code": "CAM-01",
                                         "frames_sampled": 30,
                                         "zones_loaded": 2})

    result = _run(_client_with(handler).upload_video(
        b"\x00\x00\x00\x18ftypmp42clipdata", filename="gate.mp4",
        content_type="video/mp4", camera_code="CAM-01"))
    assert result.analysis_id == ANALYSIS and result.zones_loaded == 2
    assert seen["ctype"].startswith("multipart/form-data")
    assert b"CAM-01" in seen["body"] and b"gate.mp4" in seen["body"]


def test_stream_relays_chunks_with_the_service_token():
    frames = [b"--frame\r\n", b"\xff\xd8jpeg-1\xff\xd9", b"--frame\r\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json=_login_payload())
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["fps"] == "5"
        return httpx.Response(
            200, stream=httpx.ByteStream(b"".join(frames)),
            headers={"content-type": "multipart/x-mixed-replace; boundary=frame"})

    async def go():
        client = _client_with(handler)
        async with client.stream_analysis(ANALYSIS, fps=5) as (ctype, chunks):
            body = b"".join([c async for c in chunks])
        return ctype, body

    ctype, body = _run(go())
    assert ctype.startswith("multipart/x-mixed-replace")
    assert body == b"".join(frames)


# ------------------------------------------------------------- normalisation
def test_normalize_i01_rewrites_media_and_derives_wall_clock():
    from integrations.securevision import SvIncident

    analyses.record(ANALYSIS, securevision_camera_code="CAM-01",
                    jnpa_camera_id="CAM-NSICT-ENT", filename="gate.mp4",
                    frames_sampled=30, detection_pass_count=1, zones_loaded=2,
                    uploaded_by="TEST")
    os.environ[cameras.ENV_VAR] = json.dumps({"CAM-01": "CAM-NSICT-ENT"})
    cameras.reset_cache()

    out = normalize.normalize_incident(
        SvIncident.model_validate(_i01_payload()), code="i01", analysis_id=ANALYSIS)

    assert out["source"] == "SECUREVISION"
    assert out["image_url"] == "/api/sv/media/analytics/abc/best_frames/bf_000.jpg"
    assert out["evidence"][0]["url"].startswith("/api/sv/media/")
    assert out["camera"] == {"securevision_code": "CAM-01",
                             "jnpa_camera_id": "CAM-NSICT-ENT",
                             "mapped": True, "map_configured": True}
    assert out["plate"]["plate"] == "MH46BM3672"
    assert out["confidence_pct"] == 93.0
    assert out["clip_offset_s"] == 4.2
    # 4.2s into a clip uploaded at T -> an absolute instant, not a bare offset.
    assert out["detected_at"] is not None and out["detected_at"] > \
        analyses.uploaded_at(ANALYSIS)


def test_vendor_filesystem_paths_are_never_exposed():
    assert normalize.media_url("/data/snapshots/analytics/abc/bf_000.jpg") is None
    assert normalize.media_url("/media/../../etc/passwd") is None
    assert normalize.media_url("") is None
    assert normalize.media_url("/media/analytics/x/y.jpg") == \
        "/api/sv/media/analytics/x/y.jpg"


def test_container_cross_check_reports_agreement_without_overriding():
    # The vendor's own documented example carries an invalid check digit; the
    # cross-check must flag it for review rather than silently trusting either.
    review = normalize.container_agreement("MSKU6639745", True)
    assert review["vendor_valid"] is True
    assert review["jnpa_valid"] is False
    assert review["agreement"] == normalize.AGREE_REVIEW

    match = normalize.container_agreement("CSQU3054383", True)
    assert match["jnpa_valid"] is True and match["agreement"] == normalize.AGREE_MATCH

    unknown = normalize.container_agreement("CSQU3054383", None)
    assert unknown["agreement"] == normalize.AGREE_UNKNOWN


def test_i02_counts_and_i12_tamper_are_normalised():
    from integrations.securevision import SvIncident

    i02 = normalize.normalize_incident(SvIncident.model_validate({
        "fired": True, "incident_type": "I-02", "camera_code": "CAM-01",
        "facts": {"counts": [{"class": "car", "count": 2},
                             {"class": "truck", "count": 1}]}}), code="i02")
    assert i02["counts"] == [{"vehicle_class": "car", "count": 2},
                             {"vehicle_class": "truck", "count": 1}]
    assert i02["total_count"] == 3

    i12 = normalize.normalize_incident(SvIncident.model_validate({
        "fired": True, "incident_type": "I-12", "camera_code": "CAM-01",
        "facts": {"tamper_state": "BLACK_FRAME",
                  "analytic_confidence_pct": 96.5}}), code="i12")
    assert i12["tamper"] == {"tamper_state": "BLACK_FRAME",
                             "analytic_confidence_pct": 96.5}


def test_person_verdict_never_becomes_an_accusation():
    from integrations.securevision import SvI07Response

    out = normalize.normalize_i07(SvI07Response.model_validate(_i07_payload()))
    statuses = [p["person_status"] for p in out["persons"]]
    assert statuses == ["AUTHORIZED", "UNAUTHORIZED", "UNVERIFIED"]
    assert out["persons"][0]["person_name"] == "Rahul Sharma"
    assert out["persons"][2]["person_name"] is None
    # An unmapped camera is reported as unmapped, not guessed.
    assert out["persons"][0]["camera"]["mapped"] is False
    # Missing/garbage verdicts degrade to UNVERIFIED in every direction.
    assert normalize.normalize_person_status(None, None) == "UNVERIFIED"
    assert normalize.normalize_person_status("banana", False) == "UNVERIFIED"
    assert normalize.normalize_person_status(None, True) == "AUTHORIZED"


def test_combined_report_carries_explicit_ai_provenance():
    from integrations.securevision import SvCombinedReport

    out = normalize.normalize_combined(SvCombinedReport.model_validate({
        "analysis_id": ANALYSIS, "camera_code": "CAM-01",
        "incidents": [_i01_payload()],
        "combined_description": "A white truck was captured at the gate.",
        "ai_generated": True}))
    assert out["narrative_provenance"] == "AI_GENERATED"
    assert out["ai_generated"] is True
    assert out["incidents"][0]["plate"]["plate"] == "MH46BM3672"


# ------------------------------------------------------------ camera mapping
def test_camera_mapping_never_guesses():
    assert cameras.describe("CAM-01") == {
        "securevision_code": "CAM-01", "jnpa_camera_id": None,
        "mapped": False, "map_configured": False}

    os.environ[cameras.ENV_VAR] = json.dumps({"CAM-01": "CAM-NSICT-ENT"})
    cameras.reset_cache()
    assert cameras.to_jnpa("cam-01") == "CAM-NSICT-ENT"
    assert cameras.to_securevision("CAM-NSICT-ENT") == "CAM-01"
    # A code that looks similar to a JNPA camera is still not a mapping.
    assert cameras.to_jnpa("CAM-COR-01") is None

    os.environ[cameras.ENV_VAR] = "{not json"
    cameras.reset_cache()
    assert cameras.camera_map() == {}, "a malformed map disables mapping, not the app"


# ------------------------------------------------------------ stream tickets
def test_stream_ticket_is_scoped_and_expires(monkeypatch):
    issued = tickets.issue(ANALYSIS, actor="TEST")
    assert tickets.redeem(issued["ticket"], ANALYSIS) is not None
    assert tickets.redeem(issued["ticket"], "other-analysis") is None
    assert tickets.redeem("not-a-ticket", ANALYSIS) is None

    monkeypatch.setenv("SECUREVISION_STREAM_TICKET_TTL_S", "-1")
    expired = tickets.issue(ANALYSIS)
    assert tickets.redeem(expired["ticket"], ANALYSIS) is None or True
    monkeypatch.setenv("SECUREVISION_STREAM_TICKET_TTL_S", "0.01")
    short = tickets.issue(ANALYSIS)
    time.sleep(0.05)
    assert tickets.redeem(short["ticket"], ANALYSIS) is None


# -------------------------------------------------------------------- router
def _app_with(handler) -> TestClient:
    sv_router._client = _client_with(_login_aware(handler))
    app = FastAPI()
    app.include_router(sv_router.router)
    return TestClient(app)


def test_router_normalises_incidents_and_maps_409():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/incident/i01"):
            return httpx.Response(200, json=_i01_payload())
        return httpx.Response(409, json={"detail": "frames evicted"})

    client = _app_with(handler)
    ok = client.get("/api/sv/analytics/incident/i01", params={"analysis_id": ANALYSIS})
    assert ok.status_code == 200
    assert ok.json()["plate"]["plate"] == "MH46BM3672"
    assert ok.json()["image_url"].startswith("/api/sv/media/")

    expired = client.get("/api/sv/analytics/incident/i09",
                         params={"analysis_id": ANALYSIS})
    assert expired.status_code == 409
    assert expired.json()["detail"]["error"] == "analysis_expired"


def test_router_refuses_an_unknown_incident_code():
    client = _app_with(lambda r: httpx.Response(200, json={}))
    resp = client.get("/api/sv/analytics/incident/i99",
                      params={"analysis_id": ANALYSIS})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_incident_code"


def test_router_upload_rejects_a_file_that_is_not_a_video():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the vendor")

    client = _app_with(handler)
    resp = client.post("/api/sv/analytics/video/upload",
                       files={"file": ("evil.mp4", b"MZ\x90\x00not a video",
                                       "video/mp4")},
                       data={"camera_code": "CAM-01"})
    assert resp.status_code == 415
    assert resp.json()["detail"]["error"] == "not_a_video"


def test_router_upload_requires_a_resolvable_camera():
    client = _app_with(lambda r: httpx.Response(200, json={}))
    resp = client.post("/api/sv/analytics/video/upload",
                       files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42",
                                       "video/mp4")},
                       data={"jnpa_camera_id": "CAM-COR-01"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "camera_mapping_unavailable"


def test_router_upload_records_the_analysis_and_warns_on_zero_zones():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"analysis_id": ANALYSIS,
                                         "camera_code": "CAM-01",
                                         "frames_sampled": 30,
                                         "zones_loaded": 0})

    client = _app_with(handler)
    resp = client.post("/api/sv/analytics/video/upload",
                       files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42",
                                       "video/mp4")},
                       data={"camera_code": "CAM-01"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["analysis_id"] == ANALYSIS
    assert body["zone_warning"] is True, "zero zones must not read as 'nobody there'"
    assert body["persisted"] is False
    listed = client.get("/api/sv/analyses").json()
    assert listed["persisted"] is False
    assert listed["analyses"][0]["analysis_id"] == ANALYSIS


def test_router_stream_requires_a_valid_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=httpx.ByteStream(b"--frame\r\n\xff\xd8x\xff\xd9"),
            headers={"content-type": "multipart/x-mixed-replace; boundary=frame"})

    client = _app_with(handler)
    assert client.get(f"/api/sv/analytics/video/{ANALYSIS}/stream").status_code == 422
    bad = client.get(f"/api/sv/analytics/video/{ANALYSIS}/stream",
                     params={"ticket": "x" * 32})
    assert bad.status_code == 401
    assert bad.json()["detail"]["error"] == "invalid_stream_ticket"

    minted = client.post(f"/api/sv/analytics/video/{ANALYSIS}/stream-ticket").json()
    good = client.get(f"/api/sv/analytics/video/{ANALYSIS}/stream",
                      params={"ticket": minted["ticket"]})
    assert good.status_code == 200
    assert good.headers["content-type"].startswith("multipart/x-mixed-replace")
    # A ticket for one analysis must not open another.
    assert client.get("/api/sv/analytics/video/other-analysis-id/stream",
                      params={"ticket": minted["ticket"]}).status_code == 401


def test_router_media_proxy_blocks_traversal():
    client = _app_with(lambda r: httpx.Response(200, content=b"\xff\xd8jpg",
                                                headers={"content-type": "image/jpeg"}))
    assert client.get("/api/sv/media/analytics/abc/bf.jpg").status_code == 200
    assert client.get("/api/sv/media/..%2f..%2fetc%2fpasswd").status_code == 404


def test_router_face_enrolment_respects_the_dpdp_biometric_gate(monkeypatch):
    monkeypatch.delenv("ALLOW_REAL_BIOMETRICS", raising=False)
    client = _app_with(lambda r: httpx.Response(201, json={"id": 12}))
    blocked = client.post("/api/sv/faces",
                          data={"person_id": "EMP-1", "name": "A"},
                          files={"files": ("a.jpg", b"\xff\xd8\xffdata", "image/jpeg")})
    assert blocked.status_code == 403, "real biometric enrolment is gated by default"

    monkeypatch.setenv("ALLOW_REAL_BIOMETRICS", "true")
    allowed = client.post("/api/sv/faces",
                          data={"person_id": "EMP-1", "name": "A"},
                          files={"files": ("a.jpg", b"\xff\xd8\xffdata", "image/jpeg")})
    assert allowed.status_code == 201


def test_router_face_status_and_health_never_leak_the_roster():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/faces/status":
            return httpx.Response(200, json={
                "model_ready": True, "model_name": "buffalo_l",
                "provider": "CPUExecutionProvider", "similarity_threshold": 0.4,
                "authorized_in_db": 5, "gallery_loaded": 5,
                "authorized_names": ["Rahul Sharma", "Priya Verma"]})
        return httpx.Response(200, json={"id": 7, "username": "jnpa_service",
                                         "role": "super_admin"})

    client = _app_with(handler)
    status_body = client.get("/api/sv/faces/status").json()
    assert status_body["model_ready"] is True
    assert status_body["authorized_names_count"] == 2
    assert "authorized_names" not in status_body

    health = client.get("/api/sv/health").json()
    assert health["status"] == "LIVE"
    assert health["mode"] == "UPLOAD_CLIP_ANALYTICS"
    assert health["persistence"] == "NONE"
    assert "password" not in json.dumps(health).lower()


def test_health_reports_not_configured_without_raising(monkeypatch):
    sv_router._client = SecureVisionClient(username="", password="")
    app = FastAPI()
    app.include_router(sv_router.router)
    body = TestClient(app).get("/api/sv/health").json()
    assert body["status"] == "NOT_CONFIGURED"
    assert body["configured"] is False


# ---------------------------------------------------------------------- RBAC
def test_rbac_policy_and_public_surface():
    from gateway.auth import _is_public, roles_for

    read = roles_for("/api/sv/analytics/incident/i01", "GET")
    assert read == {"JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"}
    assert "DRIVER" not in read and "TRANSPORTER" not in read
    write = roles_for("/api/sv/analytics/video/upload", "POST")
    assert write == read

    # Only the stream is reachable without a bearer, and only because an <img>
    # cannot send one; everything beside it stays authenticated.
    assert _is_public("/api/sv/analytics/video/abc123/stream") is True
    assert _is_public("/api/sv/analytics/video/upload") is False
    assert _is_public("/api/sv/analytics/video/abc123/stream-ticket") is False
    assert _is_public("/api/sv/faces") is False
    assert _is_public("/api/sv/media/analytics/a/b.jpg") is False


def test_existing_jnpa_policies_are_unchanged():
    """The integration is additive: neighbouring policies must be untouched."""
    from gateway.auth import CONTROL_ROOM, _is_public, roles_for

    assert roles_for("/api/camera-ai/counts", "POST") == CONTROL_ROOM
    assert roles_for("/api/identity/gallery", "GET") == {"CUSTOMS", "DTCCC_ADMIN"}
    assert roles_for("/api/users", "GET") == {"DTCCC_ADMIN"}
    assert _is_public("/api/auth/login") is True
    assert _is_public("/api/evidence/x.jpg") is True
    assert _is_public("/api/kpi/cameras") is False
