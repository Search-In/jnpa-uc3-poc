"""JnpaSyncService — the incremental poller that keeps the RDS in step with
the JNPA Port-Data API, plus the gateway lifespan loop.

The sanctioned incremental pattern (API Reference §4.4) with the known
hazards defended:

  * ``since = watermark − 1s`` + ON CONFLICT record dedup — the API's
    exclusive ``since`` runs over a NON-unique publishedAt (ties observed in
    the captures), so resuming exactly at the watermark silently skips tied
    records; rewinding one second re-reads the boundary and the record_id
    conflict absorbs the overlap;
  * checksum-first downloads — a record whose sha256 is already in ANY
    import ledger (a dump-loaded file) or in the raw store is never
    downloaded again (the dump-vs-API dual-source guarantee);
  * per-group advisory lock — multi-worker lifespans and manual+scheduled
    overlap cannot double-sync;
  * bad_cursor fallback — a mid-pagination cursor rejection restarts the
    group once from the watermark (dedup absorbs the re-read);
  * every drained DefectObservation lands in core.api_defect_log (JNPA's
    31-Jul notice REQUIRES observed defects to be reported) and every run in
    core.api_ingest_run (the D1-3 evidence trail).
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from integrations.jnpa_portdata import (
    IndexedRecord,
    JnpaError,
    JnpaHTTPError,
    JnpaNotConfigured,
    JnpaPortDataClient,
)

from .repository import SyncRepository
from .routing import (
    ALL_GROUPS,
    INDEXED_GROUPS,
    REPORT_GROUPS,
    STATIC_GROUPS,
    JnpaRouter,
)
from .store import ApiFileStore

log = get_logger("services.jnpa_sync.service")

# One-second rewind: the exclusive-since boundary-tie defense.
WATERMARK_REWIND = timedelta(seconds=1)
DEFAULT_STORE_DIR = "data/jnpa_api"


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class JnpaSyncService:
    def __init__(self, dsn: Optional[str] = None, *,
                 client: Optional[JnpaPortDataClient] = None,
                 store: Optional[ApiFileStore] = None,
                 repository: Optional[SyncRepository] = None,
                 router: Optional[JnpaRouter] = None,
                 store_dir: Optional[str] = None,
                 api_mode: str = "LIVE") -> None:
        self._client = client or JnpaPortDataClient()
        self._repo = repository or SyncRepository(dsn)
        self._store = store or ApiFileStore(store_dir or DEFAULT_STORE_DIR)
        self._router = router or JnpaRouter(dsn)
        self._api_mode = api_mode

    @property
    def client(self) -> JnpaPortDataClient:
        return self._client

    # ------------------------------------------------------------ overview
    async def health(self) -> Dict[str, Any]:
        """The read model behind GET /api/integrations/jnpa/health."""
        states = {s["group_slug"]: s for s in await self._repo.list_sync_state()}
        runs = await self._repo.list_runs(limit=1)
        return {
            "configured": self._client.configured,
            "mode": self._api_mode if self._client.configured else "DISABLED",
            "api_url": self._client.api_url,
            "groups": [
                {
                    "group": group,
                    "kind": self._router.kind(group),
                    "watermark_ts": (states.get(group) or {}).get("watermark_ts"),
                    "last_status": (states.get(group) or {}).get("last_status"),
                    "updated_at": (states.get(group) or {}).get("updated_at"),
                }
                for group in ALL_GROUPS
            ],
            "last_run": runs[0] if runs else None,
        }

    # ------------------------------------------------------------- sync all
    async def sync_all(self, *, trigger: str = "SCHEDULED",
                       dry_run: bool = False) -> Dict[str, Any]:
        """One pass over every group. A failing group never stalls the
        rest; report groups are handled by report_ingest (Phase 3) and
        skipped gracefully until it lands; static groups are recorded as
        SKIPPED_STATIC (the dump remains their source)."""
        results: Dict[str, Any] = {}
        for group in INDEXED_GROUPS:
            try:
                results[group] = await self.sync_group(group, trigger=trigger,
                                                       dry_run=dry_run)
            except JnpaNotConfigured:
                raise
            except JnpaError as exc:
                results[group] = {"status": "ERROR", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - isolate group failures
                log.warning("jnpa_sync_group_crashed", group=group,
                            error=str(exc))
                results[group] = {"status": "ERROR", "error": str(exc)}
        for group in REPORT_GROUPS:
            try:
                results[group] = await self.sync_reports(group, trigger=trigger,
                                                         dry_run=dry_run)
            except JnpaError as exc:
                results[group] = {"status": "ERROR", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.warning("jnpa_sync_reports_crashed", group=group,
                            error=str(exc))
                results[group] = {"status": "ERROR", "error": str(exc)}
        for group in STATIC_GROUPS:
            if not dry_run:
                await self._repo.upsert_sync_state(group,
                                                   last_status="SKIPPED_STATIC")
            results[group] = {"status": "SKIPPED_STATIC",
                              "reason": "static delivery — served empty by "
                                        "the API; the sample-pack dump is "
                                        "the source"}
        return results

    # ----------------------------------------------------------- sync group
    async def sync_group(self, group: str, *, trigger: str = "MANUAL",
                         dry_run: bool = False) -> Dict[str, Any]:
        """Incrementally sync one indexed group."""
        if group not in INDEXED_GROUPS:
            raise ValueError(f"{group!r} is not an indexed group")
        if not self._client.configured:
            raise JnpaNotConfigured("JNPA_PORTDATA_CLIENT_KEY is not set")

        async with self._repo.group_lock(group) as won:
            if not won:
                log.info("jnpa_sync_lock_busy", group=group)
                return {"status": "LOCKED",
                        "reason": "another sync of this group is running"}
            return await self._sync_group_locked(group, trigger=trigger,
                                                 dry_run=dry_run)

    async def _sync_group_locked(self, group: str, *, trigger: str,
                                 dry_run: bool) -> Dict[str, Any]:
        state = await self._repo.get_sync_state(group)
        watermark: Optional[datetime] = (state or {}).get("watermark_ts")
        since = (watermark - WATERMARK_REWIND) if watermark else None

        run_id = None
        if not dry_run:
            run_id = await self._repo.open_run(trigger=trigger, group=group,
                                               api_mode=self._api_mode)
        counters = {k: 0 for k in (
            "records_listed", "records_new", "records_duplicate",
            "files_downloaded", "files_304", "files_skipped_checksum",
            "bytes_downloaded")}
        max_published = watermark
        status, error_text = "OK", None

        try:
            self._client.request_stats(reset=True)
            async for record in self._iter_with_cursor_fallback(group, since):
                counters["records_listed"] += 1
                published = _parse_ts(record.publishedAt)
                if dry_run:
                    continue
                landed = await self._land_record(group, record, run_id,
                                                 counters)
                if landed and published is not None:
                    if max_published is None or published > max_published:
                        max_published = published
        except JnpaError as exc:
            status, error_text = "ERROR", str(exc)
        except Exception as exc:  # noqa: BLE001
            status, error_text = "ERROR", str(exc)
            log.warning("jnpa_sync_unexpected", group=group, error=str(exc))

        stats = self._client.request_stats(reset=True)
        observations = self._client.drain_observations()

        if dry_run:
            return {"status": "DRY_RUN", "group": group,
                    "records_listed": counters["records_listed"],
                    "watermark_ts": watermark.isoformat() if watermark else None,
                    "observations": [o.code for o in observations]}

        if status == "OK" and counters["records_listed"] and (
                counters["records_new"] == 0
                and counters["records_duplicate"] == counters["records_listed"]):
            # A pure boundary re-read — normal, not noteworthy.
            pass

        counters["request_count"] = stats.request_count
        counters["rate_limit_remaining_min"] = stats.rate_limit_remaining_min
        await self._repo.log_defects(observations, run_id)
        await self._repo.close_run(run_id, status=status,
                                   counters=counters, error=error_text)
        await self._repo.upsert_sync_state(
            group,
            watermark_ts=max_published,
            last_cursor=None,
            last_run_id=run_id,
            last_status=status)
        result = {"status": status, "group": group, "run_id": run_id,
                  **counters}
        if error_text:
            result["error"] = error_text
        log.info("jnpa_sync_group_done", group=group, **{
            k: v for k, v in result.items() if k not in ("group",)})
        return result

    async def _iter_with_cursor_fallback(self, group: str,
                                         since: Optional[datetime]):
        """iter_records with ONE restart on 400 bad_cursor (cursors share
        the fileRef namespace upstream and are signed — a server-side reset
        mid-walk must not kill the run; dedup absorbs the re-read)."""
        attempts = 0
        while True:
            try:
                async for record in self._client.iter_records(
                        group, since=since, order="asc", limit=500):
                    yield record
                return
            except JnpaHTTPError as exc:
                if exc.is_bad_cursor and attempts == 0:
                    attempts += 1
                    log.warning("jnpa_bad_cursor_restart", group=group)
                    continue
                raise

    async def _land_record(self, group: str, record: IndexedRecord,
                           run_id: Optional[int],
                           counters: Dict[str, int]) -> bool:
        """Land one record: api_record row -> checksum dedup -> download ->
        raw store -> route. Returns True when the record counts toward the
        watermark (i.e. it is fully processed, new or already-known)."""
        file_meta = record.file
        inserted = await self._repo.insert_record(
            record_id=record.recordId,
            group=group,
            message_type=record.messageType,
            message_name=record.messageName,
            published_at=_parse_ts(record.publishedAt),
            container_count=record.containerCount,
            vessel_call=record.vesselCall,
            summary=record.summary,
            file_ref=file_meta.fileRef if file_meta else None,
            media_type=file_meta.mediaType if file_meta else None,
            size_bytes=file_meta.sizeBytes if file_meta else None,
            checksum_sha256=file_meta.checksumSha256 if file_meta else None,
            ingest_run_id=run_id,
            payload=record.model_dump(exclude_none=True))
        if inserted is None:
            counters["records_duplicate"] += 1
            return True                      # boundary re-read: fully known
        counters["records_new"] += 1

        if file_meta is None:
            await self._repo.update_record_routing(
                record.recordId, routed_status="UNROUTED",
                routed_service=group)
            return True

        sha = (file_meta.checksumSha256 or "").lower() or None
        if sha:
            known = await self._repo.known_sha(sha)
            if known:
                counters["files_skipped_checksum"] += 1
                await self._repo.update_record_routing(
                    record.recordId,
                    routed_service=known["source"],
                    routed_status="SKIPPED_DUPLICATE")
                log.info("jnpa_checksum_skip", group=group,
                         sha=sha[:12], source=known["source"])
                return True

        fetched = await self._client.fetch_file(
            file_meta.fileRef, expected_sha256=file_meta.checksumSha256)
        if fetched.not_modified:
            counters["files_304"] += 1
            await self._repo.update_record_routing(
                record.recordId, routed_status="SKIPPED_DUPLICATE",
                routed_service="etag_304")
            return True
        counters["files_downloaded"] += 1
        counters["bytes_downloaded"] += fetched.size_bytes

        filename = fetched.filename or f"{file_meta.fileRef}.bin"
        stored_path = self._store.save(group, fetched.sha256 or "nosha",
                                       filename, fetched.content or b"")
        outcome = await self._router.route(
            group, filename=filename, content=fetched.content or b"",
            message_type=record.messageType)
        await self._repo.update_record_routing(
            record.recordId,
            stored_path=stored_path,
            routed_service=outcome.service,
            routed_status=outcome.status,
            routed_file_id=outcome.file_id)
        return True

    # ------------------------------------------------------------- reports
    async def sync_reports(self, group: str, *, trigger: str = "MANUAL",
                           dry_run: bool = False,
                           date_from: Optional[str] = None,
                           date_to: Optional[str] = None) -> Dict[str, Any]:
        """Report groups (JSON delivery). Implemented by report_ingest
        (Phase 3); imported lazily so Phase 2 ships without it."""
        try:
            from .report_ingest import sync_report_group
        except ImportError:
            return {"status": "PENDING",
                    "reason": "report ingest not built yet (Phase 3)"}
        return await sync_report_group(
            self, group, trigger=trigger, dry_run=dry_run,
            date_from=date_from, date_to=date_to)

    # -------------------------------------------------------------- replay
    async def replay_unrouted(self, group: str) -> Dict[str, Any]:
        """Re-route records that landed UNROUTED, straight from the raw
        store — no re-download. Run after a new consumer is wired (Phase 4
        rail services; a future EDI consumer)."""
        records = await self._repo.list_records(group=group,
                                                routed_status="UNROUTED",
                                                limit=10_000)
        replayed = succeeded = 0
        for row in records:
            sha = (row.get("checksum_sha256") or "").lower()
            found = self._store.open_bytes(sha) if sha else None
            if found is None:
                continue
            content, filename = found
            outcome = await self._router.route(
                group, filename=filename, content=content,
                message_type=row.get("message_type"))
            replayed += 1
            if outcome.status not in ("UNROUTED", "FAILED", "REJECTED"):
                succeeded += 1
            await self._repo.update_record_routing(
                row["record_id"],
                routed_service=outcome.service,
                routed_status=outcome.status,
                routed_file_id=outcome.file_id)
        return {"group": group, "candidates": len(records),
                "replayed": replayed, "succeeded": succeeded}


# ---------------------------------------------------------------- scheduler
async def jnpa_sync_loop(state: Any, stop: asyncio.Event) -> None:
    """Gateway lifespan task: periodic sync_all. Mirrors mqtt_truck_pump's
    contract — reads config off state.cfg, never crashes boot, exits when
    the shared stop event is set. Started ONLY when a client key is
    configured (the Kafka-pump guard posture: TestClient runs stay
    task-free)."""
    cfg = state.cfg
    interval = float(getattr(cfg, "jnpa_sync_interval_s", 300) or 300)
    client = JnpaPortDataClient(
        getattr(cfg, "jnpa_portdata_api_url", "") or None,
        client_key=getattr(cfg, "jnpa_portdata_client_key", "") or None)
    service = JnpaSyncService(
        getattr(cfg, "postgres_dsn", None) or None,
        client=client,
        store_dir=getattr(cfg, "jnpa_store_dir", None) or DEFAULT_STORE_DIR,
        api_mode=getattr(cfg, "jnpa_api_mode", "LIVE"))
    # Initial jitter so a multi-worker deployment staggers its first pass
    # (the advisory lock already serialises; this avoids the thundering herd).
    try:
        await asyncio.wait_for(stop.wait(),
                               timeout=random.uniform(1.0, 5.0))
        return
    except asyncio.TimeoutError:
        pass
    log.info("jnpa_sync_loop_started", interval_s=interval)
    while not stop.is_set():
        try:
            await service.sync_all(trigger="SCHEDULED")
        except JnpaNotConfigured:
            log.info("jnpa_sync_loop_unconfigured")
            return
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.warning("jnpa_sync_loop_error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


__all__ = ["JnpaSyncService", "jnpa_sync_loop", "WATERMARK_REWIND"]
