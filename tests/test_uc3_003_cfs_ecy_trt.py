"""UC3-003 — CFS/ECY gate-event ingestion + the empty-container TRT KPI.

Four layers, deliberately separated so the suite is meaningful everywhere:

* **Corpus** — runs against the actual customer drop and is skipped when it is
  not present (CI machines without the data). Asserts the source inventory, the
  event mapping, the lifecycle chains and the anomalies, all recomputed from the
  workbooks rather than compared to constants the importer itself produced.
* **KPI** — pure arithmetic against ``jnpa_shared.kpi``: the target (45 min),
  the baseline (72 min) and the definition the service must not re-invent.
* **Router** — runs everywhere against stub services, pinning the API contract
  the Empty-TRT screen and the Data Quality console consume.
* **Live database** — skipped unless ``UC3_TEST_DSN`` names a reachable
  database. Asserts what actually landed: all 1929 rows, the preserved source
  duplicate, the DQ ledger, and that a second import inserts nothing.

The expected counts in the corpus layer come from the brief (961 / 968 / 242 /
529 / 432); everything else is derived, so a different drop fails loudly instead
of silently re-baselining.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path in the router layer fails fast.
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from jnpa_shared import kpi as kpi_engine  # noqa: E402
from services.cfs_ecy import EmptyTrtService  # noqa: E402
from services.dq import DqService  # noqa: E402

# What the brief states the source drop contains. These are the ONLY hard-coded
# expectations, and they are assertions about the customer's data, not about our
# own output.
ECY_ROWS, CFS_ROWS = 961, 968
ECY_OUT, ECY_IN = 529, 432
CFS_IN, CFS_OUT = 484, 484
VALID_CHAINS = 242
HERO = "ONEU2122848"
DUP_CONTAINER = "COSU4663595"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_uc3_003_cfs_ecy", REPO_ROOT / "scripts" / "import_uc3_003_cfs_ecy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is absent for a hand-loaded spec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


I = _load_importer()


# ============================================================== corpus layer
@pytest.fixture(scope="module")
def corpus() -> Path:
    try:
        return I.find_corpus(None)
    except I.SourceError as exc:
        pytest.skip(f"CFS/ECY CODECO corpus not available: {exc}")


@pytest.fixture(scope="module")
def analysis(corpus):
    return I.build(corpus)


# --- A. source ingestion -----------------------------------------------------
def test_both_workbooks_are_discovered(corpus):
    for feed in I.FEEDS:
        assert (corpus / feed.filename).is_file(), f"{feed.filename} missing from {corpus}"


def test_source_row_counts_match_the_brief(analysis):
    assert analysis.per_feed["ECY"]["parsed_rows"] == ECY_ROWS
    assert analysis.per_feed["CFS"]["parsed_rows"] == CFS_ROWS
    assert analysis.total_rows == ECY_ROWS + CFS_ROWS == 1929


def test_every_source_row_maps_to_an_event(analysis):
    """No row is dropped: parsed == mapped, and nothing was rejected."""
    assert analysis.rejected == []
    assert len(analysis.events) == analysis.total_rows


def test_required_columns_are_enforced(tmp_path):
    """A workbook with the wrong header is refused, not partially parsed."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Box", "When", "Direction"])
    ws.append(["ONEU2122848", "01/07/2026 10:00", "Out"])
    path = tmp_path / "ECY-CODECO.xlsx"
    wb.save(path)
    with pytest.raises(I.SourceError) as exc:
        I.parse_feed(path, I.FEEDS[0])
    assert "Container Number" in str(exc.value)


def test_event_mapping_is_file_and_mode_driven(analysis):
    """file + Mode -> event_type / location_type / direction, for every row."""
    seen = Counter((e.source_file, e.raw_mode.upper(), e.event_type,
                    e.location_type, e.direction) for e in analysis.events)
    expected = {
        ("ECY-CODECO.xlsx", "OUT", "ECY_OUT", "ECY", "O"): ECY_OUT,
        ("ECY-CODECO.xlsx", "IN", "ECY_IN", "ECY", "I"): ECY_IN,
        ("CFS-CODECO.xlsx", "IN", "CFS_IN", "CFS", "I"): CFS_IN,
        ("CFS-CODECO.xlsx", "OUT", "CFS_OUT", "CFS", "O"): CFS_OUT,
    }
    assert dict(seen) == expected


