"""Regression tests for the four workflow-audit findings (WORKFLOW_AUDIT_REPORT.md).

Each test pins the SYMPTOM the audit observed, so a future change that reintroduces
the bug fails with the operational consequence rather than an abstract assertion.

  T-1  POST /api/trucks/{id}/route called the truck-sim FIRST and raised 502 on a
       connect error, so the advisory never reached core.reroute_advisory, no
       decision was audited and the driver was never notified — the whole
       driver-advisory workflow was lost because a simulator was down.
  T-2  POST /api/trucks/{id}/route/ack answered {"acked": true} unconditionally and
       wrote a REROUTE_ACK decision first, fabricating a push -> driver -> ACK
       round-trip for a device that had never been pushed anything.
  G-1  A duplicate upload reported `imported: N` (the ORIGINAL import's count)
       while persisting nothing, so an operator read it as N more rows landing.
       Applies to BOTH upload services (gate documents + CFS-ECY).
  G-2  core.gate_capture carried no evidence object reference, so
       GET /api/evidence/{object_path} had nothing in the DB to resolve.

Follows the existing in-memory-fake style of tests/test_demo_failure_fixes.py —
no database, no network.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest
from fastapi import HTTPException


class _FakeCfg:
    truck_api_url = "http://truck-sim:8000"
    port = 8000
    postgres_dsn = None
    gate_boom_delay_s = 30


class _FakeWs:
    def __init__(self) -> None:
        self.frames: list[tuple[str, Any, Optional[str]]] = []

    async def broadcast(self, type_, payload, *, device_id=None):
        self.frames.append((type_, payload, device_id))


class _FakeAdvisoryRepo:
    """Mirrors AdvisoryRepository's contract: save() upserts, ack() reports whether
    a row was actually updated (the return value the router used to discard)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.saves = 0

    async def save(self, device_id, advisory):
        self.rows[device_id] = {**dict(advisory), "ack_state": None}
        self.saves += 1
        return True

    async def ack(self, device_id, state_val):
        if device_id in self.rows:
            self.rows[device_id]["ack_state"] = state_val
            return True
        return False

    async def latest(self, device_id):
        return self.rows.get(device_id)


class _DeadHttp:
    """A truck-sim that is not there — the exact condition that lost the advisory."""

    async def post(self, url, **kw):
        raise httpx.ConnectError("nodename nor servname provided, or not known")


class _LiveHttp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def post(self, url, **kw):
        self.calls.append(url)
        return httpx.Response(200, json=self._payload)


class _Gw:
    def __init__(self, http) -> None:
        self.http = http
        self.cfg = _FakeCfg()
        self.ws = _FakeWs()
        self.decisions: list[dict] = []

    async def record_decision(self, **kw):
        self.decisions.append(kw)


@pytest.fixture()
def trucks_router(monkeypatch):
    """The trucks router with its advisory repo + notification fan-out faked out."""
    from gateway.routers import trucks

    repo = _FakeAdvisoryRepo()
    monkeypatch.setattr(trucks, "_advisory_repo", lambda gw: repo)
    trucks.LAST_REROUTE.clear()

    class _Fanout:
        webpush = False

        def as_dict(self):
            return {"ws": True, "webpush": False, "fcm": False}

    async def _dispatch(gw, device_id, advisory, ws_type=None):
        return _Fanout()

    from gateway import notifications
    monkeypatch.setattr(notifications, "dispatch", _dispatch)
    return trucks, repo


# ------------------------------------------------------------------ T-1
@pytest.mark.asyncio
async def test_reroute_is_persisted_when_the_truck_sim_is_down(trucks_router):
    """T-1: a dead truck-sim must NOT lose the advisory.

    The audit observed: POST .../route -> 502 truck_sim_unreachable, and
    core.reroute_advisory rows = 0. The driver was never told to re-route.
    """
    trucks, repo = trucks_router
    gw = _Gw(_DeadHttp())

    out = await trucks.reroute_truck(
        "TRK-000001", body={"gate_id": "GATE-3", "reason": "congestion"}, gw=gw)

    # The workflow completed rather than 502'ing.
    assert out["persisted"] is True
    assert out["sim"] == {"delivered": False, "error": "truck_sim_unreachable"}
    assert out["decision_path"] == "REROUTE_DEGRADED"

    # The advisory is durable — this is the row that used to be missing entirely.
    assert "TRK-000001" in repo.rows
    assert repo.rows["TRK-000001"]["gate_id"] == "GATE-3"
    # ...and reachable through the PWA's polling fallback.
    assert trucks.LAST_REROUTE["TRK-000001"]["gate_id"] == "GATE-3"

    # The decision is audited, flagged as degraded rather than silently dropped.
    assert len(gw.decisions) == 1
    assert gw.decisions[0]["decision_path"] == "REROUTE_DEGRADED"


