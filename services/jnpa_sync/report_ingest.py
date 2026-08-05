"""Report-group ingestion (Phase 3): land-raw-then-map.

The two report groups (``berthing-reports`` / ``daily-reports``) are served as
JSON (defect D9: a 5-field envelope, no pagination, no file references) whose
item schema is UNDOCUMENTED (defect D10). They are therefore ingested very
differently from the indexed file groups:

  1. ONE CALL RETURNS EVERYTHING. The live report endpoint is not paginated and
     ignores ``limit``: a single ``GET /v2/groups/{group}/records`` returns the
     full set of report items, each self-describing with its own ``reportDate``
     (+ ``terminal`` for the per-terminal berthing report). We fetch once and
     bucket the items by their own (reportDate, terminal); ``date_from`` /
     ``date_to`` optionally clip the buckets. Reports are small (tens of items),
     so re-fetching each pass is cheap and the content-sha dedup below turns an
     unchanged bucket into a no-op.

  2. LAND THE RAW SNAPSHOT FIRST. A non-empty answer is written verbatim into
     ``core.api_report_snapshot`` (content-sha dedup: re-polling unchanged
     content is a no-op — ``insert_report_snapshot`` returns None). Empty answers
     are NOT evidence and are skipped cheaply. The raw snapshot is the audit
     trail regardless of whether mapping later succeeds.

  3. THEN MAP onto the SAME validated upload pipeline the manual dump import
     uses — berthing rows are rendered to the berthing CSV template and fed
     through ``BerthingUploadService.import_file`` (sha-ledger + dedup); daily
     rows through ``UploadService.import_file("daily_status", ...)``. No new
     core tables, no raw SQL into report tables. When the item shape cannot be
     mapped, the snapshot is left RAW_ONLY (honest) — the raw evidence remains.

Every pass is wrapped in an ``api_ingest_run`` (counters in ``detail``) and every
drained client DefectObservation is logged, exactly like the indexed path.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:  # Asia/Kolkata is the API's declared timezone (service description)
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo always present on 3.13
    IST = timezone(timedelta(hours=5, minutes=30))

from jnpa_shared.logging import get_logger

from integrations.jnpa_portdata import JnpaError

from .report_mappers import (
    map_berthing_items,
    map_daily_items,
    render_berthing_csv,
    render_daily_csv,
)

log = get_logger("services.jnpa_sync.report_ingest")

UPLOADED_BY = "jnpa-api"
BERTHING_GROUP = "berthing-reports"
DAILY_GROUP = "daily-reports"
REPORT_GROUPS = (BERTHING_GROUP, DAILY_GROUP)

# The five JNPA container terminals — the berthing report is issued per-terminal
# (mirrors services.berthing.upload_parsers.TERMINALS).
BERTHING_TERMINALS = ("APMT", "NSICT", "NSIGT", "BMCT", "NSFT")

DEFAULT_LOOKBACK_DAYS = 7


class ReportSinks:
    """The two validated upload services mapped report rows are fed through.

    Resolved lazily from the sync service's DSN in production; injected as fakes
    in tests via ``service._report_sinks``."""

    def __init__(self, *, berthing: Any = None, daily: Any = None) -> None:
        self.berthing = berthing
        self.daily = daily


def _resolve_sinks(service: Any) -> ReportSinks:
    """The upload-service sinks: honour an injected ``service._report_sinks``,
    otherwise build the real services from the repository's DSN (constructed but
    never called on an empty poll — cheap)."""
    injected = getattr(service, "_report_sinks", None)
    sinks = injected if isinstance(injected, ReportSinks) else ReportSinks()
    if sinks.berthing is not None and sinks.daily is not None:
        return sinks
    dsn = getattr(getattr(service, "_repo", None), "_dsn", None)
    if sinks.berthing is None:
        from services.berthing.upload_service import BerthingUploadService

        sinks.berthing = BerthingUploadService(dsn=dsn)
    if sinks.daily is None:
        from services.performance.upload_service import UploadService

        sinks.daily = UploadService(dsn=dsn)
    return sinks


def _to_date(value: Any) -> Optional[_date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return _date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None


def _daterange(start: _date, end: _date) -> List[_date]:
    days: List[_date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _map_items(group: str, items: List[Dict[str, Any]], *, report_date: str,
               terminal: Optional[str]):
    if group == BERTHING_GROUP:
        return map_berthing_items(items, report_date=report_date,
                                  terminal=terminal)
    return map_daily_items(items, report_date=report_date)


async def _persist(service: Any, group: str, outcome: Any, *, sinks: ReportSinks,
                   report_date: str, terminal: Optional[str]):
    """Feed mapped rows through the validated upload pipeline. Returns
    (final_status, detail). Never raises — any sink fault degrades to
    MAP_FAILED with the raw snapshot already landed."""
    base: Dict[str, Any] = {
        "map_status": outcome.status,
        "notes": outcome.notes,
        "unmapped_keys": outcome.unmapped_keys,
        "rows": len(outcome.rows),
    }
    if outcome.status != "MAPPED":
        return outcome.status, base
    try:
        if group == BERTHING_GROUP:
            csv_bytes = render_berthing_csv(outcome.rows)
            if not csv_bytes:
                return "RAW_ONLY", {
                    **base,
                    "notes": "berthing template columns unsatisfiable from "
                             "mapped keys"}
            filename = f"api-report-{group}-{report_date}-{terminal or 'ALL'}.csv"
            result = await sinks.berthing.import_file(
                terminal, csv_bytes, filename, UPLOADED_BY)
            imp = str(result.get("status") or "").upper()
            landed = imp in ("SUCCESS", "PARTIAL", "SKIPPED_DUPLICATE")
            detail = {**base, "sink": "berthing", "import_status": imp,
                      "file_id": result.get("file_id"),
                      "imported": result.get("imported"), "filename": filename}
            return ("MAPPED" if landed else "MAP_FAILED"), detail

        # daily-reports -> the performance daily_status upload
        csv_bytes = render_daily_csv(outcome.rows)
        if not csv_bytes:
            return "RAW_ONLY", {
                **base,
                "notes": "daily_status template columns unsatisfiable from "
                         "mapped keys"}
        filename = f"api-report-{group}-{report_date}.csv"
        result = await sinks.daily.import_file(
            "daily_status", csv_bytes, filename, UPLOADED_BY)
        imp = str(result.get("status") or "").upper()
        landed = imp in ("IMPORTED", "SUCCESS", "PARTIAL")
        detail = {**base, "sink": "performance.daily_status",
                  "import_status": imp, "upload_id": result.get("upload_id"),
                  "inserted": result.get("inserted"), "filename": filename}
        return ("MAPPED" if landed else "MAP_FAILED"), detail
    except Exception as exc:  # noqa: BLE001 - outcome, not crash
        log.warning("jnpa_report_persist_failed", group=group,
                    report_date=report_date, terminal=terminal, error=str(exc))
        return "MAP_FAILED", {**base, "error": f"{type(exc).__name__}: {exc}"}


def _report_date_of(item: Dict[str, Any]) -> Optional[str]:
    """The item's own report date (ISO yyyy-mm-dd) from any known date key."""
    for key in ("reportDate", "report_date", "date", "day"):
        raw = item.get(key)
        if raw:
            parsed = _to_date(raw)
            if parsed:
                return parsed.isoformat()
    return None