def test_timestamps_are_parsed_as_ist():
    """DD/MM/YYYY HH:MM with no timezone is stamped Asia/Kolkata, not UTC."""
    ts = I.parse_ts("01/07/2026 10:00")
    assert ts == dt.datetime(2026, 7, 1, 10, 0, tzinfo=IST)
    assert ts.astimezone(dt.timezone.utc).hour == 4      # 10:00 IST == 04:30 UTC
    assert I.parse_ts("not a date") is None
    assert I.parse_ts(None) is None


def test_source_values_are_preserved_verbatim(analysis):
    """Every event keeps the original cell text, so nothing is normalised away."""
    hero = [e for e in analysis.events if e.container_no == HERO]
    assert {e.raw_timestamp for e in hero} == {
        "01/07/2026 10:00", "01/07/2026 14:00", "07/07/2026 08:16"}
    assert {e.raw_mode for e in hero} == {"Out", "In"}


# --- B. the ECY anomaly, detected not patched --------------------------------
def test_ecy_log_is_unpaired_and_the_rows_are_all_kept(analysis):
    ecy = analysis.per_feed["ECY"]
    assert (ecy["out_events"], ecy["in_events"]) == (ECY_OUT, ECY_IN)
    # The surplus is still in the dataset — nothing was deleted to make it pair.
    assert ecy["events"] == ECY_OUT + ECY_IN == ECY_ROWS


def test_ecy_out_and_in_blocks_are_date_disjoint(analysis):
    outs = [e.event_ts for e in analysis.events if e.event_type == "ECY_OUT"]
    ins = [e.event_ts for e in analysis.events if e.event_type == "ECY_IN"]
    out_containers = {e.container_no for e in analysis.events if e.event_type == "ECY_OUT"}
    in_containers = {e.container_no for e in analysis.events if e.event_type == "ECY_IN"}
    assert out_containers & in_containers == set(), "blocks unexpectedly share a container"
    assert max(outs).date() <= min(ins).date()


def test_the_pairing_anomaly_is_recorded_as_a_dq_finding(analysis):
    counts = {f["issue_type"] for f in analysis.findings}
    assert "count_mismatch" in counts and "disjoint_ranges" in counts
    headline = next(f for f in analysis.findings if f["issue_type"] == "count_mismatch")
    assert headline["severity"] == "warn"
    assert str(ECY_OUT) in headline["description"]
    assert str(ECY_IN) in headline["description"]
    assert headline["record_ref"] == "ECY-CODECO.xlsx"


def test_findings_are_grouped_not_one_row_per_record(analysis):
    """The ledger stays readable: a handful of grouped rows, not hundreds."""
    assert 0 < len(analysis.findings) <= 12


def test_exact_duplicate_source_row_is_reported_and_kept(analysis):
    dupes = [k for k, n in Counter(e.key for e in analysis.events).items() if n > 1]
    assert dupes, "the corpus's duplicate CFS gate-in vanished"
    assert all(k[0] == DUP_CONTAINER for k in dupes)
    assert any(f["issue_type"] == "duplicate" for f in analysis.findings)


def test_unmatched_records_are_flagged_never_completed(analysis):
    """ECY gate-outs with no CFS leg are reported, and no event was invented."""
    ecy_out = {e.container_no for e in analysis.events if e.event_type == "ECY_OUT"}
    cfs = {e.container_no for e in analysis.events
           if e.event_type in ("CFS_IN", "CFS_OUT")}
    unmatched = ecy_out - cfs
    assert unmatched, "expected ECY gate-outs with no CFS arrival"
    assert len(analysis.chains) + len(unmatched) == len(ecy_out)
    missing = [f for f in analysis.findings if f["issue_type"] == "missing_key"]
    assert any(str(len(unmatched)) in f["description"] for f in missing)


# --- C. the lifecycle chains -------------------------------------------------
def test_valid_chain_count_is_derived_from_the_source(analysis):
    assert len(analysis.chains) == VALID_CHAINS


def test_every_chain_is_correctly_ordered(analysis):
    for cn, c in analysis.chains.items():
        assert c["ecy_out_ts"] <= c["cfs_in_ts"] <= c["cfs_out_ts"], cn
        assert c["trt_min"] >= 0 and c["dwell_min"] >= 0


def test_incomplete_chains_are_excluded_from_the_kpi(analysis):
    """A container missing any leg contributes no TRT sample."""
    complete = set(analysis.chains)
    by_container: Dict[str, set] = {}
    for e in analysis.events:
        by_container.setdefault(e.container_no, set()).add(e.event_type)
    for cn, types in by_container.items():
        if not {"ECY_OUT", "CFS_IN", "CFS_OUT"} <= types:
            assert cn not in complete


