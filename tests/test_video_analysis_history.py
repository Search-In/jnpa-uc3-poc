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
