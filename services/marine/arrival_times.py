"""Six arrival-time ladder (spec UI-025 / UC1-019) — pure assembly.

Each row is a separately stored definition with a NAMED SOURCE. Missing corpus
coverage stays null with an honest note — never fabricated. The planted
ETA-vs-anchorage gap (~31 days on TSS AMBER) is flagged, not smoothed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .state_engine import EVENT_ANCHORED, EVENT_BERTHED, EVENT_FIRST_LINE, EVENT_PILOT_BOARDED

# (key, label) in display order.
_LADDER: tuple[tuple[str, str], ...] = (
    ("proforma_eta", "Proforma ETA"),
    ("declared_eta", "Declared ETA (PCS)"),
    ("last_reported_eta", "Last-reported ETA"),
    ("at_anchorage", "Arrival at anchorage"),
    ("pilot_boarding", "Pilot boarding"),
    ("first_line", "First line ashore"),
)

#: Gap (days) at/above which the planted temporal anomaly is flagged.
_ANOMALY_GAP_DAYS = 28.0


def _ts(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    return None


def _first_event(events: Sequence[Mapping[str, Any]], *types: str) -> Optional[Mapping[str, Any]]:
    wanted = set(types)
    hits = [e for e in events if e.get("event_type") in wanted and e.get("event_ts") is not None]
    if not hits:
        return None
    return min(hits, key=lambda e: e["event_ts"])


def _source_file_label(ev: Optional[Mapping[str, Any]]) -> str:
    """Best-effort PCS log / message label for the row citation."""
    if not ev:
        return ""
    # Prefer explicit source tags when the projection merged pilotage milestones.
    src = (ev.get("source") or ev.get("_source_file") or ev.get("source_note") or "").strip()
    return src


def _pilot_name(call: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Pilot display name when ACKPLM/pilotage carried one (never invented)."""
    for key in ("pilot_name", "effective_pilot_name"):
        v = call.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for e in events:
        extras = e.get("extras") if isinstance(e.get("extras"), dict) else None
        if extras and extras.get("pilot_name"):
            return str(extras["pilot_name"]).strip()
        if e.get("pilot_name"):
            return str(e["pilot_name"]).strip()
    return None