@pytest.mark.asyncio
async def test_reroute_still_enriches_from_a_live_truck_sim(trucks_router):
    """T-1 must not regress the happy path: a reachable sim still enriches the
    advisory with dest/route_km and the decision stays LIVE."""
    trucks, repo = trucks_router
    http = _LiveHttp({"dest": "GATE-3", "route_km": 4.2})
    gw = _Gw(http)

    out = await trucks.reroute_truck("TRK-000002", body={"gate_id": "GATE-3"}, gw=gw)

    assert http.calls, "the truck-sim must still be called"
    assert out["sim"]["delivered"] is True
    assert out["decision_path"] == "REROUTE"
    assert out["advisory"]["route_km"] == 4.2
    assert out["advisory"]["dest"] == "GATE-3"
    assert repo.rows["TRK-000002"]["route_km"] == 4.2
    assert gw.decisions[0]["decision_path"] == "REROUTE"


# ------------------------------------------------------------------ T-2
@pytest.mark.asyncio
async def test_ack_without_an_advisory_is_rejected(trucks_router):
    """T-2: ACK for a device that was never pushed anything must not claim success.

    The audit observed 200 {"acked": true} against zero advisory rows, plus a
    REROUTE_ACK decision-audit entry for a round-trip that never happened.
    """
    trucks, repo = trucks_router
    gw = _Gw(_DeadHttp())

    with pytest.raises(HTTPException) as excinfo:
        await trucks.ack_reroute("QA-GHOST-DEVICE-0001", body={"state": "ACK"}, gw=gw)

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["error"] == "no_advisory_to_ack"
    assert excinfo.value.detail["acked"] is False
    # No phantom audit entry, and no ACK frame on anyone's socket.
    assert gw.decisions == []
    assert gw.ws.frames == []


@pytest.mark.asyncio
async def test_ack_succeeds_for_a_real_advisory(trucks_router):
    """T-2 must not break the legitimate ACK: push then ack still works and is
    audited exactly once."""
    trucks, repo = trucks_router
    gw = _Gw(_DeadHttp())

    await trucks.reroute_truck("TRK-000003", body={"gate_id": "GATE-1"}, gw=gw)
    gw.decisions.clear()

    out = await trucks.ack_reroute("TRK-000003", body={"state": "ACK"}, gw=gw)

    assert out["acked"] is True
    assert out["state"] == "ACK"
    assert repo.rows["TRK-000003"]["ack_state"] == "ACK"
    assert len(gw.decisions) == 1
    assert gw.decisions[0]["decision_path"] == "REROUTE_ACK"
    # Addressed to the acking driver only (pre-existing guarantee, kept).
    assert gw.ws.frames[0][2] == "TRK-000003"


# ------------------------------------------------------------------ G-1
class _DupRepo:
    """Repository that reports a duplicate file, echoing the ORIGINAL import's
    imported_count — exactly what the real repositories return."""

    def __init__(self, original_count: int) -> None:
        self._n = original_count

    async def persist(self, *a, **kw):
        return {"file_id": 5, "import_status": "SKIPPED_DUPLICATE",
                "imported_count": self._n, "duplicate": True, "duplicate_count": 0}

    async def record_upload(self, *a, **kw):
        return {"file_id": 5, "import_status": "SKIPPED_DUPLICATE",
                "imported_count": self._n, "duplicate": True, "duplicate_count": 0}

    async def add_row_errors(self, *a, **kw):
        return None

    async def mark_partial(self, *a, **kw):
        return None


