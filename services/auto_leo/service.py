"""Auto-LEO reconciliation board (UC3-040).

Joins the four evidence streams per export truck and runs the SAME pure
reconciler the gate-data service uses (``gate-data/leo.py``), so the board and
the service can never disagree about whether a container is clear for a Let
Export Order.

What this layer adds over the pure function:

  * it sources the four records from RDS (``core.gate_capture``) instead of the
    in-memory seed, so the board reflects what was actually captured;
  * it marks each row's provenance per stream — a REAL corpus Form 13 is labelled
    REAL and the simulated weighbridge/ICEGATE feeds beside it are labelled
    SIMULATED, because gaps G8/G10 mean those two feeds do not exist in the
    corpus and must never be shown as though they did;
  * it attaches the X4 reroute record to a WEIGHT_MISSING row, so the flag comes
    with the remedy that was actually taken.
"""
from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from .repository import CAPTURE_TO_SOURCE, AutoLeoRepository, _payload

log = get_logger("services.auto_leo.service")

# The reconciler lives in the gate-data service package, whose directory name is
# hyphenated and therefore not importable as a module path. It is the single
# source of truth for the LEO rules, so it is loaded rather than reimplemented.
_GATE_DATA = Path(__file__).resolve().parents[2] / "gate-data"
if str(_GATE_DATA.parent) not in sys.path:
    sys.path.insert(0, str(_GATE_DATA.parent))


def _leo():
    """Import the pure reconciler lazily (keeps import order/test isolation simple)."""
    from gate_data import leo  # type: ignore
    return leo


#: Streams with no feed in the supplied corpus (gap register G8 / G10). Any row
#: sourced from these is SIMULATED, whatever the Form 13 beside it is.
SIMULATED_STREAMS = ("weighbridge", "icegate")

#: Assumption reference rendered beside a simulated stream.
ASSUMPTION_REF = "A-G8/G10"
ASSUMPTION_TEXT = (
    "Weighbridge and ICEGATE event feeds do not exist in the supplied corpus "
    "(gaps G8/G10). Those two streams are simulated around the REAL Form 13 "
    "values and badged SIMULATED. The four-way join logic is the deliverable; "
    "the two missing feeds are named post-award integrations."
)


def _record(source: str, payload: Dict[str, Any], row: Dict[str, Any]) -> Any:
    """Shape one capture row into the attribute bag the reconciler reads.

    A SimpleNamespace rather than the seed dataclasses: the reconciler only ever
    reads attributes, and building the frozen dataclasses here would require
    inventing values for fields a capture row does not carry.
    """
    container = row.get("container_no")
    if source == "eseal":
        return SimpleNamespace(
            eseal_id=payload.get("eseal_id"),
            container_no=container,
            status=payload.get("status") or row.get("status"),
            tamper_flag=bool(payload.get("tamper_flag")),
            captured_at=payload.get("captured_at"),
        )
    if source == "form13":
        wt = payload.get("gross_wt_kg")
        return SimpleNamespace(
            form13_no=payload.get("form13_no"),
            container_no=container,
            shipping_bill_no=payload.get("shipping_bill_no"),
            cargo_desc=payload.get("cargo_desc"),
            gross_wt_kg=int(wt) if wt not in (None, "") else 0,
        )
    if source == "weighbridge":
        wt = payload.get("measured_wt_kg")
        return SimpleNamespace(
            vehicle_plate=payload.get("vehicle_plate") or row.get("vehicle_plate"),
            container_no=container,
            measured_wt_kg=int(wt) if wt not in (None, "") else 0,
            axle_count=payload.get("axle_count"),
            captured_at=payload.get("captured_at"),
        )
    return SimpleNamespace(
        shipping_bill_no=payload.get("shipping_bill_no"),
        container_no=container,
        leo_status=payload.get("leo_status") or row.get("status"),
        igm_no=payload.get("igm_no"),
        assessment=payload.get("assessment"),
    )


