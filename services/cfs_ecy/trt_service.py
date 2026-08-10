"""UC3-003 empty-container lifecycle + TRT KPI — read orchestration.

Thin over :class:`EmptyTrtRepository`, mirroring :class:`CfsEcyService`: the
repository owns the SQL, this owns observability, the KPI arithmetic and the
response shaping.

KPI 3, "TRT for empty containers from ECD", is NOT re-derived here. The value is
handed to the project's existing engine — ``jnpa_shared.kpi`` — which supplies
the target (45 min), the baseline (72 min), the direction and the delta, exactly
as it does for the other three Appendix-C KPIs::

    trt_empty_ecd_min(samples_in_seconds)  ->  compute_kpi("trt_empty_ecd", …)

The samples are the ECY-gate-out -> CFS-gate-in leg of every COMPLETE chain,
which is that helper's documented meaning ("ECD pickup to gate-in"). Containers
whose chain is unpaired or incomplete contribute NO sample — they are reported
as anomalies instead, never averaged into the KPI.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Mapping, Optional

from jnpa_shared.logging import get_logger

from .trt_repository import EmptyTrtRepository

log = get_logger("services.cfs_ecy.trt_service")

# KPI identity in jnpa_shared.kpi.KPI_TARGETS — target/baseline/unit live there,
# not here, so the dashboard and this service can never disagree.
KPI_KEY = "trt_empty_ecd"

# What each chain-level anomaly code means, shipped with the response so the
# control room does not need the source workbook to read a flag. Mirrors
# services.cfs_ecy.chain_service.ANOMALY_LABELS and extends it with the ECY-side
# code that only the container_event feed can see.
ANOMALY_LABELS: Dict[str, str] = {
    "DUPLICATE_IN": "the CFS gate-IN is recorded more than once in the source",
    "MULTI_OUT": "more than one CFS gate-OUT recorded (the chain uses the latest)",
    "OUT_BEFORE_IN": "a CFS gate-OUT precedes the first CFS gate-IN",
    "ORPHAN_CFS_IN": "CFS activity with no ECY gate-OUT in the corpus",
    "NO_CFS_IN": "an ECY gate-OUT that never reached a CFS",
    "ECY_IN_WITHOUT_ECY_OUT": "an ECY gate-IN with no matching ECY gate-OUT",
}

# The lifecycle the KPI is measured over, named for the UI.
_LEG_LABELS = (
    ("ecy_out_ts", "ECY_GATE_OUT", "ECY gate-out (empty released from the depot)"),
    ("cfs_in_ts", "CFS_GATE_IN", "CFS gate-in"),
    ("cfs_out_ts", "CFS_GATE_OUT", "CFS gate-out"),
)


def _labels(codes: Optional[List[str]]) -> List[str]:
    return [ANOMALY_LABELS.get(c, c) for c in (codes or [])]


def _f(value: Any) -> Optional[float]:
    """Numeric(…) comes back as Decimal; JSON wants a float."""
    return None if value is None else float(value)


class EmptyTrtService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[EmptyTrtRepository] = None) -> None:
        self._repo = repository or EmptyTrtRepository(dsn=dsn)

    # ================================================================ events
    async def list_events(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> Dict[str, Any]:
        t0 = perf_counter()
        rows = await self._repo.list_events(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count_events(filters)
        log.info("empty_trt.events", extra={"total": total, "returned": len(rows),
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    # =================================================================== KPI
    async def kpi(self) -> Dict[str, Any]:
        """The KPI-3 result, plus everything needed to trust it.

        Returns ``kpi`` (the standard KpiResult envelope every other KPI uses),
        the distribution behind it, the source-feed inventory the 529/432
        anomaly is read off, and the DQ findings — so a reader can see the
        number AND why the excluded records were excluded.
        """
        from jnpa_shared import kpi as kpi_engine

        t0 = perf_counter()
        agg = await self._repo.trt_aggregate()
        statuses = await self._repo.chain_status_counts()
        anomalies = await self._repo.anomaly_counts()
        inventory = await self._repo.feed_inventory()
        files = await self._repo.source_files()
        daily = await self._repo.trt_daily()
        issues = await self._repo.dq_issues()

        valid = int(agg.get("valid_containers") or 0)
        avg = _f(agg.get("avg_trt_min"))
        target = kpi_engine.KPI_TARGETS[KPI_KEY]

        if valid and avg is not None:
            # ``avg`` is SQL's mean of the per-container trt_min samples — the
            # same arithmetic as kpi_engine.trt_empty_ecd_min(), evaluated in the
            # database so the 242 samples are not shipped just to be averaged.
            # tests/test_uc3_003_cfs_ecy_trt.py pins the two against each other.
            trend = [float(d["avg_trt_min"]) for d in daily
                     if d.get("avg_trt_min") is not None]
            result = kpi_engine.compute_kpi(KPI_KEY, round(avg, 2), trend=trend or None,
                                            source="live", n=valid).to_dict()
        else:
            # No COMPLETE chain in this database yet: show the configured baseline,
            # explicitly labelled, rather than a zero that reads like a measurement.
            result = kpi_engine.compute_kpi(KPI_KEY, target.baseline,
                                            source="baseline", n=0).to_dict()

        totals = {
            "ecy_out_events": 0, "ecy_in_events": 0,
            "cfs_in_events": 0, "cfs_out_events": 0, "total_events": 0,
        }
        for row in inventory:
            key = f"{row['event_type'].lower()}_events"
            if key in totals:
                totals[key] = int(row["events"])
            totals["total_events"] += int(row["events"])

        log.info("empty_trt.kpi", extra={"valid": valid, "avg_trt_min": avg,
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {
            "kpi": result,
            "definition": {
                "key": KPI_KEY,
                "label": target.label,
                "measure": "ECY gate-out → CFS gate-in (ECD pickup to gate-in)",
                "unit": target.unit,
                "target": target.target,
                "baseline": target.baseline,
                "direction": target.direction,
                "eligible": "chain_status = COMPLETE "
                            "(ECY-Out → CFS-In → CFS-Out, correctly ordered)",
            },
            "distribution": {
                "valid_containers": valid,
                "avg_trt_min": avg,
                "median_trt_min": _f(agg.get("median_trt_min")),
                "min_trt_min": _f(agg.get("min_trt_min")),
                "max_trt_min": _f(agg.get("max_trt_min")),
                "avg_dwell_min": _f(agg.get("avg_dwell_min")),
                "avg_cycle_min": _f(agg.get("avg_cycle_min")),
                "window_from": agg.get("window_from"),
                "window_to": agg.get("window_to"),
                "vs_target_min": (None if avg is None
                                  else round(avg - target.target, 2)),
                "vs_baseline_min": (None if avg is None
                                    else round(avg - target.baseline, 2)),
            },
            "chains": {
                "complete": statuses.get("COMPLETE", 0),
                "partial": statuses.get("PARTIAL", 0),
                "orphan": statuses.get("ORPHAN", 0),
                "total": sum(statuses.values()),
            },
            "source": {
                **totals,
                "files": files,
                # The evaluator's headline check, counted from the stored rows.
                "ecy_pairing_gap": abs(totals["ecy_out_events"] - totals["ecy_in_events"]),
                "cfs_paired": totals["cfs_in_events"] == totals["cfs_out_events"],
            },
            "anomalies": [{**a, "label": ANOMALY_LABELS.get(a["code"], a["code"])}
                          for a in anomalies],
            "data_quality": issues,
            "daily": daily,
        }

    # ================================================================ chains
    async def list_chains(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_chains(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count_chains(filters)
        for r in rows:
            r["anomaly_labels"] = _labels(r.get("anomaly_codes"))
        log.info("empty_trt.chains", extra={"total": total, "returned": len(rows)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def container(self, container_no: str) -> Optional[Dict[str, Any]]:
        """One container's full empty-container story: chain, legs, raw events.

        Returns None when the container has no CODECO event at all (router → 404).
        """
        cn = (container_no or "").strip().upper()
        chain = await self._repo.get_chain(cn)
        if chain is None:
            return None
        events = await self._repo.container_events(cn)
        chain["anomaly_labels"] = _labels(chain.get("anomaly_codes"))
        legs = [
            {"seq": i + 1, "leg": leg, "label": label,
             "ts": chain.get(col), "present": chain.get(col) is not None}
            for i, (col, leg, label) in enumerate(_LEG_LABELS)
        ]
        return {
            "container_no": cn,
            "chain": chain,
            "legs": legs,
            "events": events,
            "trt_min": _f(chain.get("trt_min")),
            "dwell_min": _f(chain.get("dwell_min")),
            "cycle_min": _f(chain.get("cycle_min")),
            "chain_status": chain.get("chain_status"),
            "counts_toward_kpi": chain.get("chain_status") == "COMPLETE",
        }

    # ============================================================= anomalies
    async def anomaly_containers(self, code: str, *, limit: int,
                                 offset: int) -> Dict[str, Any]:
        rows, total = await self._repo.unpaired_containers(code, limit=limit,
                                                           offset=offset)
        return {"code": code.strip().upper(),
                "label": ANOMALY_LABELS.get(code.strip().upper(), code),
                "items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}