_EIR_CSV = (
    "EIR No,Terminal,Container No,Vessel,VIA,Seal No,BAT,Truck No,Driver Name,"
    "Driver Licence,Truck In,Truck Out,Company,From CFS,To CFS,Scanner Stamp,Remarks\n"
    "4339869,PSA BMCT,MSMU1908508,SAV,S0696,EU31716082,B723,MH43BX1488,BABALU KUMAR,"
    "UP6420140008203,06/06/2026 08:26,06/06/2026 11:11,TRANSTA,CLP CFS,,SCANNED CLEAN,\n"
).encode()

_CFS_CSV = (
    "Container Number,Timestamp,Mode,Facility\n"
    "ONEU2122848,05/08/2026 09:00,In,CFS\n"
).encode()


@pytest.mark.asyncio
async def test_gate_doc_duplicate_upload_reports_zero_imported():
    """G-1: a SKIPPED_DUPLICATE persisted nothing, so `imported` must be 0."""
    from services.gate_documents.service import GateDocumentService

    svc = GateDocumentService(repository=_DupRepo(original_count=7))
    out = await svc.import_file("EIR", _EIR_CSV, "dup.csv", "qa")

    assert out["status"] == "SKIPPED_DUPLICATE"
    assert out["duplicate_file"] is True
    assert out["imported"] == 0, "a duplicate upload imports nothing"
    # The original import's count stays visible, just not as `imported`.
    assert out["previously_imported"] == 7


@pytest.mark.asyncio
async def test_cfs_ecy_duplicate_upload_reports_zero_imported():
    """G-1 (CFS-ECY half, audit finding E-1): same contract, same fix."""
    from services.cfs_ecy.upload_service import CfsEcyUploadService

    svc = CfsEcyUploadService(repository=_DupRepo(original_count=2))
    out = await svc.import_file("CFS", _CFS_CSV, "dup.csv", "qa")

    assert out["status"] == "SKIPPED_DUPLICATE"
    assert out["duplicate_file"] is True
    assert out["imported"] == 0
    assert out["previously_imported"] == 2


@pytest.mark.asyncio
async def test_fresh_upload_still_reports_its_real_imported_count():
    """G-1 must not zero out a genuine import."""
    from services.gate_documents.service import GateDocumentService

    class _FreshRepo(_DupRepo):
        async def persist(self, *a, **kw):
            return {"file_id": 6, "import_status": "SUCCESS", "imported_count": 1,
                    "duplicate": False, "duplicate_count": 0}

    svc = GateDocumentService(repository=_FreshRepo(original_count=1))
    out = await svc.import_file("EIR", _EIR_CSV, "fresh.csv", "qa")

    assert out["status"] == "SUCCESS"
    assert out["imported"] == 1
    assert "previously_imported" not in out


# ------------------------------------------------------------------ G-2
def test_evidence_uri_is_the_gateway_proxy_path():
    """G-2: the stored reference must be resolvable by GET /api/evidence/{path}.

    An internal ``s3://bucket/key`` URI is unreachable from a browser; the gateway
    proxies the private bucket at /api/evidence/{object_path}.
    """
    from services.gate_documents.repository import evidence_uri_for

    assert evidence_uri_for("form13/F13000001.jpg") == "/api/evidence/form13/F13000001.jpg"
    assert evidence_uri_for("/form13/F13000001.jpg") == "/api/evidence/form13/F13000001.jpg"
    assert evidence_uri_for(None) is None
    assert evidence_uri_for("") is None


def test_form13_insert_carries_the_evidence_reference():
    """G-2: the parsed object key reaches core.gate_capture's new columns."""
    from services.gate_documents.repository import _form13_params, _FORM13_INSERT

    for col in ("object_path", "evidence_uri", "object_name"):
        assert col in _FORM13_INSERT, f"{col} must be persisted by the Form-13 insert"

    params = _form13_params(
        {"container_number": "FFAU4770682", "vehicle_no": "MH43BX1488",
         "in_gate": "IGTK01", "object_path": "form13/F13000001.jpg"},
        import_file_id=1)
    assert params["object_path"] == "form13/F13000001.jpg"
    assert params["evidence_uri"] == "/api/evidence/form13/F13000001.jpg"


def test_form13_without_evidence_stays_valid():
    """G-2 is additive: a document-only Form-13 keeps NULL references."""
    from services.gate_documents.repository import _form13_params

    params = _form13_params(
        {"container_number": "FFAU4770682", "vehicle_no": "MH43BX1488"},
        import_file_id=1)
    assert params["object_path"] is None
    assert params["evidence_uri"] is None


