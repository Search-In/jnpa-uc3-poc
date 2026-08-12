"""Video Analytics history — durability, paging and the personal-data boundary.

Regression cover for the reported defect "video analytics history is not there,
all logs should be displayed": the history lived in an in-process OrderedDict,
so it was empty after any gateway/container/worker restart and invisible to a
second worker.

The durable store (``core.video_analysis``, migration 0143) is exercised through
a fake repository that behaves like the table — the SQL itself is covered by the
schema test below and by the live-DB check in the verification report. What
matters here is the SERVICE contract every screen depends on:

  * an upload is persisted, and the response says so;
  * the history survives the service object being thrown away and rebuilt
    (the restart / second-worker case);
  * paging returns the right slice, newest first, with a whole-history total;
  * a store that cannot be read is reported as DEGRADED, never as an empty
    archive;
  * no face/biometric/person field is ever written.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.securevision import analyses  # noqa: E402
from services.securevision.history import VideoAnalysisHistory  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeRepo:
    """In-memory stand-in for core.video_analysis with the same semantics.

    Deliberately shared across VideoAnalysisHistory instances in the restart
    test: the table is what outlives the process, so the fake must too.
    """

    def __init__(self, *, enabled: bool = True, fail_reads: bool = False) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.enabled = enabled
        self.fail_reads = fail_reads

    async def record(self, entry: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        self.rows[entry["analysis_id"]] = dict(entry, deleted_at=None, persisted=True)
        return True

    async def soft_delete(self, analysis_id: str) -> bool:
        row = self.rows.get(analysis_id)
        if not row or row.get("deleted_at"):
            return False
        row["deleted_at"] = "2026-08-12T00:00:00+00:00"
        row["status"] = "DELETED"
        return True

    async def recent(self, *, limit: int, offset: int, include_deleted: bool = False,
                     jnpa_camera_id: Optional[str] = None,
                     ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        if self.fail_reads or not self.enabled:
            return None
        rows = [r for r in self.rows.values() if include_deleted or not r.get("deleted_at")]
        if jnpa_camera_id:
            rows = [r for r in rows if r.get("jnpa_camera_id") == jnpa_camera_id]
        rows.sort(key=lambda r: r["uploaded_at"], reverse=True)
        return rows[offset:offset + limit], len(rows)

    async def get(self, analysis_id: str):
        return self.rows.get(analysis_id)


def _upload(history: VideoAnalysisHistory, analysis_id: str, **over: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = dict(
        securevision_camera_code="CAM-01",
        jnpa_camera_id="CAM-COR-01",
        filename=f"{analysis_id}.mp4",
        frames_sampled=120,
        detection_pass_count=1,
        zones_loaded=3,
        uploaded_by="DTCCC_ADMIN",
        status="COMPLETED",
        processing_ms=4200,
        source="securevision",
    )
    fields.update(over)
    return _run(history.record(analysis_id, **fields))


@pytest.fixture(autouse=True)
def _clean_registry():
    analyses.reset()
    yield
    analyses.reset()


# --------------------------------------------------------------- persistence
def test_an_upload_is_persisted_and_reported_as_persisted():
    repo = FakeRepo()
    history = VideoAnalysisHistory(repository=repo)

    entry = _upload(history, "aaaa111122223333")

    assert entry["persisted"] is True
    assert repo.rows["aaaa111122223333"]["filename"] == "aaaa111122223333.mp4"
    assert repo.rows["aaaa111122223333"]["processing_ms"] == 4200


def test_history_survives_the_service_being_recreated():
    """The restart case: a NEW service over the SAME store still sees the rows.

    This is the actual reported bug — the old registry was per-process, so this
    assertion failed by construction.
    """
    repo = FakeRepo()
    _upload(VideoAnalysisHistory(repository=repo), "bbbb111122223333")

    # Simulate the restart: drop the process cache and build a fresh service.
    analyses.reset()
    reborn = VideoAnalysisHistory(repository=repo)
    page = _run(reborn.recent())

    assert page["persisted"] is True
    assert page["degraded"] is False
    assert [a["analysis_id"] for a in page["analyses"]] == ["bbbb111122223333"]


def test_history_is_newest_first_and_paginates_over_the_whole_store():
    repo = FakeRepo()
    history = VideoAnalysisHistory(repository=repo)
    for i in range(5):
        _upload(history, f"cccc00000000000{i}", uploaded_at=None)
        # Deterministic ordering without sleeping: stamp the stored row.
        repo.rows[f"cccc00000000000{i}"]["uploaded_at"] = f"2026-08-0{i + 1}T10:00:00+00:00"

    first = _run(history.recent(limit=2, offset=0))
    second = _run(history.recent(limit=2, offset=2))

    assert first["total"] == 5 and second["total"] == 5
    assert [a["analysis_id"] for a in first["analyses"]] == ["cccc000000000004",
                                                            "cccc000000000003"]
    assert [a["analysis_id"] for a in second["analyses"]] == ["cccc000000000002",
                                                             "cccc000000000001"]
    # Every page is reachable: 5 rows over pages of 2 = 3 pages, none dropped.
    third = _run(history.recent(limit=2, offset=4))
    assert [a["analysis_id"] for a in third["analyses"]] == ["cccc000000000000"]


def test_delete_is_soft_and_leaves_the_audit_trail():
    repo = FakeRepo()
    history = VideoAnalysisHistory(repository=repo)
    _upload(history, "dddd111122223333")

    _run(history.forget("dddd111122223333"))

    listed = _run(history.recent())
    assert listed["analyses"] == []          # hidden from the workbench…
    assert repo.rows["dddd111122223333"]["deleted_at"] is not None  # …but recorded
    assert repo.rows["dddd111122223333"]["status"] == "DELETED"


# ------------------------------------------------------------- failure modes
def test_an_unreadable_store_is_degraded_not_empty():
    """A store that cannot be read must never be reported as "no history"."""
    repo = FakeRepo(fail_reads=True)
    history = VideoAnalysisHistory(repository=repo)
    _upload(history, "eeee111122223333")

    page = _run(history.recent())

    assert page["degraded"] is True
    assert page["persisted"] is False
    assert page["source"] == "process-cache"
    assert "unavailable" in page["note"].lower()
    # The process cache still answers, so the operator is not left blind.
    assert [a["analysis_id"] for a in page["analyses"]] == ["eeee111122223333"]


def test_empty_history_is_a_genuine_empty_state_not_an_error():
    page = _run(VideoAnalysisHistory(repository=FakeRepo()).recent())
    assert page["analyses"] == []
    assert page["count"] == 0
    assert page["total"] == 0
    assert page["persisted"] is True
    assert page["degraded"] is False


def test_no_store_configured_is_reported_without_claiming_persistence():
    repo = FakeRepo(enabled=False)
    history = VideoAnalysisHistory(repository=repo)
    _upload(history, "ffff111122223333")

    page = _run(history.recent())
    assert page["persisted"] is False
    assert page["degraded"] is False          # nothing is broken — none is configured
    assert "no history store is configured" in page["note"].lower()


# ------------------------------------------------- personal-data boundary
#: Anything matching these must never reach the durable store.
_FORBIDDEN_FIELD = re.compile(
    r"face|embedding|biometric|person_name|similarity|photo|image|descriptor",
    re.IGNORECASE,
)


def test_no_face_or_person_field_is_ever_persisted():
    repo = FakeRepo()
    history = VideoAnalysisHistory(repository=repo)

    # Even when a caller tries to smuggle person data through, it is not stored:
    # record() only forwards the operational field set.
    _upload(
        history,
        "9999111122223333",
        face_embedding=[0.1, 0.2, 0.3],
        person_name="A. Person",
        face_similarity=0.94,
        photo_b64="ZmFrZQ==",
    )

    stored = repo.rows["9999111122223333"]
    offending = [k for k in stored if _FORBIDDEN_FIELD.search(k)]
    assert offending == [], f"personal-data fields reached the store: {offending}"
    assert set(stored) <= {
        "analysis_id", "securevision_camera_code", "jnpa_camera_id", "camera_mapped",
        "filename", "frames_sampled", "detection_pass_count", "zones_loaded",
        "status", "processing_ms", "source", "uploaded_by", "uploaded_at",
        "deleted_at", "persisted",
    }


def test_the_migration_defines_no_person_or_biometric_column():
    """The boundary is enforced by the SCHEMA, not only by the write path."""
    sql = (REPO_ROOT / "infra/postgres/v3/0143_video_analysis_history.sql").read_text()
    body = sql[sql.index("CREATE TABLE"):sql.index("COMMENT ON TABLE")]
    for line in body.splitlines():
        column = line.strip().split(" ")[0]
        assert not _FORBIDDEN_FIELD.search(column), f"forbidden column: {column}"


# ------------------------------------------------- uploaded_at / asyncpg bind
# Production defect: POST /api/sv/analytics/video/upload answered 201 while the
# INSERT died inside the best-effort except with
#
#   asyncpg.exceptions.DataError: invalid input for query argument $13:
#   '2026-08-12T20:25:06.601876+00:00'
#   expected a datetime.date or datetime.datetime instance, got 'str'
#
# The in-process cache stamps uploaded_at as an ISO STRING and the history
# service forwards that entry to the repository unchanged; asyncpg binds by
# Python type, so every upload was silently lost. A fake repository cannot catch
# this — the assertions below run the REAL repository against an engine that
# reproduces the driver's type check.
from datetime import date as date_type  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from services.securevision import repository as sv_repo  # noqa: E402
from services.securevision.repository import (  # noqa: E402
    VideoAnalysisRepository,
    _as_utc_datetime,
)

_DSN = "postgresql+asyncpg://stub/stub"


class StrictAsyncpgEngine:
    """Engine stand-in that rejects a bind asyncpg would reject.

    Mirrors the driver's parameter encoding: a timestamptz argument must be a
    real ``datetime``/``date``, never a string. Because
    ``VideoAnalysisRepository.record`` swallows write errors by design, a
    ``record()`` that returns True IS the proof that the bind was acceptable.
    """

    def __init__(self) -> None:
        self.statements: List[str] = []
        self.params: List[Dict[str, Any]] = []

    # engine.begin() -> async context manager yielding a connection
    def begin(self) -> "StrictAsyncpgEngine":
        return self

    async def __aenter__(self) -> "StrictAsyncpgEngine":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, statement: Any, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        for key in ("uploaded_at", "deleted_at"):
            value = params.get(key)
            if value is not None and not isinstance(value, (datetime, date_type)):
                raise TypeError(
                    f"invalid input for query argument {key!r}: {value!r} "
                    "expected a datetime.date or datetime.datetime instance, "
                    f"got {type(value).__name__!r}")
        self.statements.append(str(statement))
        self.params.append(dict(params))
        return None


@pytest.fixture()
def engine(monkeypatch) -> StrictAsyncpgEngine:
    stub = StrictAsyncpgEngine()
    monkeypatch.setattr(sv_repo, "get_engine", lambda dsn: stub)
    return stub


def _entry(analysis_id: str, **over: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "analysis_id": analysis_id,
        "securevision_camera_code": "CAM-01",
        "jnpa_camera_id": "CAM-COR-01",
        "camera_mapped": True,
        "filename": f"{analysis_id}.mp4",
        "frames_sampled": 120,
        "detection_pass_count": 1,
        "zones_loaded": 3,
        "status": "COMPLETED",
        "processing_ms": 4200,
        "source": "securevision",
        "uploaded_by": "DTCCC_ADMIN",
    }
    entry.update(over)
    return entry


def test_iso_string_uploaded_at_persists(engine):
    """The exact shape the upload path produces — the reported failure."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    ok = _run(repo.record(_entry("iso1111122223333",
                                 uploaded_at="2026-08-12T20:25:06.601876+00:00")))

    assert ok is True                       # the write was NOT swallowed
    bound = engine.params[0]["uploaded_at"]
    assert isinstance(bound, datetime)      # a datetime reached the driver…
    assert bound.tzinfo is not None         # …carrying its zone…
    assert bound == datetime(2026, 8, 12, 20, 25, 6, 601876, tzinfo=timezone.utc)