def test_hero_container_lifecycle_matches_the_source(analysis):
    c = analysis.chains.get(HERO)
    assert c is not None, f"{HERO} is not a complete chain"
    ist = lambda t: t.astimezone(IST).strftime("%d/%m/%Y %H:%M")  # noqa: E731
    assert ist(c["ecy_out_ts"]) == "01/07/2026 10:00"
    assert ist(c["cfs_in_ts"]) == "01/07/2026 14:00"
    assert ist(c["cfs_out_ts"]) == "07/07/2026 08:16"
    assert c["trt_min"] == 240.0            # 10:00 -> 14:00


# --- D. the KPI --------------------------------------------------------------
def test_kpi_targets_are_the_projects_own_not_redefined():
    t = kpi_engine.KPI_TARGETS["trt_empty_ecd"]
    assert (t.target, t.baseline, t.unit) == (45.0, 72.0, "min")
    assert t.direction == "lower_is_better"


def test_trt_summary_uses_the_existing_kpi_definition(analysis):
    """The service's value must equal kpi.trt_empty_ecd_min() over the samples."""
    summary = I.trt_summary(analysis)
    samples = [c["trt_min"] * 60.0 for c in analysis.chains.values()]
    expected = round(kpi_engine.trt_empty_ecd_min(samples), 2)
    assert summary["valid_containers"] == VALID_CHAINS
    assert summary["avg_trt_min"] == expected
    k = summary["kpi"]
    assert (k["key"], k["target"], k["baseline"], k["unit"]) == \
        ("trt_empty_ecd", 45.0, 72.0, "min")
    assert k["n"] == VALID_CHAINS and k["source"] == "live"
    assert k["onTarget"] is (summary["avg_trt_min"] <= 45.0)


def test_trt_distribution_is_internally_consistent(analysis):
    s = I.trt_summary(analysis)
    assert s["min_trt_min"] <= s["median_trt_min"] <= s["max_trt_min"]
    assert s["min_trt_min"] <= s["avg_trt_min"] <= s["max_trt_min"]


def test_kpi_engine_is_not_bypassed_for_an_empty_dataset():
    empty = I.Analysis()
    assert I.trt_summary(empty) == {"valid_containers": 0, "kpi": None}


# --- E. the migration --------------------------------------------------------
def test_migration_is_additive_only():
    sql = (REPO_ROOT / "infra" / "postgres" / "v3"
           / "0133_uc3_003_empty_container_trt.sql").read_text()
    lowered = sql.lower()
    for forbidden in ("drop table", "drop view", "drop column", "delete from",
                      "truncate", "alter table"):
        assert forbidden not in lowered, f"migration 0133 contains {forbidden!r}"
    assert "create or replace view mart.v_empty_container_trt" in lowered
    assert "create or replace view mart.v_empty_container_chain" in lowered
    # The customer's own view must survive untouched.
    assert "v_ecy_trt as" not in lowered


def test_migration_scopes_the_view_to_the_codeco_vocabulary():
    sql = (REPO_ROOT / "infra" / "postgres" / "v3"
           / "0133_uc3_003_empty_container_trt.sql").read_text()
    assert "location_type IN ('ECY', 'CFS')" in sql
    assert "'ECY_OUT', 'ECY_IN', 'CFS_IN', 'CFS_OUT'" in sql


# ============================================================== router layer
# Stub repositories with the same method contracts as the real ones, so the
# router + service logic is exercised with no database.
_T0 = dt.datetime(2026, 7, 1, 10, 0, tzinfo=IST)


def _chain(cn, *, ecy_out=None, cfs_in=None, cfs_out=None, ecy_in=None,
           status="COMPLETE", codes=(), counts=(1, 1, 1, 0)):
    ci, co, eo, ei = counts
    trt = (round((cfs_in - ecy_out).total_seconds() / 60.0, 2)
           if ecy_out and cfs_in else None)
    dwell = (round((cfs_out - cfs_in).total_seconds() / 60.0, 2)
             if cfs_in and cfs_out else None)
    cycle = (round((cfs_out - ecy_out).total_seconds() / 60.0, 2)
             if ecy_out and cfs_out else None)
    return {"container_no": cn, "ecy_out_ts": ecy_out, "ecy_in_ts": ecy_in,
            "cfs_in_ts": cfs_in, "cfs_out_ts": cfs_out, "cfs_first_out_ts": cfs_out,
            "ecy_out_events": eo, "ecy_in_events": ei, "cfs_in_events": ci,
            "cfs_out_events": co, "event_count": ci + co + eo + ei,
            "first_event_ts": ecy_out or cfs_in, "last_event_ts": cfs_out,
            "legs_present": sum(x is not None for x in (ecy_out, cfs_in, cfs_out)),
            "chain_status": status, "trt_min": trt, "dwell_min": dwell,
            "cycle_min": cycle, "anomaly_codes": list(codes)}