class AutoLeoService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[AutoLeoRepository] = None) -> None:
        self._repo = repository or AutoLeoRepository(dsn=dsn)

    async def board(self, *, limit: int = 50,
                    source_mode: Optional[str] = None) -> Dict[str, Any]:
        """One row per export container: four-way join + flags + provenance."""
        t0 = perf_counter()
        leo = _leo()
        containers = await self._repo.export_containers(limit=limit, source_mode=source_mode)
        if not containers:
            return {"rows": [], "count": 0, "summary": _summary([]),
                    "flags": _flag_catalogue(leo), "assumption": {
                        "ref": ASSUMPTION_REF, "text": ASSUMPTION_TEXT},
                    "weight_tolerance_pct": 2.0}

        captures = await self._repo.captures_for(containers)
        reroutes = await self._repo.reroutes_for(containers)
        real_docs = {d["container_no"]: d for d in await self._repo.real_form13_documents()
                     if d.get("container_no")}

        # Newest capture per (container, stream). The ORDER BY in the repository
        # puts the newest first, so the first one seen wins.
        joined: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in captures:
            src = CAPTURE_TO_SOURCE.get(row["capture_type"])
            cn = row.get("container_no")
            if not src or not cn:
                continue
            joined.setdefault(cn, {}).setdefault(src, row)

        reroute_by_container: Dict[str, Dict[str, Any]] = {}
        for r in reroutes:
            reroute_by_container.setdefault(r["container_no"], r)

        rows: List[Dict[str, Any]] = []
        for cn in containers:
            streams = joined.get(cn, {})
            dataset_rec = SimpleNamespace(
                container_no=cn,
                vehicle_plate=(streams.get("weighbridge") or {}).get("vehicle_plate"),
                eseal=None, form13=None, weighbridge=None, icegate=None,
            )
            evidence: Dict[str, Any] = {}
            for src, row in streams.items():
                p = _payload(row.get("payload"))
                setattr(dataset_rec, src, _record(src, p, row))
                evidence[src] = {
                    "capture_id": row.get("id"),
                    "captured_at": row.get("captured_at"),
                    "status": row.get("status"),
                    "source_mode": row.get("source_mode"),
                    "vehicle_plate": row.get("vehicle_plate"),
                    "evidence_uri": row.get("evidence_uri"),
                    # A stream with no corpus feed is SIMULATED regardless of how
                    # the row happens to be tagged in the capture store.
                    "provenance": ("SIMULATED" if src in SIMULATED_STREAMS
                                   else ("REAL" if row.get("source_mode") == "live"
                                         else "SIMULATED")),
                    "payload": p,
                }

            result = leo.reconcile(cn, dataset={cn: dataset_rec})
            doc = real_docs.get(cn)
            reroute = reroute_by_container.get(cn)

            rows.append({
                "container_no": cn,
                "vehicle_plate": result.vehicle_plate
                                 or (streams.get("form13") or {}).get("vehicle_plate"),
                "leo_ready": result.leo_ready,
                "customs_flags": result.customs_flags,
                "sources": result.sources,
                "checks": result.checks,
                "evidence": evidence,
                # The Form 13 this row is anchored to, when it is one of the 12
                # REAL documents the customer supplied.
                "form13_document": ({
                    "doc_variant": doc["doc_variant"],
                    "doc_ref": doc["doc_ref"],
                    "vehicle_no": doc["vehicle_no"],
                    "custom_seal_no": doc["seal2"],
                    "line_seal_no": doc["seal1"],
                    "declared_wt_kg": (float(doc["gross_weight_kg"])
                                       if doc["gross_weight_kg"] is not None else None),
                    "data_origin": doc["data_origin"],
                } if doc else None),
                "anchored_to_real_document": bool(doc),
                "weighbridge_reroute": reroute,
            })

        log.info("auto_leo.board", extra={"rows": len(rows),
                                          "ms": round((perf_counter() - t0) * 1000)})
        return {
            "rows": rows,
            "count": len(rows),
            "summary": _summary(rows),
            "flags": _flag_catalogue(leo),
            "weight_tolerance_pct": 2.0,
            "assumption": {"ref": ASSUMPTION_REF, "text": ASSUMPTION_TEXT},
            "join_key": "container_no",
            "streams": ["eseal", "form13", "weighbridge", "icegate"],
        }

    async def container(self, container_no: str) -> Optional[Dict[str, Any]]:
        board = await self.board(limit=1000)
        for row in board["rows"]:
            if row["container_no"] == container_no:
                return {**row, "flags": board["flags"],
                        "assumption": board["assumption"]}
        # Not on the board => no Form 13 => reconcile still answers, honestly.
        leo = _leo()
        result = leo.reconcile(container_no, dataset={})
        return {
            "container_no": container_no,
            "vehicle_plate": None,
            "leo_ready": False,
            "customs_flags": result.customs_flags,
            "sources": result.sources,
            "checks": result.checks,
            "evidence": {},
            "form13_document": None,
            "anchored_to_real_document": False,
            "weighbridge_reroute": None,
            "flags": _flag_catalogue(leo),
            "assumption": {"ref": ASSUMPTION_REF, "text": ASSUMPTION_TEXT},
        }


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_flag: Dict[str, int] = {}
    for r in rows:
        for f in r["customs_flags"]:
            by_flag[f] = by_flag.get(f, 0) + 1
    return {
        "total": len(rows),
        "leo_ready": sum(1 for r in rows if r["leo_ready"]),
        "blocked": sum(1 for r in rows if not r["leo_ready"]),
        "anchored_to_real_document": sum(1 for r in rows if r["anchored_to_real_document"]),
        "by_flag": by_flag,
    }


def _flag_catalogue(leo: Any) -> List[Dict[str, str]]:
    """Every flag the board can raise, with what it means. Rendered as the legend
    so an evaluator reads the vocabulary rather than inferring it from data."""
    return [
        {"flag": leo.FLAG_ID_MISMATCH,
         "meaning": "The captured streams disagree about which container this is."},
        {"flag": leo.FLAG_ESEAL_TAMPER,
         "meaning": "The e-seal reported a tamper condition."},
        {"flag": leo.FLAG_WEIGHT_MISMATCH,
         "meaning": "Weighbridge weight differs from the Form 13 VGM by more than 2%."},
        {"flag": leo.FLAG_WEIGHT_MISSING,
         "meaning": ("No weight to reconcile — the weighbridge failed (X4). The truck "
                     "is rerouted to an alternate weighbridge and customs is notified.")},
        {"flag": leo.FLAG_LEO_MISSING,
         "meaning": "ICEGATE has not granted the Let Export Order."},
        {"flag": leo.FLAG_RECORDS_MISSING,
         "meaning": "A required evidence stream was never captured for this container."},
    ]
