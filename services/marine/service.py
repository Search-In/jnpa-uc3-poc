"""UC-I Marine vessel-call read orchestration.

Thin over :class:`VesselCallRepository`: list/search/paginate vessel calls, resolve a
call by VCN or short VIA, one call with its ordered actuals, and KPI aggregates for the
dashboard. Read-only; the ingestion write path lands in a later slice as a separate
upload service, exactly as :class:`services.berthing.BerthingUploadService` does for
berthing.

No business logic lives here — the envelope assembly below is the same shape
:class:`services.berthing.BerthingService` produces, so routers stay declarative.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from jnpa_shared.logging import get_logger

from .manual_pilot import ManualPilotService
from .projection import MarineProjection, project
from .pilot_milestones import PilotMilestoneService, merge_events
from .repository import VesselCallRepository

log = get_logger("services.marine.service")


class VesselCallService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VesselCallRepository] = None,
                 projection: Optional[MarineProjection] = None,
                 manual: Optional[ManualPilotService] = None,
                 milestones: Optional[PilotMilestoneService] = None) -> None:
        self._repo = repository or VesselCallRepository(dsn)
        self._projection = projection or MarineProjection(dsn)
        # Single-call paths need the manual assignment directly. The batched LIST path
        # gets it inside MarineProjection, which is why the two diverged before this.
        # Injectable (defaulted) so the timeline merge is testable without a database.
        self._manual = manual or ManualPilotService(dsn)
        # Pilot-movement milestones core.pilotage recorded but the event ledger never
        # carried. Injectable for the same reason `manual` is: the timeline merge must be
        # testable without a database.
        self._milestones = milestones or PilotMilestoneService(dsn)

    async def _attach_lifecycle(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach the derived lifecycle to each row, from the SHARED projection.

        ONE batched read for the whole page — the same `by_call_ids` call the berthing
        reconciliation uses — not one query per row. No rule is evaluated here: the
        projection is asked, and its answer is attached.

        The row's stored `status` is left EXACTLY as the parser wrote it. The two are
        different facts: `status` is the message stage the call reached, `lifecycle.status`
        is its operational state. Overwriting the first would destroy the parser's record
        and the source-vs-derived comparison.

        A call the projection cannot answer for keeps `lifecycle = None` — a real state
        (nothing ingested for it yet), not an error.
        """
        ids = [int(i["call_id"]) for i in items if i.get("call_id") is not None]
        if not ids:
            return items
        states = await self._projection.by_call_ids(ids)
        for row in items:
            p = states.get(int(row["call_id"])) if row.get("call_id") is not None else None
            row["lifecycle"] = p.to_dict() if p is not None else None
        return items

    async def list_calls(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                         limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_calls(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count(filters)
        items = await self._attach_lifecycle(items)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def count(self, filters: Mapping[str, Any]) -> int:
        return await self._repo.count(filters)

    async def get(self, call_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get(call_id)

    async def get_by_vcn(self, vcn: str) -> Optional[Dict[str, Any]]:
        return await self._repo.get_by_vcn(vcn)

    async def get_by_via(self, via_no: str) -> List[Dict[str, Any]]:
        """A short VIA may resolve to several calls — see VesselCallRepository."""
        return await self._repo.get_by_via(via_no)

    async def timeline(self, call_id: int) -> Optional[Dict[str, Any]]:
        """One call, its actuals, and the lifecycle derived from BOTH.

        The repository has already loaded the call and its events, so those need no second
        query and the repository's SQL is untouched.

        THE THIRD INPUT. Pilot state is `imported OR manual OR pending`, and the manual
        half lives in core.manual_pilot_assignment — a table the repository's timeline
        query does not read. Calling ``project()`` with only the call and its events
        therefore returned `Pilot = Pending` for a vessel that HAD a manual pilot, while
        the list endpoint — which goes through MarineProjection, and so fetches it —
        showed the same call correctly. That divergence was the whole defect: two paths
        into one pure function, one of them supplying an incomplete input.

        ``resolve_effective_pilot`` is the single reader for that input, and it applies the
        imported-wins predicate in SQL, so this method still evaluates no rule of its own.

        ``project()`` remains the SAME pure function every other consumer uses, so there is
        one implementation of the lifecycle, not two.
        """
        call = await self._repo.timeline(call_id)
        if call is None:
            return None
        manual = await self._manual.resolve_effective_pilot(int(call_id))

        # THE FOURTH INPUT. core.pilotage times first line, all fast, pilot away and berth
        # cleared, and the ledger carries none of them — so the pane showed nothing between
        # boarding and berthing for a movement the port had timed to the minute. Merged
        # here through the SAME helper the batched projection uses, ledger first, so an
        # imported milestone always wins a same-instant duplicate.
        events = merge_events(call.get("events") or (),
                              await self._milestones.by_call_id(int(call_id)))
        # The pane renders `events` as its Actuals list, so the merged set is what an
        # operator reads AND what the lifecycle is derived from — one set, never two.
        call["events"] = events
        call["lifecycle"] = project(call, events, manual).to_dict()
        return call

    async def list_events(self, call_id: int, *, limit: int,
                          offset: int) -> List[Dict[str, Any]]:
        return await self._repo.list_events(call_id, limit=limit, offset=offset)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
