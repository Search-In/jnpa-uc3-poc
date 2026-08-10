"""FASTag toll-crossing accumulator — the 72-hour retention workaround.

NLDSL retains only the **past 72 hours** of crossings per vehicle registration
number ("Data related to VRN will be available for past 72 Hours",
ulip-docs/ULIP_FASTAG_Integration_Requirement.pdf §1.3). That single sentence
decides the whole design of this module:

  * An on-demand ``FASTAG/01`` call at demo time returns nothing for any truck
    that last crossed a plaza more than three days ago — which is most of them.
  * Toll history therefore cannot be *assembled* at read time. It has to be
    *accumulated* before it is needed, by sweeping the active fleet on a cycle
    shorter than the retention window and persisting every crossing found.
  * Once persisted, ``core.fastag_transaction`` is the durable record and the
    live call is only a top-up. That is exactly how
    ``GET /api/fastag/transactions`` already reads (fetch, dedup-persist, then
    read BACK the stored history) — this poller is what fills it.

Dedup is free: ``seq_no`` is NETC's own idempotency token and is UNIQUE in the
table, so re-reading the same 72-hour window every cycle inserts each crossing
exactly once. Re-polling overlapping windows is therefore not just safe but
required — a gap longer than 72 h loses data permanently, with no way to
backfill it from any granted API.

Runs as a gateway lifespan task, mirroring
:func:`services.jnpa_sync.service.jnpa_sync_loop`: reads its config off
``state.cfg``, never crashes boot, exits when the shared stop event is set, and
starts only when a ULIP credential is actually configured.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, List, Optional

from integrations.ulip import UlipClient, UlipError, UlipNotConfigured
from jnpa_shared.logging import get_logger

from .mappers import map_fastag_transactions
from .service import FastagService

log = get_logger("services.fastag.poller")

# Sweep cadence. The default is deliberately far below the 72-hour retention
# window: at one pass per hour a fleet-wide outage would have to last three
# days before any crossing is lost.
DEFAULT_INTERVAL_S = 3600.0
# How far back to consider a vehicle "active" and worth polling. Wider than the
# retention window, because a truck seen 5 days ago may still have crossed a
# plaza yesterday.
DEFAULT_ACTIVE_DAYS = 7
# Ceiling on plates per pass, so one sweep cannot exhaust the subscription's
# call budget. Plates are ordered by most-recently-seen, so the cap drops the
# least relevant vehicles first — and the drop is always logged, never silent.
DEFAULT_MAX_PLATES = 500
# Pause between per-plate calls; ULIP is a shared national gateway and a tight
# loop over hundreds of plates reads as abuse.
DEFAULT_PACING_S = 0.2

_ACTIVE_PLATES_SQL = """
SELECT plate, MAX(ts) AS last_seen
FROM core.gate_event
WHERE plate IS NOT NULL
  AND plate <> ''
  AND ts > now() - CAST(:window AS interval)