def test_form13_parser_picks_up_an_evidence_column():
    """G-2: an optional evidence column is recognised without changing the template."""
    from services.gate_documents import upload_parsers as P

    header = ["Form13 No", "Vehicle No", "Container No", "Image File"]
    rows = [{"Form13 No": "F13000000001", "Vehicle No": "MH43BX1488",
             "Container No": "FFAU4770682", "Image File": "form13/F13000001.jpg"}]
    res = P.parse(header, rows, doc_type="FORM13", source_file="qa.csv")

    assert res.records, "the row must parse"
    assert res.records[0]["object_path"] == "form13/F13000001.jpg"

    # ...and the template is unchanged (the column stays optional).
    assert "Image File" not in P.template_csv("FORM13").splitlines()[0]


def test_evidence_router_searches_both_buckets():
    """G-2: OCR documents land in the `documents` bucket; violation frames in
    `evidence`. Both persist an /api/evidence/ reference, so the route must
    resolve either."""
    from gateway.routers.evidence import _buckets

    buckets = _buckets()
    assert "evidence" in buckets
    assert "documents" in buckets
    assert len(buckets) == len(set(buckets)), "no duplicate bucket lookups"


# ------------------------------------------------------------------ T-4 / T-5
# Transport fallback chains: the two Transport steps that used to be
# "environment-gated" were in fact MISSING RUNGS. The single-device read
# implemented PRIMARY -> SECONDARY -> TERTIARY; the fleet LIST and the
# model-performance metrics had no fallback at all, so a stopped container
# blanked them. Both rungs serve REAL data (the persisted telemetry tail /
# the committed training artifact) — never a synthesised stand-in.
class _RdsGw(_Gw):
    """Gateway whose truck-sim is dead but whose RDS DSN is set."""

    def __init__(self, http, dsn="postgresql+asyncpg://x:x@127.0.0.1:1/none"):
        super().__init__(http)
        self.cfg = type("_Cfg", (), {
            "truck_api_url": "http://truck-sim:8000", "port": 8000,
            "postgres_dsn": dsn, "gate_boom_delay_s": 30,
            "congestion_url": "http://congestion:8311"})()


class _DeadListHttp:
    async def get(self, url, **kw):
        raise httpx.ConnectError("nodename nor servname provided, or not known")

    async def post(self, url, **kw):
        raise httpx.ConnectError("nodename nor servname provided, or not known")


@pytest.mark.asyncio
async def test_truck_list_falls_back_to_the_rds_telemetry_tail(monkeypatch):
    """T-4: a dead truck-sim must not blank the fleet list.

    Observed before: GET /api/trucks -> {"count":0,"devices":[],"degraded":true}
    even though core.truck_telemetry held current positions.
    """
    from gateway.routers import trucks

    rows = [{"device_id": "TRK-000001", "plate": "MH04AB1234", "lat": 18.9,
             "lon": 72.9, "speed_kmh": 41.0, "heading": 90.0, "ts": None}]

    async def _fake_fetch_all(sql, params=None, dsn=None):
        assert "core.truck_telemetry" in sql
        return rows

    import jnpa_shared.db as _db
    monkeypatch.setattr(_db, "fetch_all", _fake_fetch_all)

    gw = _RdsGw(_DeadListHttp())
    out = await trucks.list_trucks(state=None, limit=50, gw=gw)

    assert out["decision_path"] == "SECONDARY"
    assert out["source"] == "rds-telemetry"
    assert out["count"] == 1
    assert out["devices"][0]["device_id"] == "TRK-000001"
    assert out["devices"][0]["plate"] == "MH04AB1234"
    assert gw.decisions[0]["decision_path"] == "SECONDARY"


@pytest.mark.asyncio
async def test_truck_list_falls_back_to_checkins_when_rds_is_empty(monkeypatch):
    """T-4: TERTIARY rung — the manual web check-ins already in memory."""
    from gateway.routers import trucks

    async def _empty(sql, params=None, dsn=None):
        return []

    import jnpa_shared.db as _db
    monkeypatch.setattr(_db, "fetch_all", _empty)
    trucks.CHECKINS.clear()
    trucks.CHECKINS["TRK-000009"] = {"plate": "MH04QA0009", "lat": 18.95,
                                     "lon": 72.94, "submitted_at": "2026-08-05T10:00:00Z"}
    try:
        gw = _RdsGw(_DeadListHttp())
        out = await trucks.list_trucks(state=None, limit=50, gw=gw)

        assert out["decision_path"] == "TERTIARY"
        assert out["source"] == "web-checkin"
        assert out["devices"][0]["elevated_scrutiny"] is True
    finally:
        trucks.CHECKINS.clear()