def _report_terminal(group: str, item: Dict[str, Any]) -> Optional[str]:
    """The item's terminal (berthing report only); daily reports are port-wide."""
    if group != BERTHING_GROUP:
        return None
    value = item.get("terminal")
    text = str(value).strip() if value is not None else ""
    return text or None


async def sync_report_group(service: Any, group: str, *, trigger: str = "MANUAL",
                            dry_run: bool = False,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None) -> Dict[str, Any]:
    """Fetch a report group in ONE call, bucket its items by their own
    (reportDate, terminal), land raw snapshots and map them onto the validated
    upload pipeline. See the module docstring."""
    if group not in REPORT_GROUPS:
        return {"status": "SKIPPED", "group": group, "items_total": 0,
                "buckets": 0, "snapshots_new": 0, "mapped": 0, "raw_only": 0,
                "map_failed": 0, "reason": f"{group!r} is not a report group"}

    client = service._client
    repo = service._repo
    api_mode = getattr(service, "_api_mode", "LIVE")

    today = datetime.now(IST).date()
    lo = _to_date(date_from)                       # optional lower clip
    hi = _to_date(date_to) or today                # never accept the future
    if hi > today:
        hi = today

    run_id: Optional[int] = None
    if not dry_run:
        run_id = await repo.open_run(trigger=trigger, group=group,
                                     api_mode=api_mode)
    sinks = None if dry_run else _resolve_sinks(service)

    counters = {"api_calls": 0, "items_total": 0, "buckets": 0,
                "skipped_range": 0, "snapshots_new": 0, "mapped": 0,
                "raw_only": 0, "map_failed": 0}
    status, error_text = "OK", None
    max_date_seen: Optional[_date] = None

    try:
        client.request_stats(reset=True)
        # The live report endpoint is not paginated — one call returns every item.
        counters["api_calls"] += 1
        envelope = await client.get_report(group)
        items = [it for it in (envelope.items or []) if isinstance(it, dict)]
        counters["items_total"] = len(items)

        # Bucket by the item's OWN (reportDate, terminal).
        as_of_date = (envelope.asOf or "")[:10] or today.isoformat()
        buckets: Dict[tuple, List[Dict[str, Any]]] = {}
        for item in items:
            date_iso = _report_date_of(item) or as_of_date
            parsed = _to_date(date_iso)
            if (lo and parsed and parsed < lo) or (hi and parsed and parsed > hi):
                counters["skipped_range"] += 1
                continue
            terminal = _report_terminal(group, item)
            buckets.setdefault((date_iso, terminal), []).append(item)
            if parsed and (max_date_seen is None or parsed > max_date_seen):
                max_date_seen = parsed
        counters["buckets"] = len(buckets)

        if not dry_run:
            for (date_iso, terminal), grp_items in sorted(
                    buckets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
                payload = {"group": envelope.group or group,
                           "delivery": envelope.delivery,
                           "report_date": date_iso, "terminal": terminal,
                           "count": len(grp_items), "items": grp_items}
                snapshot_id = await repo.insert_report_snapshot(
                    group=group, report_date=date_iso, terminal=terminal,
                    payload=payload, item_count=len(grp_items),
                    ingest_run_id=run_id)
                if snapshot_id is None:
                    continue                      # unchanged content — dedup no-op
                counters["snapshots_new"] += 1
                outcome = _map_items(group, grp_items, report_date=date_iso,
                                     terminal=terminal)
                final_status, detail = await _persist(
                    service, group, outcome, sinks=sinks,
                    report_date=date_iso, terminal=terminal)
                await repo.update_report_mapped(snapshot_id, status=final_status,
                                                detail=detail)
                if final_status == "MAPPED":
                    counters["mapped"] += 1
                elif final_status == "MAP_FAILED":
                    counters["map_failed"] += 1
                else:
                    counters["raw_only"] += 1
    except JnpaError as exc:
        status, error_text = "ERROR", str(exc)
    except Exception as exc:  # noqa: BLE001 - isolate the group's failure
        status, error_text = "ERROR", str(exc)
        log.warning("jnpa_report_ingest_unexpected", group=group, error=str(exc))

    stats = client.request_stats(reset=True)
    observations = client.drain_observations()

    if dry_run:
        return {"status": "DRY_RUN", "group": group,
                "items_total": counters["items_total"],
                "buckets": counters["buckets"], "snapshots_new": 0, "mapped": 0,
                "raw_only": 0, "map_failed": 0, "api_calls": counters["api_calls"],
                "observations": [o.code for o in observations]}

    run_counters = {"records_listed": counters["items_total"],
                    "request_count": stats.request_count,
                    "rate_limit_remaining_min": stats.rate_limit_remaining_min}
    await repo.log_defects(observations, run_id)
    await repo.close_run(run_id, status=status, counters=run_counters,
                         detail=counters, error=error_text)
    # Watermark = the latest report date observed (content-sha dedup makes a
    # re-fetch of the same boundary harmless).
    if status == "OK":
        wm = max_date_seen or today
        await repo.upsert_sync_state(
            group,
            watermark_ts=datetime(wm.year, wm.month, wm.day, tzinfo=IST),
            last_run_id=run_id, last_status=status)
    else:
        await repo.upsert_sync_state(group, last_run_id=run_id,
                                     last_status=status)

    result = {"status": status, "group": group,
              "items_total": counters["items_total"],
              "buckets": counters["buckets"],
              "snapshots_new": counters["snapshots_new"],
              "mapped": counters["mapped"], "raw_only": counters["raw_only"],
              "map_failed": counters["map_failed"], "run_id": run_id}
    if error_text:
        result["error"] = error_text
    log.info("jnpa_report_ingest_done",
             **{k: v for k, v in result.items() if k != "group"})
    return result


__all__ = ["sync_report_group", "ReportSinks", "REPORT_GROUPS",
           "BERTHING_TERMINALS"]
