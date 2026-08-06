"""Report-group ingestion tests (Phase 3) — real client, real sim (in-process
ASGI), dedup-aware fake sync repo + REAL upload services over in-memory fake
repos (Pattern A: no DB, no network).

Covers the land-raw-then-map guarantees:
  * an empty report answer is NOT evidence — no snapshot, status OK;
  * the sim's synthetic items map end-to-end: a raw snapshot is landed AND the
    rows land through the SAME validated upload pipeline (berthing CSV ->
    BerthingUploadService, daily -> UploadService("daily_status")); a second
    identical poll is a content-sha no-op (snapshots_new == 0);
  * the mappers are tolerant alias dictionaries: alias hits map, unknown shapes
    degrade to RAW_ONLY, and junk items never raise.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    from datetime import timezone

    IST = timezone(timedelta(hours=5, minutes=30))

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from integrations.jnpa_portdata import JnpaPortDataClient  # noqa: E402
from services.jnpa_sync.service import JnpaSyncService  # noqa: E402
from services.jnpa_sync.report_ingest import (  # noqa: E402
    BERTHING_TERMINALS,
    ReportSinks,
    sync_report_group,
)
from services.jnpa_sync.report_mappers import (  # noqa: E402
    MapOutcome,
    map_berthing_items,
    map_daily_items,
    render_berthing_csv,
    render_daily_csv,
)
from services.jnpa_sync.repository import payload_sha256  # noqa: E402
from services.berthing.upload_service import BerthingUploadService  # noqa: E402
from services.performance.upload_service import UploadService  # noqa: E402

from test_jnpa_sync_service import FakeSyncRepo  # noqa: E402
from _jnpa_sim_fixtures import (  # noqa: E402
    SIM_KEY,
    build_fixture_corpus,
    fresh_sim,
    sim_asgi_app,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class ReportSyncRepo(FakeSyncRepo):
    """FakeSyncRepo + the content-sha snapshot dedup the real SyncRepository's
    ON CONFLICT natural key provides (so a double poll is idempotent)."""

    def __init__(self, known_shas: Optional[set] = None) -> None:
        super().__init__(known_shas=known_shas)
        self._snap_keys: set = set()
        self.mapped_updates: List[Dict[str, Any]] = []

    async def insert_report_snapshot(self, *, group, report_date, terminal,
                                     payload, item_count, ingest_run_id):
        key = (group, report_date, terminal or "", payload_sha256(payload))
        if key in self._snap_keys:
            return None
        self._snap_keys.add(key)
        rec = {"id": len(self.report_snapshots) + 1, "group": group,
               "report_date": report_date, "terminal": terminal,
               "payload": payload, "item_count": item_count,
               "ingest_run_id": ingest_run_id, "mapped_status": "RAW_ONLY"}
        self.report_snapshots.append(rec)
        return rec["id"]

    async def update_report_mapped(self, snapshot_id, *, status, detail=None):
        self.mapped_updates.append({"id": snapshot_id, "status": status,
                                    "detail": detail})
        for rec in self.report_snapshots:
            if rec["id"] == snapshot_id:
                rec["mapped_status"] = status


class FakeBerthingRepo:
    """In-memory twin of BerthingRepository for the surface
    BerthingUploadService.import_file touches — real parse/validate, fake DB."""

    def __init__(self) -> None:
        self.files: List[Dict[str, Any]] = []
        self.persisted: List[Dict[str, Any]] = []
        self._by_hash: Dict[str, Dict[str, Any]] = {}

    async def find_file_by_hash(self, file_hash):
        return self._by_hash.get(file_hash)

    async def persist(self, records, *, terminal, filename, file_hash,
                      physical_format, file_size=None, uploaded_by=None,
                      source="UPLOAD"):
        if file_hash in self._by_hash:
            ex = self._by_hash[file_hash]
            return {"file_id": ex["id"], "status": "SKIPPED_DUPLICATE",
                    "inserted": 0, "updated": 0,
                    "success_rows": ex["success_rows"], "duplicate_file": True}
        fid = len(self.files) + 1
        row = {"id": fid, "filename": filename, "terminal": terminal,
               "success_rows": len(records), "records": list(records)}
        self.files.append(row)
        self._by_hash[file_hash] = row
        self.persisted.append(row)
        return {"file_id": fid, "status": "SUCCESS", "inserted": len(records),
                "updated": 0, "success_rows": len(records),
                "duplicate_file": False}

    async def record_rejected_upload(self, *, terminal, physical_format,
                                     filename, file_hash, uploaded_by, detail,
                                     errors):
        fid = len(self.files) + 1
        self.files.append({"id": fid, "filename": filename, "terminal": terminal,
                           "status": "REJECTED", "detail": detail})
        return fid

    async def add_row_errors(self, file_id, errors):
        return None

    async def mark_partial(self, file_id, *, failed_rows, duplicate_rows=0):
        return None

    async def set_duplicates(self, file_id, *, duplicate_rows):
        return None


class FakePerfRepo:
    """In-memory twin of UploadRepository for UploadService.import_file."""

    def __init__(self) -> None:
        self.uploads: List[Dict[str, Any]] = []
        self.imported: List[Dict[str, Any]] = []

    async def existing_report_keys(self, report_type, keys):
        return set()

    async def create_upload(self, *, report_type, filename, size, uploaded_by,
                            status, row_count, error_count, notes,
                            file_format="CSV"):
        uid = str(len(self.uploads) + 1)
        self.uploads.append({"upload_id": uid, "report_type": report_type,
                             "filename": filename, "status": status})
        return uid

    async def add_errors(self, upload_id, errors):
        return None

    async def add_log(self, upload_id, phase, level, message,
                      target_table=None, affected=None):
        return None

    async def import_records(self, records, *, upload_id=None, source_file=None,
                             data_origin="MANUAL"):
        total = sum(len(v) for v in records.values())
        self.imported.append({"upload_id": upload_id, "records": records,
                              "source_file": source_file, "data_origin": data_origin})
        per_table = [(k, len(v), len(v), 0) for k, v in records.items() if v]
        return total, 0, per_table

    async def finalize_upload(self, upload_id, *, status, inserted, skipped,
                              updated=0, notes=None):
        for u in self.uploads:
            if u["upload_id"] == upload_id:
                u["status"] = status


def build_service(tmp_path: Path, *, report_items: str = "empty",
                  repo: Optional[ReportSyncRepo] = None):
    data_dir = build_fixture_corpus(tmp_path)
    fresh_sim(data_dir, report_items=report_items)
    transport = httpx.ASGITransport(app=sim_asgi_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://sim")
    client = JnpaPortDataClient("http://sim", client_key=SIM_KEY,
                                http_client=http, retries=1, backoff_s=0.0,
                                rate_limited_wait_s=0.01,
                                rate_limited_jitter_s=0.0)
    repo = repo or ReportSyncRepo()
    service = JnpaSyncService(client=client, repository=repo, api_mode="SIM")
    berthing = BerthingUploadService(repository=FakeBerthingRepo())
    daily = UploadService(repository=FakePerfRepo())
    service._report_sinks = ReportSinks(berthing=berthing, daily=daily)
    return service, repo, berthing, daily


def _window(days_back: int = 1) -> Dict[str, str]:
    """A deterministic date window that always ends today (Asia/Kolkata) so the
    sim — which only serves items for dates <= now — has evidence to return."""
    today = datetime.now(IST).date()
    return {"date_from": (today - timedelta(days=days_back)).isoformat(),
            "date_to": today.isoformat()}


# --------------------------------------------------------------------------- #
# (a) empty report answers                                                     #
# --------------------------------------------------------------------------- #
def test_empty_report_answers_make_no_snapshots(tmp_path):
    service, repo, _b, _d = build_service(tmp_path, report_items="empty")
    res = asyncio.run(sync_report_group(service, "berthing-reports",
                                        trigger="TEST"))
    assert res["status"] == "OK"
    assert res["items_total"] == 0           # the empty sim returns no items
    assert res["buckets"] == 0
    assert res["snapshots_new"] == 0
    assert res["mapped"] == 0
    assert res["raw_only"] == 0
    assert res["map_failed"] == 0
    assert repo.report_snapshots == []       # empty answers are not evidence
    assert repo.mapped_updates == []


# --------------------------------------------------------------------------- #
# (b) synthetic corpus: snapshots + MAPPED + idempotent double-run             #
# --------------------------------------------------------------------------- #
def test_synthetic_berthing_maps_and_is_idempotent(tmp_path):
    service, repo, berthing, _d = build_service(tmp_path, report_items="synthetic")

    r1 = asyncio.run(sync_report_group(service, "berthing-reports",
                                       trigger="TEST"))
    assert r1["status"] == "OK"
    # One call returns the full set (2 dates x 5 terminals); each item is its own
    # (reportDate, terminal) bucket -> 10 snapshots, all MAPPED.
    assert r1["snapshots_new"] == 2 * len(BERTHING_TERMINALS)
    assert r1["mapped"] == r1["snapshots_new"]
    assert r1["raw_only"] == 0
    assert r1["map_failed"] == 0

    # Rows actually landed through the REAL berthing upload pipeline.
    assert len(berthing._repo.persisted) == r1["mapped"]
    assert all(f["terminal"] in BERTHING_TERMINALS
               for f in berthing._repo.persisted)
    assert all(u["status"] == "MAPPED" for u in repo.mapped_updates)

    # Idempotent second run: identical content -> zero new snapshots (dedup).
    r2 = asyncio.run(sync_report_group(service, "berthing-reports",
                                       trigger="TEST"))
    assert r2["status"] == "OK"
    assert r2["snapshots_new"] == 0
    assert r2["mapped"] == 0
    # No further rows pushed into the pipeline on the no-op pass.
    assert len(berthing._repo.persisted) == r1["mapped"]


def test_synthetic_daily_maps_through_performance_pipeline(tmp_path):
    service, repo, _b, daily = build_service(tmp_path, report_items="synthetic")

    r1 = asyncio.run(sync_report_group(service, "daily-reports",
                                       trigger="TEST"))
    assert r1["status"] == "OK"
    # One call returns 2 per-date daily items -> 2 buckets, each carrying 5
    # terminal rows in its byTerminal array.
    assert r1["snapshots_new"] == 2
    assert r1["mapped"] == 2
    assert r1["raw_only"] == 0
    assert r1["map_failed"] == 0
    # Each mapped snapshot lands through the daily_status upload pipeline.
    assert len(daily._repo.imported) == 2
    assert all(u["status"] == "MAPPED" for u in repo.mapped_updates)

    r2 = asyncio.run(sync_report_group(service, "daily-reports",
                                       trigger="TEST"))
    assert r2["snapshots_new"] == 0
    assert len(daily._repo.imported) == 2


def test_dry_run_polls_but_mutates_nothing(tmp_path):
    service, repo, berthing, _d = build_service(tmp_path, report_items="synthetic")
    res = asyncio.run(sync_report_group(service, "berthing-reports",
                                        trigger="TEST", dry_run=True))
    assert res["status"] == "DRY_RUN"
    assert res["items_total"] == 2 * len(BERTHING_TERMINALS)   # it DID call the API
    assert res["buckets"] == 2 * len(BERTHING_TERMINALS)
    assert res["snapshots_new"] == 0
    assert repo.report_snapshots == []                     # but mutated nothing
    assert repo.runs == []
    assert berthing._repo.persisted == []


# --------------------------------------------------------------------------- #
# (c) mapper unit tests: alias hits, degrade, never-raise                      #
# --------------------------------------------------------------------------- #
def test_berthing_mapper_alias_hits():
    items = [{"berthNo": "A1", "vcn": "V123", "vesselName": "MV TEST",
              "ETB": "2026-08-01T06:00:00+05:30", "reportDate": "2026-08-01"}]
    out = map_berthing_items(items, report_date="2026-08-01", terminal="NSICT")
    assert out.status == "MAPPED"
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row["vessel_name"] == "MV TEST"
    assert row["vessel_call"] == "V123"
    assert row["berth"] == "A1"
    assert row["berthing_time"] == "2026-08-01T06:00:00+05:30"
    assert row["terminal"] == "NSICT"           # from the selector fallback
    assert out.unmapped_keys == []              # reportDate is a param, not drift


def test_berthing_mapper_uses_sim_synthetic_keys():
    from jnpa_portdata_sim.seed import synthetic_report_items

    items = synthetic_report_items("berthing-reports", "2026-08-01", "APMT")
    out = map_berthing_items(items, report_date="2026-08-01", terminal="APMT")
    assert out.status == "MAPPED"
    csv_bytes = render_berthing_csv(out.rows)
    assert csv_bytes is not None
    assert b"Vessel Name" in csv_bytes           # the real template header
    assert b"Voyage Number" in csv_bytes


def test_daily_mapper_uses_sim_synthetic_keys():
    from jnpa_portdata_sim.seed import synthetic_report_items

    items = synthetic_report_items("daily-reports", "2026-08-01", None)
    out = map_daily_items(items, report_date="2026-08-01")
    assert out.status == "MAPPED"
    assert len(out.rows) == 5                    # APMT/NSICT/NSIGT/BMCT/NSFT
    csv_bytes = render_daily_csv(out.rows)
    assert csv_bytes is not None
    assert b"terminal_code" in csv_bytes
    assert b"report_date" in csv_bytes


def test_mappers_degrade_to_raw_only_on_unknown_shape():
    b = map_berthing_items([{"weird": 1, "nonsense": 2}],
                           report_date="2026-08-01", terminal="APMT")
    assert b.status == "RAW_ONLY"
    assert b.rows == []
    d = map_daily_items([{"weird": 1, "nonsense": 2}], report_date="2026-08-01")
    assert d.status == "RAW_ONLY"
    assert d.rows == []


def test_mappers_recognised_shape_without_key_is_raw_only():
    # berth + eta recognised (>=2 keys) but no vessel_name/vessel_call -> RAW_ONLY
    out = map_berthing_items([{"berth": "A1", "eta": "2026-08-01T00:00:00"}],
                             report_date="2026-08-01", terminal="APMT")
    assert out.status == "RAW_ONLY"
    assert out.rows == []


def test_mappers_never_raise_on_junk():
    junk_inputs = ([], [None], [{}], [{"weird": 1}], ["not-a-dict"],
                   [{"vesselName": None}], [123, None, {"x": object()}])
    for junk in junk_inputs:
        b = map_berthing_items(junk, report_date="2026-08-01", terminal="APMT")
        d = map_daily_items(junk, report_date="2026-08-01")
        assert isinstance(b, MapOutcome)
        assert isinstance(d, MapOutcome)
        assert b.status in ("RAW_ONLY", "MAPPED", "MAP_FAILED")
        assert d.status in ("RAW_ONLY", "MAPPED", "MAP_FAILED")
    # renderers tolerate empty row sets
    assert render_berthing_csv([]) is None
    assert render_daily_csv([]) is None


# --------------------------------------------------------------- report_date bind
# Regression: the ingest layer threads report_date as an ISO STRING (it doubles
# as the snapshot bucket key), but core.api_report_snapshot.report_date is a
# `date` bound under CAST(:d AS date). asyncpg resolves the bind's type from that
# cast and rejects a str outright ("'str' object has no attribute 'toordinal'"),
# so the statement never reaches Postgres and the WHOLE report group fails —
# berthing-reports and daily-reports landed ZERO snapshots on the live RDS.
# The tests above fake insert_report_snapshot, so only this exercises the bind.
def test_report_date_param_coerces_iso_string_to_date():
    from datetime import date as _date

    from services.jnpa_sync.repository import SyncRepository

    coerce = SyncRepository._report_date_param
    assert coerce("2026-07-11") == _date(2026, 7, 11)
    # a full ISO timestamp is truncated to its date part
    assert coerce("2026-07-11T13:45:00+05:30") == _date(2026, 7, 11)
    # already-a-date and None pass straight through
    assert coerce(_date(2026, 7, 11)) == _date(2026, 7, 11)
    assert coerce(None) is None
    # unparseable/empty degrade to NULL rather than exploding the group
    assert coerce("") is None
    assert coerce("not-a-date") is None
    # every returned value is what asyncpg's date codec needs
    for value in ("2026-07-11", _date(2026, 7, 11)):
        assert hasattr(coerce(value), "toordinal")