@pytest.mark.asyncio
async def test_truck_list_state_filter_never_answers_from_a_fallback(monkeypatch):
    """T-4 honesty guard: only the truck-sim knows TruckState.

    Returning the UNFILTERED fleet for `state=AT_GATE_QUEUE` would answer a
    different question than the one asked, so the fallback rungs are skipped and
    the response says the filter could not be applied.
    """
    from gateway.routers import trucks

    called = False

    async def _should_not_run(sql, params=None, dsn=None):
        nonlocal called
        called = True
        return [{"device_id": "X", "plate": "P", "lat": 1.0, "lon": 2.0,
                 "speed_kmh": 0.0, "heading": 0.0, "ts": None}]

    import jnpa_shared.db as _db
    monkeypatch.setattr(_db, "fetch_all", _should_not_run)

    out = await trucks.list_trucks(state="AT_GATE_QUEUE", limit=50,
                                   gw=_RdsGw(_DeadListHttp()))

    assert out["count"] == 0
    assert out["state_filter_supported"] is False
    assert out["filter_state"] == "AT_GATE_QUEUE"
    assert called is False, "a state-filtered query must not be served from RDS"


def test_congestion_metrics_artifact_is_the_real_committed_file():
    """T-5: the LOCAL_ARTIFACT rung reads ai/congestion/artifacts/metrics.json —
    the SAME file ai/congestion's GET /metrics serves, not invented numbers."""
    import json
    from pathlib import Path
    from gateway.routers.traffic import _congestion_metrics_artifact

    repo_root = Path(__file__).resolve().parents[1]
    on_disk = json.loads(
        (repo_root / "ai/congestion/artifacts/metrics.json").read_text())

    artifact = _congestion_metrics_artifact()
    assert artifact is not None
    # Byte-for-byte the same evidence, not a regenerated approximation.
    assert artifact == on_disk
    assert artifact["congestion_onset_f1"] == on_disk["congestion_onset_f1"]


def test_congestion_metrics_artifact_returns_none_when_absent(monkeypatch, tmp_path):
    """T-5 honesty guard: no artifact -> None, so the caller still 503s.

    Model-performance numbers are evidential; the fallback must never fabricate
    them when the file is genuinely missing.
    """
    from gateway.routers import traffic

    monkeypatch.setenv("CONGESTION_METRICS_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(traffic, "_CONGESTION_METRICS_DEFAULT",
                        "does/not/exist/metrics.json")
    assert traffic._congestion_metrics_artifact() is None


@pytest.mark.asyncio
async def test_congestion_metrics_prefers_the_live_service(monkeypatch):
    """T-5 must not regress the happy path: a reachable ai/congestion still wins
    and is labelled LIVE."""
    from gateway.routers import traffic

    class _LiveGet:
        async def get(self, url, **kw):
            return httpx.Response(200, json={"congestion_onset_f1": 0.9,
                                             "precision": 0.9, "recall": 0.9,
                                             "num_segments": 13,
                                             "support_total": 100})

    state = _RdsGw(_LiveGet())
    out = await traffic._congestion_metrics(state)

    assert out["decision_path"] == "LIVE"
    assert out["source"] == "ai/congestion"
    assert out["f1"] == 0.9


@pytest.mark.asyncio
async def test_congestion_metrics_degrades_to_the_artifact(monkeypatch):
    """T-5: a dead ai/congestion serves the artifact, clearly labelled so a
    cached read can never be mistaken for a live one."""
    from gateway.routers import traffic

    out = await traffic._congestion_metrics(_RdsGw(_DeadListHttp()))

    assert out["decision_path"] == "LOCAL_ARTIFACT"
    assert out["live_service_available"] is False
    assert out["f1"] == out["congestion_onset_f1"]
    assert out["metrics_synthetic"] is False