def test_timezone_aware_datetime_uploaded_at_persists(engine):
    """Callers that already hold a datetime keep working, unconverted in value."""
    repo = VideoAnalysisRepository(dsn=_DSN)
    ist = timezone(timedelta(hours=5, minutes=30))          # Asia/Kolkata
    stamped = datetime(2026, 8, 13, 1, 55, 6, tzinfo=ist)

    ok = _run(repo.record(_entry("dt11111122223333", uploaded_at=stamped)))

    assert ok is True
    bound = engine.params[0]["uploaded_at"]
    assert isinstance(bound, datetime)
    # Normalised to UTC, but the same INSTANT — no silent shift of the clip time.
    assert bound.utcoffset() == timedelta(0)
    assert bound == stamped


def test_naive_datetime_is_read_as_utc(engine):
    """Everything upstream stamps UTC, so a naive value is UTC — not local time."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    ok = _run(repo.record(_entry("naive11122223333",
                                 uploaded_at=datetime(2026, 8, 12, 20, 25, 6))))

    assert ok is True
    assert engine.params[0]["uploaded_at"] == datetime(
        2026, 8, 12, 20, 25, 6, tzinfo=timezone.utc)


def test_missing_uploaded_at_leaves_the_database_default_in_charge(engine):
    """None must stay None so the INSERT's COALESCE(..., now()) applies."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    assert _run(repo.record(_entry("null1111122223333", uploaded_at=None))) is True
    # …and a caller that omits the key entirely behaves identically.
    absent = _entry("miss1111122223333")
    absent.pop("uploaded_at", None)
    assert _run(repo.record(absent)) is True

    assert engine.params[0]["uploaded_at"] is None
    assert engine.params[1]["uploaded_at"] is None
    # The fallback is the DATABASE's clock, not a value invented here.
    assert "COALESCE(CAST(:uploaded_at AS timestamptz), now())" in engine.statements[0]