GROUP BY plate
ORDER BY last_seen DESC
LIMIT :limit
"""


class FastagPoller:
    """Sweeps the active fleet through ``FASTAG/01`` and persists what it finds.

    Stateless apart from its collaborators, so a single instance is safe to
    reuse for the process lifetime.
    """

    def __init__(
        self,
        *,
        client: Optional[UlipClient] = None,
        service: Optional[FastagService] = None,
        dsn: Optional[str] = None,
        active_days: int = DEFAULT_ACTIVE_DAYS,
        max_plates: int = DEFAULT_MAX_PLATES,
        pacing_s: float = DEFAULT_PACING_S,
    ) -> None:
        self._client = client or UlipClient()
        self._service = service or FastagService(dsn=dsn)
        self._dsn = dsn
        self.active_days = max(1, int(active_days))
        self.max_plates = max(1, int(max_plates))
        self.pacing_s = max(0.0, float(pacing_s))

    async def active_plates(self) -> List[str]:
        """Plates seen at the gate within the active window, newest first.

        Sourced from ``core.gate_event`` rather than a vehicle registry: a
        vehicle that has not passed the gate recently cannot have produced a
        corridor toll crossing worth chasing.
        """
        if not self._dsn:
            return []
        from jnpa_shared.db import fetch_all

        try:
            rows = await fetch_all(
                _ACTIVE_PLATES_SQL,
                {"window": f"{self.active_days} days", "limit": self.max_plates},
                dsn=self._dsn,
            )
        except Exception as exc:  # noqa: BLE001 — a sweep must never crash
            log.warning("fastag_poller_plate_query_failed", error=str(exc))
            return []
        return [r["plate"] for r in rows if r.get("plate")]

    async def sweep_once(self) -> dict:
        """One full pass over the active fleet. Returns a summary dict.

        Never raises: a per-plate failure is counted and the sweep continues,
        because one unknown vehicle must not cost the whole fleet its history.
        """
        if not self._client.configured:
            raise UlipNotConfigured("no ULIP credential configured")
        plates = await self.active_plates()
        if len(plates) == self.max_plates:
            # Explicit, never silent: a capped sweep means some active vehicles
            # were NOT polled this cycle and may lose crossings to the 72-hour
            # window entirely.
            log.warning("fastag_poller_capped", limit=self.max_plates,
                        active_days=self.active_days,
                        note="some active plates were not polled this pass")
        inserted = skipped = failed = 0
        for index, plate in enumerate(plates):
            if index and self.pacing_s:
                await asyncio.sleep(self.pacing_s)
            try:
                envelope = await self._client.fetch_vehicle_movement(plate)
            except UlipError as exc:
                failed += 1
                log.debug("fastag_poller_fetch_failed", plate=plate,
                          error=f"{type(exc).__name__}")
                continue
            mapped = map_fastag_transactions(envelope.model_dump(), client_id="poller")
            if mapped.get("status") != "success":
                failed += 1
                continue
            if not mapped.get("db"):
                continue  # no crossings in the retained window — not a failure
            result = await self._service.process_transactions(mapped, client_id="poller")
            if result.get("status") != "SUCCESS":
                failed += 1
                continue
            inserted += int(result.get("inserted_count", 0))
            skipped += int(result.get("skipped_count", 0))
        summary = {"plates": len(plates), "inserted": inserted,
                   "skipped": skipped, "failed": failed}
        log.info("fastag_poller_sweep", **summary)
        return summary


async def fastag_poll_loop(state: Any, stop: asyncio.Event) -> None:
    """Gateway lifespan task: periodic :meth:`FastagPoller.sweep_once`.

    Mirrors ``jnpa_sync_loop``'s contract — config off ``state.cfg``, never
    crashes boot, exits when the shared stop event is set. Starts only when a
    ULIP credential is configured, so TestClient runs and credential-free
    deployments stay task-free.
    """
    cfg = state.cfg
    client = UlipClient()
    if not client.configured:
        log.info("fastag_poll_loop_unconfigured")
        return
    interval = float(getattr(cfg, "fastag_poll_interval_s", DEFAULT_INTERVAL_S)
                     or DEFAULT_INTERVAL_S)
    dsn = getattr(cfg, "postgres_dsn", None) or None
    poller = FastagPoller(
        client=client, dsn=dsn,
        service=FastagService(dsn=dsn),
        active_days=int(getattr(cfg, "fastag_poll_active_days", DEFAULT_ACTIVE_DAYS)
                        or DEFAULT_ACTIVE_DAYS),
        max_plates=int(getattr(cfg, "fastag_poll_max_plates", DEFAULT_MAX_PLATES)
                       or DEFAULT_MAX_PLATES),
    )
    # Initial jitter so a multi-worker deployment staggers its first pass.
    try:
        await asyncio.wait_for(stop.wait(), timeout=random.uniform(1.0, 5.0))
        return
    except asyncio.TimeoutError:
        pass
    log.info("fastag_poll_loop_started", interval_s=interval)
    while not stop.is_set():
        try:
            await poller.sweep_once()
        except UlipNotConfigured:
            log.info("fastag_poll_loop_unconfigured")
            return
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("fastag_poll_loop_error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


__all__ = ["FastagPoller", "fastag_poll_loop", "DEFAULT_INTERVAL_S"]
