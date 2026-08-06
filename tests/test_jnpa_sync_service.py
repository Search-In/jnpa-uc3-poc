"""JnpaSyncService orchestration tests — real client, real sim (in-process
ASGI), fake repository + fake router (Pattern A: no DB, no network).

Covers the Phase-2 guarantees:
  * a full sync lands every record, downloads every file, routes with the
    Content-Disposition filename, and advances the watermark;
  * a second sync is a boundary re-read only: zero new records, zero
    downloads (the −1s rewind re-reads ONLY the tied records and the
    record_id dedup absorbs them);
  * dump-first-then-API: checksums already in an import ledger are never
    downloaded (files_skipped_checksum) — the dual-source-safety proof;
  * dry_run mutates nothing;
  * UNROUTED records replay from the raw store with no re-download;
  * customs import_bytes == import_file on the same bytes (seam parity).
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from integrations.jnpa_portdata import JnpaPortDataClient  # noqa: E402
from services.jnpa_sync.routing import JnpaRouter, RouteOutcome  # noqa: E402
from services.jnpa_sync.service import JnpaSyncService  # noqa: E402
from services.jnpa_sync.store import ApiFileStore  # noqa: E402

from _jnpa_sim_fixtures import (  # noqa: E402
    SIM_KEY,
    build_fixture_corpus,
    fresh_sim,
    sim_asgi_app,
)


class FakeSyncRepo:
    """In-memory twin of SyncRepository with byte-identical signatures for
    the surface JnpaSyncService touches."""

    def __init__(self, known_shas: Optional[set] = None) -> None:
        self.state: Dict[str, Dict[str, Any]] = {}
        self.runs: List[Dict[str, Any]] = []
        self.records: Dict[str, Dict[str, Any]] = {}
        self.defects: List[Any] = []
        self.report_snapshots: List[Dict[str, Any]] = []
        self.known_shas = known_shas or set()
        self.locks_taken: List[str] = []

    @asynccontextmanager
    async def group_lock(self, group: str):
        self.locks_taken.append(group)
        yield True

    async def get_sync_state(self, group):
        return self.state.get(group)

    async def list_sync_state(self):
        return list(self.state.values())

    async def upsert_sync_state(self, group, *, watermark_ts=None,
                                last_cursor=None, last_run_id=None,
                                last_status=None):
        row = self.state.setdefault(group, {"group_slug": group,
                                            "watermark_ts": None,
                                            "last_cursor": None,
                                            "last_run_id": None,
                                            "last_status": None,
                                            "updated_at": None})
        if watermark_ts is not None:
            row["watermark_ts"] = watermark_ts
        row["last_cursor"] = last_cursor
        if last_run_id is not None:
            row["last_run_id"] = last_run_id
        if last_status is not None:
            row["last_status"] = last_status

    async def open_run(self, *, trigger, group, api_mode):
        self.runs.append({"id": len(self.runs) + 1, "trigger": trigger,
                          "group_slug": group, "api_mode": api_mode,
                          "status": "RUNNING"})
        return self.runs[-1]["id"]

    async def close_run(self, run_id, *, status, counters=None, error=None,
                        detail=None):
        run = self.runs[run_id - 1]
        run.update({"status": status, "error": error, **(counters or {})})

    async def list_runs(self, *, limit=50, group=None):
        rows = [r for r in self.runs if group is None
                or r["group_slug"] == group]
        return list(reversed(rows))[:limit]

    async def insert_record(self, *, record_id, group, message_type,
                            message_name, published_at, container_count,
                            vessel_call, summary, file_ref, media_type,
                            size_bytes, checksum_sha256, ingest_run_id,
                            payload):
        if record_id in self.records:
            return None
        self.records[record_id] = {
            "record_id": record_id, "group_slug": group,
            "message_type": message_type, "published_at": published_at,
            "checksum_sha256": checksum_sha256, "stored_path": None,
            "routed_service": None, "routed_status": None,
            "routed_file_id": None, "payload": payload}
        return len(self.records)

    async def update_record_routing(self, record_id, *, stored_path=None,
                                    routed_service=None, routed_status=None,
                                    routed_file_id=None):
        row = self.records[record_id]
        if stored_path is not None:
            row["stored_path"] = stored_path
        if routed_service is not None:
            row["routed_service"] = routed_service
        if routed_status is not None:
            row["routed_status"] = routed_status
        if routed_file_id is not None:
            row["routed_file_id"] = routed_file_id

    async def list_records(self, *, group=None, routed_status=None, limit=100):
        rows = [r for r in self.records.values()
                if (group is None or r["group_slug"] == group)
                and (routed_status is None
                     or r["routed_status"] == routed_status)]
        return rows[:limit]

    async def find_record_by_sha(self, sha256):
        for row in self.records.values():
            if row["checksum_sha256"] == sha256 and row["stored_path"]:
                return row
        return None

    async def known_sha(self, sha256):
        if sha256 in self.known_shas:
            return {"source": "dump_ledger", "table": "core.test_ledger"}
        record = await self.find_record_by_sha(sha256)
        if record:
            return {"source": "api_record", "table": "core.api_record"}
        return None

    async def insert_report_snapshot(self, **kwargs):
        self.report_snapshots.append(kwargs)
        return len(self.report_snapshots)

    async def update_report_mapped(self, snapshot_id, *, status, detail=None):
        pass

    async def log_defects(self, observations, run_id):
        self.defects.extend(observations)
        return len(list(observations))

    async def list_defects(self, *, limit=200, severity=None):
        return []

    async def list_report_snapshots(self, *, group=None, limit=100):
        return self.report_snapshots[:limit]


class RecordingRouter(JnpaRouter):
    """Real router base, but every group resolves to a recording fake — the
    routing heuristics stay real, the consumers do not."""

    def __init__(self, outcome_status: str = "SUCCESS") -> None:
        super().__init__(dsn=None, services={})
        self.calls: List[Dict[str, Any]] = []
        self.outcome_status = outcome_status

    async def _route(self, group, *, filename, content, message_type):
        self.calls.append({"group": group, "filename": filename,
                           "bytes": len(content),
                           "message_type": message_type})
        if self.outcome_status == "UNROUTED":
            return RouteOutcome(service=group, status="UNROUTED",
                                detail={"reason": "test"})
        return RouteOutcome(service=f"fake_{group}",
                            status=self.outcome_status, file_id=7)


def make_service(tmp_path: Path, *, repo: Optional[FakeSyncRepo] = None,
                 router: Optional[RecordingRouter] = None):
    data_dir = build_fixture_corpus(tmp_path)
    fresh_sim(data_dir)
    transport = httpx.ASGITransport(app=sim_asgi_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://sim")
    client = JnpaPortDataClient("http://sim", client_key=SIM_KEY,
                                http_client=http, retries=1, backoff_s=0.0,
                                rate_limited_wait_s=0.01,
                                rate_limited_jitter_s=0.0)
    repo = repo or FakeSyncRepo()
    router = router or RecordingRouter()
    store = ApiFileStore(tmp_path / "store")
    service = JnpaSyncService(client=client, repository=repo, store=store,
                              router=router, api_mode="SIM")
    return service, repo, router, store


FIXTURE_INDEXED_TOTAL = 14   # customs 9 + nlp-marine 3 + cfs-ecy 1 + transport 1


def test_full_sync_lands_routes_and_advances_watermark(tmp_path):
    service, repo, router, store = make_service(tmp_path)
    results = asyncio.run(service.sync_all(trigger="TEST"))

    assert results["customs"]["records_new"] == 9
    assert results["customs"]["files_downloaded"] == 9
    assert results["nlp-marine"]["records_new"] == 3
    total_new = sum(r.get("records_new", 0) for r in results.values()
                    if isinstance(r, dict))
    assert total_new == FIXTURE_INDEXED_TOTAL

    # Routed with the REAL Content-Disposition filenames.
    customs_calls = [c for c in router.calls if c["group"] == "customs"]
    assert len(customs_calls) == 9
    assert all(c["filename"].startswith(("CHPOI03_", "CHPOI10_"))
               for c in customs_calls)

    # Every record marked routed, bytes in the raw store, watermark set.
    assert all(r["routed_status"] == "SUCCESS"
               for r in repo.records.values()
               if r["group_slug"] == "customs")
    assert repo.state["customs"]["watermark_ts"] is not None
    assert (tmp_path / "store" / "customs").is_dir()

    # Static + report groups handled gracefully.
    assert results["bathymetry"]["status"] == "SKIPPED_STATIC"
    assert results["berthing-reports"]["status"] in ("PENDING", "OK")


def test_second_sync_is_a_pure_boundary_reread(tmp_path):
    service, repo, router, _ = make_service(tmp_path)
    asyncio.run(service.sync_all(trigger="TEST"))
    first_calls = len(router.calls)

    results = asyncio.run(service.sync_all(trigger="TEST"))
    assert all(r.get("records_new", 0) == 0 for r in results.values()
               if isinstance(r, dict))
    assert all(r.get("files_downloaded", 0) == 0 for r in results.values()
               if isinstance(r, dict))
    # The −1s rewind re-reads ONLY the boundary-tied records (customs has a
    # 4-way tie at its final timestamp; other fixture groups re-read their
    # single boundary record at most).
    assert results["customs"]["records_duplicate"] == 4
    assert len(router.calls) == first_calls          # nothing re-routed


def test_dump_loaded_checksums_are_never_downloaded(tmp_path):
    # Pre-compute the fixture checksums = "already imported from the dump".
    import hashlib
    data_dir = build_fixture_corpus(tmp_path / "corpus_probe")
    dump_shas = {hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in data_dir.rglob("*") if p.is_file()}

    repo = FakeSyncRepo(known_shas=dump_shas)
    service, repo, router, _ = make_service(tmp_path, repo=repo)
    results = asyncio.run(service.sync_all(trigger="TEST"))

    total_skipped = sum(r.get("files_skipped_checksum", 0)
                        for r in results.values() if isinstance(r, dict))
    total_downloaded = sum(r.get("files_downloaded", 0)
                           for r in results.values() if isinstance(r, dict))
    assert total_skipped == FIXTURE_INDEXED_TOTAL
    assert total_downloaded == 0
    assert router.calls == []                        # nothing to route
    assert all(r["routed_status"] == "SKIPPED_DUPLICATE"
               for r in repo.records.values())


def test_dry_run_mutates_nothing(tmp_path):
    service, repo, router, _ = make_service(tmp_path)
    result = asyncio.run(service.sync_group("customs", dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["records_listed"] == 9
    assert repo.records == {}
    assert repo.runs == []
    assert router.calls == []


def test_replay_unrouted_uses_the_store_no_redownload(tmp_path):
    router = RecordingRouter(outcome_status="UNROUTED")
    service, repo, router, store = make_service(tmp_path, router=router)
    asyncio.run(service.sync_group("customs", trigger="TEST"))
    unrouted = [r for r in repo.records.values()
                if r["routed_status"] == "UNROUTED"]
    assert len(unrouted) == 9

    stats_before = service.client.request_stats()
    router.outcome_status = "SUCCESS"                # the consumer arrives
    replay = asyncio.run(service.replay_unrouted("customs"))
    assert replay["replayed"] == 9
    assert replay["succeeded"] == 9
    assert all(r["routed_status"] == "SUCCESS"
               for r in repo.records.values()
               if r["group_slug"] == "customs")
    # No additional HTTP requests: replay reads the raw store.
    assert service.client.request_stats().request_count == \
        stats_before.request_count


def test_locked_group_is_skipped_politely(tmp_path):
    class LockedRepo(FakeSyncRepo):
        @asynccontextmanager
        async def group_lock(self, group):
            yield False

    service, repo, router, _ = make_service(tmp_path, repo=LockedRepo())
    result = asyncio.run(service.sync_group("customs"))
    assert result["status"] == "LOCKED"
    assert repo.runs == []


# ---------------------------------------------------------------------------
# Customs import_bytes seam parity (contradiction C-1)
# ---------------------------------------------------------------------------
class _FakeCustomsRepo:
    def __init__(self) -> None:
        self.persist_calls: List[Dict[str, Any]] = []

    async def persist(self, parsed, *, source_file, source_sha256,
                      file_size=None, data_origin="MANUAL"):
        self.persist_calls.append({"source_file": source_file,
                                   "source_sha256": source_sha256,
                                   "file_size": file_size,
                                   "data_origin": data_origin})
        return {"message_id": len(self.persist_calls), "module": "IGM",
                "import_status": "SUCCESS", "record_count": 1,
                "imported_count": 1, "error_count": 0, "duplicate": False}

    async def record_event(self, *args, **kwargs):
        return None


def test_customs_import_bytes_matches_import_path(tmp_path, monkeypatch):
    """The same bytes through import_file(path) and import_bytes(content,
    filename) must produce an IDENTICAL ledger envelope (source_file, sha256,
    size) — the dual-source dedup rides on it."""
    import services.customs.service as customs_mod

    class _StubParsed:
        message: Dict[str, Any] = {}

    monkeypatch.setattr(customs_mod, "detect_parser",
                        lambda path: (lambda p: _StubParsed(), "IGM"))

    content = b"<CHPOI03Payload><IGM_NO>777</IGM_NO></CHPOI03Payload>"
    filename = "CHPOI03_777_test.xml"
    disk_file = tmp_path / filename
    disk_file.write_bytes(content)

    repo = _FakeCustomsRepo()
    svc = customs_mod.CustomsService(repository=repo)

    via_path = asyncio.run(svc.import_file(str(disk_file)))
    via_bytes = asyncio.run(svc.import_bytes(content, filename))

    assert via_path["import_status"] == via_bytes["import_status"] == "SUCCESS"
    assert via_path["source_file"] == via_bytes["source_file"] == filename
    a, b = repo.persist_calls
    assert a["source_sha256"] == b["source_sha256"]
    assert a["file_size"] == b["file_size"] == len(content)


def test_customs_import_bytes_unknown_format_fails_like_path(tmp_path):
    from services.customs.service import CustomsService

    content = b"garbage bytes"
    filename = "unrecognised.dat"
    disk_file = tmp_path / filename
    disk_file.write_bytes(content)

    svc = CustomsService(repository=_FakeCustomsRepo())
    via_path = asyncio.run(svc.import_file(str(disk_file)))
    via_bytes = asyncio.run(svc.import_bytes(content, filename))
    assert via_path["import_status"] == via_bytes["import_status"] == "FAILED"
    assert via_path["source_file"] == via_bytes["source_file"] == filename