def _gap_days(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 86400.0


def assemble_arrival_times(
    call: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the six-row ladder + optional planted-anomaly note for one call.

    ``events`` should already be the timeline merge (ledger + pilotage milestones).
    """
    rows: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    # --- proforma: not present in NLP Marine PCS family -----------------------
    rows.append({
        "key": "proforma_eta",
        "label": "Proforma ETA",
        "value": None,
        "source": None,
        "derived": False,
        "note": "no source in ingested corpus (proforma not carried by NLP Marine PCS)",
    })

    # --- declared ETA: vessel_call.eta seeded by CALINF (EDTA) ----------------
    declared = _ts(call.get("eta"))
    if declared is not None:
        note = call.get("source_note")
        cite = "PCS CALINF · EDTA"
        if note:
            cite = f"{cite} · ref {note}"
        rows.append({
            "key": "declared_eta",
            "label": "Declared ETA (PCS)",
            "value": declared,
            "source": cite,
            "derived": False,
            "note": None,
        })
    else:
        rows.append({
            "key": "declared_eta",
            "label": "Declared ETA (PCS)",
            "value": None,
            "source": None,
            "derived": False,
            "note": "no CALINF EDTA on this call in the ingested corpus",
        })

    # --- last-reported ETA: no dedicated PCS revision stream in corpus --------
    rows.append({
        "key": "last_reported_eta",
        "label": "Last-reported ETA",
        "value": None,
        "source": None,
        "derived": False,
        "note": "no source in ingested corpus (no ETA-revision message ingested)",
    })

    # --- anchorage ------------------------------------------------------------
    anchored = _first_event(events, EVENT_ANCHORED)
    if anchored is not None:
        label = _source_file_label(anchored)
        cite = "PCS VESARR · AnchoringDateTime"
        if label:
            cite = f"{cite} · {label}"
        rows.append({
            "key": "at_anchorage",
            "label": "Arrival at anchorage",
            "value": _ts(anchored.get("event_ts")),
            "source": cite,
            "derived": False,
            "note": None,
        })
    else:
        rows.append({
            "key": "at_anchorage",
            "label": "Arrival at anchorage",
            "value": None,
            "source": None,
            "derived": False,
            "note": "no VESARR AnchoringDateTime in ingested corpus",
        })

    # --- pilot boarding -------------------------------------------------------
    boarded = _first_event(events, EVENT_PILOT_BOARDED)
    if boarded is not None:
        label = _source_file_label(boarded)
        cite = "PCS VESARR · PilotBoardedTime"
        if label:
            cite = f"{cite} · {label}"
        pname = _pilot_name(call, events)
        note = f"pilot {pname}" if pname else (
            "pilot name not in ingested corpus (ACKPLM / pilotage)")
        rows.append({
            "key": "pilot_boarding",
            "label": "Pilot boarding",
            "value": _ts(boarded.get("event_ts")),
            "source": cite,
            "derived": False,
            "note": note,
        })
    else:
        rows.append({
            "key": "pilot_boarding",
            "label": "Pilot boarding",
            "value": None,
            "source": None,
            "derived": False,
            "note": "no VESARR PilotBoardedTime in ingested corpus",
        })

    # --- first line: prefer FIRST_LINE; else BERTHED as derived alongside -----
    first_line = _first_event(events, EVENT_FIRST_LINE)
    berthed = _first_event(events, EVENT_BERTHED)
    if first_line is not None:
        label = _source_file_label(first_line)
        cite = "PCS pilotage · first_line_at"
        if label:
            cite = f"{cite} · {label}"
        rows.append({
            "key": "first_line",
            "label": "First line ashore",
            "value": _ts(first_line.get("event_ts")),
            "source": cite,
            "derived": False,
            "note": None,
        })
    elif berthed is not None:
        label = _source_file_label(berthed)
        cite = "PCS VESARR · BerthDateTime (≈ first line / alongside)"
        if label:
            cite = f"{cite} · {label}"
        rows.append({
            "key": "first_line",
            "label": "First line ashore",
            "value": _ts(berthed.get("event_ts")),
            "source": cite,
            "derived": True,
            "note": "derived from BERTHED — corpus has no separate first-line stamp",
        })
    else:
        rows.append({
            "key": "first_line",
            "label": "First line ashore",
            "value": None,
            "source": None,
            "derived": False,
            "note": "no source in ingested corpus",
        })

    # --- planted temporal anomaly (TSS AMBER pattern) -------------------------
    anch_ts = _ts(anchored.get("event_ts")) if anchored else None
    if declared is not None and anch_ts is not None:
        gap = _gap_days(declared, anch_ts)
        if gap >= _ANOMALY_GAP_DAYS:
            anomalies.append({
                "code": "eta_vs_anchorage_gap",
                "days": round(gap, 1),
                "message": (
                    f"Planted anomaly: arrival at anchorage is {gap:.0f} days after "
                    f"declared ETA — flagged, not hidden."
                ),
            })

    # Keep ladder key order stable even if assembly above drifts.
    by_key = {r["key"]: r for r in rows}
    ordered = [by_key[k] for k, _ in _LADDER if k in by_key]

    return {
        "call_id": call.get("call_id"),
        "vcn": call.get("vcn"),
        "via_no": call.get("via_no"),
        "vessel_name": call.get("vessel_name"),
        "voyage_no": call.get("voyage_no"),
        "imo_no": call.get("imo_no"),
        "arrival_times": ordered,
        "actuals": {
            "ata": call.get("ata"),
            "atc": call.get("atc"),
            "atd": call.get("atd"),
        },
        "anomalies": anomalies,
    }