_CHAINS = [
    _chain(HERO, ecy_out=_T0, cfs_in=_T0 + dt.timedelta(hours=4),
           cfs_out=_T0 + dt.timedelta(days=6)),
    _chain("CCLU0578304", ecy_out=_T0 + dt.timedelta(hours=1), status="PARTIAL",
           codes=("NO_CFS_IN",), counts=(0, 0, 1, 0)),
    _chain("MSCU7573931", ecy_in=_T0 + dt.timedelta(days=12), status="ORPHAN",
           codes=("ECY_IN_WITHOUT_ECY_OUT",), counts=(0, 0, 0, 1)),
]

_EVENTS = [
    {"event_id": 1, "container_no": HERO, "event_ts": _T0, "event_type": "ECY_OUT",
     "location_type": "ECY", "direction": "O", "source_table": "staging ecy_codeco",
     "source_file": 164, "details": {"source_file": "ECY-CODECO.xlsx", "source_row": 1}},
    {"event_id": 2, "container_no": HERO, "event_ts": _T0 + dt.timedelta(hours=4),
     "event_type": "CFS_IN", "location_type": "CFS", "direction": "I",
     "source_table": "staging cfs_codeco", "source_file": 163,
     "details": {"source_file": "CFS-CODECO.xlsx", "source_row": 1}},
    {"event_id": 3, "container_no": HERO, "event_ts": _T0 + dt.timedelta(days=6),
     "event_type": "CFS_OUT", "location_type": "CFS", "direction": "O",
     "source_table": "staging cfs_codeco", "source_file": 163, "details": None},
    {"event_id": 4, "container_no": "CCLU0578304", "event_ts": _T0 + dt.timedelta(hours=1),
     "event_type": "ECY_OUT", "location_type": "ECY", "direction": "O",
     "source_table": "staging ecy_codeco", "source_file": 164, "details": None},
]

_DQ = [
    {"issue_id": 1, "file_id": 164, "source_path": "Data/13-CFS-ECY/ECY-CODECO.xlsx",
     "source_table": "core.container_event", "record_ref": "ECY-CODECO.xlsx",
     "issue_type": "count_mismatch", "severity": "warn",
     "description": f"ECY CODECO log unpaired: {ECY_OUT} OUT vs {ECY_IN} IN events",
     "detected_at": _T0},
    {"issue_id": 2, "file_id": 163, "source_path": "Data/13-CFS-ECY/CFS-CODECO.xlsx",
     "source_table": "core.container_event", "record_ref": "CFS-CODECO.xlsx",
     "issue_type": "too_clean", "severity": "info",
     "description": "CFS CODECO log is perfectly paired", "detected_at": _T0},
]