def test_an_unreadable_timestamp_falls_back_to_now_instead_of_losing_the_row(engine):
    """A timestamp we cannot parse is not worth dropping the whole analysis for."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    assert _run(repo.record(_entry("junk1111122223333",
                                   uploaded_at="not-a-timestamp"))) is True
    assert engine.params[0]["uploaded_at"] is None


def test_reupload_of_an_existing_analysis_id_still_upserts(engine):
    """ON CONFLICT (analysis_id) behaviour is unchanged by the coercion."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    first = _run(repo.record(_entry("dupe1111122223333", status="COMPLETED",
                                    uploaded_at="2026-08-12T20:25:06+00:00")))
    second = _run(repo.record(_entry("dupe1111122223333", status="FAILED",
                                     uploaded_at=datetime(2026, 8, 12, 21, 0,
                                                          tzinfo=timezone.utc))))

    assert first is True and second is True
    sql = engine.statements[1]
    assert "ON CONFLICT (analysis_id) DO UPDATE SET" in sql
    assert "uploaded_at              = EXCLUDED.uploaded_at" in sql
    assert "deleted_at               = NULL" in sql          # a re-upload undeletes
    assert engine.params[1]["status"] == "FAILED"
    assert isinstance(engine.params[1]["uploaded_at"], datetime)