class FakeTrtRepo:
    """In-memory stand-in for EmptyTrtRepository with identical contracts."""

    def __init__(self, chains=None, events=None, issues=None) -> None:
        self._chains = [dict(c) for c in (chains if chains is not None else _CHAINS)]
        self._events = [dict(e) for e in (events if events is not None else _EVENTS)]
        self._issues = [dict(i) for i in (issues if issues is not None else _DQ)]

    # -- events
    def _match_event(self, e: Mapping[str, Any], f: Mapping[str, Any]) -> bool:
        if f.get("container") and f["container"].upper() not in e["container_no"]:
            return False
        for col in ("location_type", "event_type", "direction"):
            if f.get(col) and e[col] != f[col]:
                return False
        if f.get("ts_from") and e["event_ts"] < f["ts_from"]:
            return False
        if f.get("ts_to") and e["event_ts"] > f["ts_to"]:
            return False
        return True

    async def list_events(self, filters, *, sort, direction, limit, offset):
        rows = [e for e in self._events if self._match_event(e, filters)]
        rows.sort(key=lambda e: e["event_ts"], reverse=str(direction).lower() != "asc")
        return rows[offset:offset + limit]

    async def count_events(self, filters):
        return len([e for e in self._events if self._match_event(e, filters)])

    async def container_events(self, cn):
        return sorted((e for e in self._events if e["container_no"] == cn),
                      key=lambda e: e["event_ts"])

    # -- inventory
    async def feed_inventory(self):
        by: Dict[str, Dict[str, Any]] = {}
        for e in self._events:
            row = by.setdefault(e["event_type"], {
                "location_type": e["location_type"], "event_type": e["event_type"],
                "direction": e["direction"], "events": 0, "containers": 0,
                "first_event_ts": e["event_ts"], "last_event_ts": e["event_ts"]})
            row["events"] += 1
        # Force the corpus's real inventory so the contract test is meaningful.
        return [
            {"location_type": "ECY", "event_type": "ECY_OUT", "direction": "O",
             "events": ECY_OUT, "containers": ECY_OUT,
             "first_event_ts": _T0, "last_event_ts": _T0},
            {"location_type": "ECY", "event_type": "ECY_IN", "direction": "I",
             "events": ECY_IN, "containers": ECY_IN,
             "first_event_ts": _T0, "last_event_ts": _T0},
            {"location_type": "CFS", "event_type": "CFS_IN", "direction": "I",
             "events": CFS_IN, "containers": CFS_IN,
             "first_event_ts": _T0, "last_event_ts": _T0},
            {"location_type": "CFS", "event_type": "CFS_OUT", "direction": "O",
             "events": CFS_OUT, "containers": CFS_OUT,
             "first_event_ts": _T0, "last_event_ts": _T0},
        ]

    async def source_files(self):
        return [{"file_id": 164, "path": "Data/13-CFS-ECY/ECY-CODECO.xlsx",
                 "source_system": "CFS-ECY", "file_format": "xlsx",
                 "row_count": ECY_ROWS, "loaded_at": _T0, "imported_events": ECY_ROWS},
                {"file_id": 163, "path": "Data/13-CFS-ECY/CFS-CODECO.xlsx",
                 "source_system": "CFS-ECY", "file_format": "xlsx",
                 "row_count": CFS_ROWS, "loaded_at": _T0, "imported_events": CFS_ROWS}]

    # -- chains
    def _match_chain(self, c, f):
        if f.get("container") and f["container"].upper() not in c["container_no"]:
            return False
        if f.get("chain_status") and c["chain_status"] != f["chain_status"].upper():
            return False
        if f.get("anomaly_code") and f["anomaly_code"].upper() not in c["anomaly_codes"]:
            return False
        if f.get("anomaly_only") and not c["anomaly_codes"]:
            return False
        return True

    async def list_chains(self, filters, *, sort, direction, limit, offset):
        rows = [c for c in self._chains if self._match_chain(c, filters)]
        return rows[offset:offset + limit]

    async def count_chains(self, filters):
        return len([c for c in self._chains if self._match_chain(c, filters)])

    async def get_chain(self, cn):
        return next((dict(c) for c in self._chains if c["container_no"] == cn), None)

    async def chain_status_counts(self):
        return dict(Counter(c["chain_status"] for c in self._chains))

    async def anomaly_counts(self):
        codes = Counter(code for c in self._chains for code in c["anomaly_codes"])
        return [{"code": k, "containers": v} for k, v in codes.most_common()]

    async def trt_aggregate(self):
        vals = [c["trt_min"] for c in self._chains
                if c["chain_status"] == "COMPLETE" and c["trt_min"] is not None]
        if not vals:
            return {"valid_containers": 0}
        return {"valid_containers": len(vals),
                "avg_trt_min": round(sum(vals) / len(vals), 2),
                "median_trt_min": sorted(vals)[len(vals) // 2],
                "min_trt_min": min(vals), "max_trt_min": max(vals),
                "avg_dwell_min": None, "avg_cycle_min": None,
                "window_from": _T0, "window_to": _T0}

    async def trt_daily(self, limit: int = 30):
        return [{"day": "2026-07-01", "containers": 1, "avg_trt_min": 240.0}]

    async def dq_issues(self):
        return [dict(i) for i in self._issues]

    async def unpaired_containers(self, code, *, limit, offset):
        rows = [c for c in self._chains if code.upper() in c["anomaly_codes"]]
        return rows[offset:offset + limit], len(rows)


class FakeDqRepo:
    def __init__(self, issues=None) -> None:
        self._issues = [dict(i) for i in (issues if issues is not None else _DQ)]

    def _match(self, i, f):
        for col in ("source_table", "issue_type", "severity"):
            if f.get(col) and i[col] != f[col]:
                return False
        if f.get("file_id") is not None and i["file_id"] != f["file_id"]:
            return False
        if f.get("q") and f["q"].lower() not in (i["description"] or "").lower():
            return False
        return True

    async def list_issues(self, filters, *, sort, direction, limit, offset):
        rows = [i for i in self._issues if self._match(i, filters)]
        return rows[offset:offset + limit]

    async def count_issues(self, filters):
        return len([i for i in self._issues if self._match(i, filters)])

    async def by_severity(self, filters):
        c = Counter(i["severity"] for i in self._issues if self._match(i, filters))
        return [{"severity": k, "issues": v} for k, v in c.items()]

    async def by_source_table(self, filters, *, limit=100):
        return [{"source_table": "core.container_event", "issues": len(self._issues),
                 "errors": 0, "warnings": 1, "info": 1, "last_seen": _T0}]

    async def by_issue_type(self, filters, *, limit=100):
        return [{"issue_type": i["issue_type"], "severity": i["severity"], "issues": 1,
                 "first_seen": _T0, "last_seen": _T0} for i in self._issues]


@pytest.fixture()
def client():
    from gateway.main import app
    from gateway.routers import cfs_ecy as cfs_ecy_router
    from gateway.routers import dq as dq_router

    trt = EmptyTrtService(repository=FakeTrtRepo())
    dq = DqService(repository=FakeDqRepo())
    app.dependency_overrides[cfs_ecy_router.get_trt_service] = lambda: trt
    app.dependency_overrides[dq_router.get_service] = lambda: dq
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(cfs_ecy_router.get_trt_service, None)
    app.dependency_overrides.pop(dq_router.get_service, None)


# --- F. events API -----------------------------------------------------------
def test_events_endpoint_lists_and_paginates(client):
    r = client.get("/api/cfs-ecy/events?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(_EVENTS) and body["count"] == 2
    assert r.headers["X-Total-Count"] == str(len(_EVENTS))


def test_events_endpoint_filters_by_container(client):
    body = client.get(f"/api/cfs-ecy/events?container={HERO}").json()
    assert body["total"] == 3
    assert {e["container_no"] for e in body["items"]} == {HERO}


def test_events_endpoint_filters_by_location_type(client):
    for loc, expected in (("ECY", 2), ("CFS", 2)):
        body = client.get(f"/api/cfs-ecy/events?location_type={loc}").json()
        assert body["total"] == expected
        assert {e["location_type"] for e in body["items"]} == {loc}


def test_events_endpoint_filters_by_event_type_and_date(client):
    body = client.get("/api/cfs-ecy/events?event_type=ecy_out").json()
    assert body["total"] == 2
    body = client.get("/api/cfs-ecy/events?from=2026-07-05T00:00:00%2B05:30").json()
    assert body["total"] == 1 and body["items"][0]["event_type"] == "CFS_OUT"


def test_events_endpoint_rejects_unknown_filters(client):
    assert client.get("/api/cfs-ecy/events?location_type=RAIL").status_code == 400
    assert client.get("/api/cfs-ecy/events?event_type=NOPE").status_code == 400


# --- G. KPI API --------------------------------------------------------------
def test_empty_trt_endpoint_returns_the_kpi_envelope(client):
    body = client.get("/api/cfs-ecy/empty-trt").json()
    k = body["kpi"]
    assert k["key"] == "trt_empty_ecd"
    assert (k["target"], k["baseline"], k["unit"]) == (45.0, 72.0, "min")
    assert k["source"] == "live" and k["n"] == 1
    assert body["definition"]["measure"].startswith("ECY gate-out")


def test_empty_trt_reports_the_source_inventory_for_the_anomaly(client):
    src = client.get("/api/cfs-ecy/empty-trt").json()["source"]
    assert src["ecy_out_events"] == ECY_OUT
    assert src["ecy_in_events"] == ECY_IN
    assert src["ecy_pairing_gap"] == ECY_OUT - ECY_IN
    assert src["cfs_paired"] is True
    assert src["total_events"] == ECY_ROWS + CFS_ROWS


def test_empty_trt_exposes_the_dq_findings(client):
    body = client.get("/api/cfs-ecy/empty-trt").json()
    types = {i["issue_type"] for i in body["data_quality"]}
    assert "count_mismatch" in types
    assert any(str(ECY_OUT) in i["description"] for i in body["data_quality"])


def test_empty_trt_falls_back_to_a_labelled_baseline_with_no_chains():
    from gateway.main import app
    from gateway.routers import cfs_ecy as cfs_ecy_router

    svc = EmptyTrtService(repository=FakeTrtRepo(chains=[], events=[]))
    app.dependency_overrides[cfs_ecy_router.get_trt_service] = lambda: svc
    try:
        with TestClient(app) as c:
            k = c.get("/api/cfs-ecy/empty-trt").json()["kpi"]
        assert k["source"] == "baseline" and k["n"] == 0 and k["value"] == 72.0
    finally:
        app.dependency_overrides.pop(cfs_ecy_router.get_trt_service, None)


def test_chains_endpoint_filters_by_status_and_anomaly(client):
    body = client.get("/api/cfs-ecy/empty-trt/chains?chain_status=COMPLETE").json()
    assert body["total"] == 1 and body["items"][0]["container_no"] == HERO
    body = client.get("/api/cfs-ecy/empty-trt/chains?anomaly_code=NO_CFS_IN").json()
    assert body["total"] == 1
    assert body["items"][0]["anomaly_labels"] == [
        "an ECY gate-OUT that never reached a CFS"]
    assert client.get(
        "/api/cfs-ecy/empty-trt/chains?chain_status=BROKEN").status_code == 400


def test_container_endpoint_returns_the_chain_and_events(client):
    body = client.get(f"/api/cfs-ecy/empty-trt/containers/{HERO.lower()}").json()
    assert body["container_no"] == HERO
    assert body["counts_toward_kpi"] is True
    assert [l["leg"] for l in body["legs"]] == [
        "ECY_GATE_OUT", "CFS_GATE_IN", "CFS_GATE_OUT"]
    assert all(l["present"] for l in body["legs"])
    assert [e["event_type"] for e in body["events"]] == ["ECY_OUT", "CFS_IN", "CFS_OUT"]
    assert body["trt_min"] == 240.0


def test_container_endpoint_marks_an_incomplete_chain_as_excluded(client):
    body = client.get("/api/cfs-ecy/empty-trt/containers/CCLU0578304").json()
    assert body["chain_status"] == "PARTIAL"
    assert body["counts_toward_kpi"] is False
    assert [l["present"] for l in body["legs"]] == [True, False, False]


def test_container_endpoint_404s_for_an_unknown_container(client):
    r = client.get("/api/cfs-ecy/empty-trt/containers/ZZZU0000000")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "container_not_found"


def test_anomaly_endpoint_lists_the_affected_containers(client):
    body = client.get("/api/cfs-ecy/empty-trt/anomalies/no_cfs_in").json()
    assert body["code"] == "NO_CFS_IN" and body["total"] == 1
    assert body["items"][0]["container_no"] == "CCLU0578304"


# --- H. Data Quality API -----------------------------------------------------
def test_dq_issues_endpoint_lists_and_filters(client):
    body = client.get("/api/dq/issues").json()
    assert body["total"] == len(_DQ)
    body = client.get("/api/dq/issues?source_table=core.container_event").json()
    assert body["total"] == len(_DQ)
    body = client.get("/api/dq/issues?severity=warn").json()
    assert body["total"] == 1 and body["items"][0]["issue_type"] == "count_mismatch"


def test_dq_issues_endpoint_rejects_an_unknown_severity(client):
    r = client.get("/api/dq/issues?severity=fatal")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_severity"


def test_dq_summary_endpoint_rolls_up(client):
    body = client.get("/api/dq/summary").json()
    assert body["total"] == len(_DQ)
    assert body["warnings"] == 1 and body["info"] == 1 and body["errors"] == 0
    assert body["by_source_table"][0]["source_table"] == "core.container_event"


# ========================================================== live database layer
LIVE_DSN = os.environ.get("UC3_TEST_DSN")


def _live_rows(sql: str, params: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Run one query on a fresh event loop.

    jnpa_shared.db caches engines per DSN, and an engine bound to a finished
    asyncio.run() loop cannot be reused — so the cache is disposed each time.
    """
    from sqlalchemy import text

    from jnpa_shared.db import dispose_all, get_engine

    async def run():
        try:
            async with get_engine(LIVE_DSN).connect() as conn:
                res = await conn.execute(text(sql), params or {})
                return [dict(r) for r in res.mappings().all()]
        finally:
            await dispose_all()

    return asyncio.run(run())


live = pytest.mark.skipif(not LIVE_DSN,
                          reason="set UC3_TEST_DSN to run the live-database layer")


@live
def test_live_all_source_rows_are_present():
    rows = _live_rows(
        "SELECT event_type, count(*)::int AS n FROM core.container_event "
        "WHERE source_table IN ('staging ecy_codeco','staging cfs_codeco') "
        "GROUP BY event_type")
    counts = {r["event_type"]: r["n"] for r in rows}
    assert counts == {"ECY_OUT": ECY_OUT, "ECY_IN": ECY_IN,
                      "CFS_IN": CFS_IN, "CFS_OUT": CFS_OUT}
    assert sum(counts.values()) == ECY_ROWS + CFS_ROWS


@live
def test_live_location_type_is_correct():
    rows = _live_rows(
        "SELECT DISTINCT location_type, event_type FROM core.container_event "
        "WHERE source_table IN ('staging ecy_codeco','staging cfs_codeco')")
    for r in rows:
        assert r["event_type"].startswith(r["location_type"] + "_")


@live
def test_live_source_duplicate_is_preserved_not_collapsed():
    rows = _live_rows(
        "SELECT container_no, event_ts, event_type, count(*)::int AS n "
        "FROM core.container_event "
        "WHERE source_table IN ('staging ecy_codeco','staging cfs_codeco') "
        "GROUP BY 1,2,3 HAVING count(*) > 1")
    assert [r["container_no"] for r in rows] == [DUP_CONTAINER]
    assert rows[0]["n"] == 2


@live
def test_live_chain_and_kpi_match_the_source():
    rows = _live_rows(
        "SELECT chain_status, count(*)::int AS n "
        "FROM mart.v_empty_container_trt GROUP BY chain_status")
    by = {r["chain_status"]: r["n"] for r in rows}
    assert by["COMPLETE"] == VALID_CHAINS

    agg = _live_rows(
        "SELECT count(*)::int AS n, round(avg(trt_min), 2) AS avg_trt "
        "FROM mart.v_empty_container_trt WHERE chain_status = 'COMPLETE'")[0]
    assert agg["n"] == VALID_CHAINS
    samples = [float(r["trt_min"]) * 60.0 for r in _live_rows(
        "SELECT trt_min FROM mart.v_empty_container_trt "
        "WHERE chain_status = 'COMPLETE'")]
    assert float(agg["avg_trt"]) == round(kpi_engine.trt_empty_ecd_min(samples), 2)


@live
def test_live_hero_container_is_searchable():
    rows = _live_rows(
        "SELECT event_type, to_char(event_ts AT TIME ZONE 'Asia/Kolkata', "
        "       'DD/MM/YYYY HH24:MI') AS ist "
        "FROM core.container_event WHERE container_no = :cn "
        "AND source_table IN ('staging ecy_codeco','staging cfs_codeco') "
        "ORDER BY event_ts", {"cn": HERO})
    assert [(r["event_type"], r["ist"]) for r in rows] == [
        ("ECY_OUT", "01/07/2026 10:00"),
        ("CFS_IN", "01/07/2026 14:00"),
        ("CFS_OUT", "07/07/2026 08:16"),
    ]


@live
def test_live_dq_ledger_records_the_pairing_anomaly():
    rows = _live_rows(
        "SELECT issue_type, severity, description FROM core.dq_issue "
        "WHERE source_table = 'core.container_event'")
    assert rows, "no DQ findings recorded for core.container_event"
    types = {r["issue_type"] for r in rows}
    assert {"count_mismatch", "disjoint_ranges"} <= types
    headline = next(r for r in rows if r["issue_type"] == "count_mismatch")
    assert str(ECY_OUT) in headline["description"]
    assert str(ECY_IN) in headline["description"]


@live
def test_live_reimport_is_idempotent():
    """A second import inserts nothing and leaves the row count unchanged."""
    before = _live_rows(
        "SELECT count(*)::int AS n FROM core.container_event "
        "WHERE source_table IN ('staging ecy_codeco','staging cfs_codeco')")[0]["n"]
    try:
        corpus = I.find_corpus(None)
    except I.SourceError as exc:
        pytest.skip(f"corpus not available: {exc}")
    async def reimport():
        from jnpa_shared.db import dispose_all
        try:
            return await I.run_import(I.build(corpus), LIVE_DSN)
        finally:
            await dispose_all()

    stats = asyncio.run(reimport())
    after = _live_rows(
        "SELECT count(*)::int AS n FROM core.container_event "
        "WHERE source_table IN ('staging ecy_codeco','staging cfs_codeco')")[0]["n"]
    assert stats["events_inserted"] == 0
    assert stats["events_already_present"] == ECY_ROWS + CFS_ROWS
    assert after == before