def test_no_personal_field_reaches_the_insert_parameters(engine):
    """The coercion did not widen what is written: operational metadata only."""
    repo = VideoAnalysisRepository(dsn=_DSN)

    _run(repo.record(_entry(
        "priv1111122223333",
        uploaded_at="2026-08-12T20:25:06+00:00",
        face_embedding=[0.1, 0.2], person_name="A. Person",
        face_similarity=0.94, photo_b64="ZmFrZQ==", image_url="s3://clip.jpg",
    )))

    bound = engine.params[0]
    assert [k for k in bound if _FORBIDDEN_FIELD.search(k)] == []
    assert set(bound) == {
        "analysis_id", "sv_code", "jnpa_camera_id", "camera_mapped", "filename",
        "frames_sampled", "detection_pass_count", "zones_loaded", "status",
        "processing_ms", "source", "uploaded_by", "uploaded_at",
    }
    # Not merely absent from the params — absent from the statement too.
    assert [k for k in ("face", "embedding", "person", "similarity", "photo",
                        "image") if k in engine.statements[0].lower()] == []


def test_the_upload_path_end_to_end_persists_through_the_real_repository(engine):
    """The production path: cache stamps an ISO string -> history -> repository.

    Regression for the 201-with-no-row report — before the fix this recorded
    `video_analysis.record_failed` and answered `persisted: false`.
    """
    history = VideoAnalysisHistory(repository=VideoAnalysisRepository(dsn=_DSN))

    entry = _run(history.record(
        "e2e11111122223333",
        securevision_camera_code="CAM-01", jnpa_camera_id="CAM-COR-01",
        filename="clip.mp4", frames_sampled=120, detection_pass_count=1,
        zones_loaded=3, uploaded_by="DTCCC_ADMIN", status="COMPLETED",
        processing_ms=4200, source="securevision"))

    assert entry["persisted"] is True
    # The cache still reports the ISO string to the API (contract unchanged)…
    assert isinstance(entry["uploaded_at"], str)
    # …while the driver got a datetime.
    assert isinstance(engine.params[0]["uploaded_at"], datetime)


def test_coercion_helper_accepts_both_shapes_and_passes_none_through():
    assert _as_utc_datetime(None) is None
    assert _as_utc_datetime("") is None
    aware = datetime(2026, 8, 12, 20, 25, 6, tzinfo=timezone.utc)
    assert _as_utc_datetime(aware) == aware
    assert _as_utc_datetime("2026-08-12T20:25:06Z") == aware      # trailing Z
    assert _as_utc_datetime("2026-08-12T20:25:06+00:00") == aware
    assert _as_utc_datetime(1234) is None                          # not a date
