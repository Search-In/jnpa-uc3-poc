"""Universal trip resolver (UC3-024) and per-visit checkpoint timeline (UC3-025).

UC3-024 — one search box, four key kinds, one trip
--------------------------------------------------
A plate, a container number, an e-seal id and a Form 13 e-gate number must all
resolve to the SAME trip record. They can, because all four are printed on one
gate document, and that document IS the trip record — so resolving is a lookup,
not a reconciliation.

Two rules keep it honest:

  * **Never guess.** When a key matches more than one visit (a tractor makes many
    trips), the resolver returns EVERY match with ``ambiguous: true`` and picks
    none. Silently choosing the newest would be indistinguishable from a correct
    answer, and wrong.
  * **Say how sure you are.** ``match_confidence`` is 1.0 only for a key that is
    unique across the corpus AND matched exactly one visit. A plate that names
    several visits scores lower, and the score is reported next to the result
    rather than kept internal.

UC3-025 — the checkpoint timeline
---------------------------------
Ten checkpoints, each carrying an EVIDENCE LABEL rather than a fabricated time:

    VERIFIED       a real corpus timestamp backs this step
    KEY_ONLY       the document evidences that the step happened (it prints the
                   weight, the BAT number, the yard slot) but prints no time
    NOT_IN_CORPUS  the corpus has no source for this step at all

No container crosses all ten steps in the corpus — gaps G6/G9 mean the enroute
and plaza checkpoints have no events — so a timeline without per-step labels
would have to invent times to look complete. The labels are the demo point: the
gap is disclosed, not filled.
"""
from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

from .repository import SEARCH_COLUMNS, TripSearchRepository, norm, parse_attrs

log = get_logger("services.trip_search.service")

# --- evidence labels ---------------------------------------------------------
VERIFIED = "VERIFIED"
KEY_ONLY = "KEY_ONLY"
NOT_IN_CORPUS = "NOT_IN_CORPUS"

EVIDENCE_MEANING = {
    VERIFIED: "A real corpus timestamp backs this step.",
    KEY_ONLY: ("The document evidences that this step happened but prints no "
               "time, so no time is shown."),
    NOT_IN_CORPUS: ("The corpus has no source for this step. It is a declared gap "
                    "(G6/G9), pending FASTag/GPS integration post-award."),
}

#: The ten checkpoints of a truck visit, in order (UI-107).
CHECKPOINTS: List[Dict[str, str]] = [
    {"key": "documents_ready", "label": "Documents ready"},
    {"key": "corridor_entry", "label": "Corridor entry"},
    {"key": "plaza_entry", "label": "Plaza entry"},
    {"key": "plaza_release", "label": "Plaza release"},
    {"key": "gate_queue_join", "label": "Gate queue join"},
    {"key": "recognition_portal", "label": "Recognition portal (ANPR arch)"},
    {"key": "weighbridge", "label": "Weighbridge"},
    {"key": "security_documentation", "label": "Security and documentation"},
    {"key": "yard_service", "label": "Yard service"},
    {"key": "gate_out", "label": "Gate out"},
]

#: Key kinds whose values are unique per document across the corpus. Matching one
#: of these exactly is the strongest evidence a resolver can have.
UNIQUE_KEY_KINDS = {"DOCUMENT_NO", "CONTAINER", "ESEAL", "PIN"}


def detect_key_kind(q: str) -> str:
    """Best guess at what the operator typed. Advisory only — the resolver
    searches every column regardless, so a wrong guess costs nothing."""
    n = norm(q)
    if not n:
        return "UNKNOWN"
    if len(n) == 11 and n[:4].isalpha() and n[4:].isdigit():
        return "CONTAINER"          # ISO 6346: 4 letters + 7 digits
    if n.isdigit():
        return "DOCUMENT_NO"        # e-gate / EIR / seal numbers are numeric
    if n[:2].isalpha() and any(c.isdigit() for c in n):
        return "PLATE"
    return "UNKNOWN"


def _matched_columns(row: Dict[str, Any], q: str) -> List[Dict[str, str]]:
    """Which of the document's key columns the query actually matched."""
    needle = norm(q)
    out = []
    for col, kind in SEARCH_COLUMNS.items():
        val = row.get(col)
        if val and norm(str(val)) == needle:
            out.append({"column": col, "kind": kind, "value": str(val)})
    return out


def _confidence(matches: List[Dict[str, Any]], matched_cols: List[Dict[str, str]]) -> float:
    """How sure the resolver is that this is the trip the operator meant.

    1.00  a corpus-unique key (document no / container / e-seal / PIN) matched
          exactly one visit — there is nothing else it could be.
    0.60  a plate matched exactly one visit. Lower because a plate identifies a
          TRACTOR, not a trip: one visit today is chance, not uniqueness.
    0.40  the key matched several visits. Reported per candidate; the resolver
          selects none of them.
    """
    if len(matches) > 1:
        return 0.4
    if any(m["kind"] in UNIQUE_KEY_KINDS for m in matched_cols):
        return 1.0
    return 0.6


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (v if v is None else str(v))


def _trip(row: Dict[str, Any], q: str, all_matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    attrs = parse_attrs(row.get("attrs"))
    matched_cols = _matched_columns(row, q)
    return {
        "trip_id": f"GD-{row['doc_id']}",
        "doc_id": row["doc_id"],
        "doc_category": row["doc_category"],
        "doc_variant": row["doc_variant"],
        "document_no": row["doc_ref"],
        "pin_no": row["pin_no"],
        "container_no": row["container_no"],
        "vehicle_no": row["vehicle_no"],
        "line_seal_no": row["seal1"],
        "custom_seal_no": row["seal2"],
        "bat_no": row["bat_no"],
        "terminal_code": row.get("terminal_code"),
        "terminal_name": row.get("terminal_name"),
        "terminal_operator": row.get("terminal_operator"),
        "transporter_name": row.get("transporter_name") or attrs.get("Transporter"),
        "driver_name": row.get("driver_name"),
        "driver_licence": row.get("driver_licence"),
        "vessel_name": row.get("vessel_name") or attrs.get("VesselName"),
        "voyage": row.get("voyage") or attrs.get("Voyage"),
        "pol": row.get("pol"),
        "pod": row.get("pod") or attrs.get("PortOfDischarge"),
        "booking_no": row.get("booking_no"),
        "cfs": row.get("cfs"),
        "iso_code": row.get("iso_code"),
        "gross_weight_kg": (float(row["gross_weight_kg"])
                            if row.get("gross_weight_kg") is not None else None),
        "yard_position": row.get("yard_position"),
        "gate_no": row.get("gate_no"),
        "doc_ts": _iso(row.get("doc_ts")),
        "truck_in_ts": _iso(row.get("truck_in_ts")),
        "truck_out_ts": _iso(row.get("truck_out_ts")),
        "image_file": row.get("image_file"),
        "source_file": row.get("source_file"),
        "data_origin": row.get("data_origin"),
        "attrs": attrs,
        # Resolution metadata — how this trip was reached and how sure we are.
        "matched_by": matched_cols,
        "match_confidence": _confidence(all_matches, matched_cols),
    }


class TripSearchService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[TripSearchRepository] = None) -> None:
        self._repo = repository or TripSearchRepository(dsn=dsn)

    # --------------------------------------------------------------- UC3-024
    async def resolve(self, q: str) -> Dict[str, Any]:
        """Resolve any supported key to a trip. Never guesses between candidates."""
        t0 = perf_counter()
        query = (q or "").strip()
        if not query:
            return {"query": q, "status": "INVALID_INPUT", "trips": [], "count": 0,
                    "reason": "empty query", "detected_kind": "UNKNOWN",
                    "searchable_keys": _searchable_keys()}
        if len(norm(query)) < 3:
            return {"query": query, "status": "INVALID_INPUT", "trips": [], "count": 0,
                    "reason": "a key needs at least 3 alphanumeric characters",
                    "detected_kind": detect_key_kind(query),
                    "searchable_keys": _searchable_keys()}

        rows = await self._repo.find_by_key(query)
        if not rows:
            suggestions = await self._repo.find_by_prefix(query)
            return {
                "query": query,
                "status": "NO_MATCH",
                "trips": [],
                "count": 0,
                "detected_kind": detect_key_kind(query),
                "reason": ("No gate document carries this key. Nothing is inferred: "
                           "a key with no document has no trip."),
                "suggestions": [
                    {"trip_id": f"GD-{r['doc_id']}", "document_no": r["doc_ref"],
                     "container_no": r["container_no"], "vehicle_no": r["vehicle_no"],
                     "terminal_code": r.get("terminal_code")}
                    for r in suggestions
                ],
                "searchable_keys": _searchable_keys(),
            }

        trips = [_trip(r, query, rows) for r in rows]
        ambiguous = len(trips) > 1
        log.info("trip_search.resolve", extra={"q": query, "matches": len(trips),
                                               "ms": round((perf_counter() - t0) * 1000)})
        return {
            "query": query,
            "status": "AMBIGUOUS" if ambiguous else "RESOLVED",
            "ambiguous": ambiguous,
            "trips": trips,
            "count": len(trips),
            "detected_kind": detect_key_kind(query),
            "resolved_trip_id": None if ambiguous else trips[0]["trip_id"],
            "reason": (f"{len(trips)} visits carry this key — choose one. The resolver "
                       "does not pick between candidates."
                       if ambiguous else None),
            "searchable_keys": _searchable_keys(),
        }

    # --------------------------------------------------------------- UC3-025
    async def trip(self, trip_id: str) -> Optional[Dict[str, Any]]:
        """One trip: identity, documents, checkpoint timeline, share link."""
        doc_id = _doc_id(trip_id)
        if doc_id is None:
            return None
        row = await self._repo.by_doc_id(doc_id)
        if row is None:
            return None

        trip = _trip(row, str(row.get("doc_ref") or ""), [row])
        related = await self._repo.related_documents(
            container_no=row.get("container_no"), vehicle_no=row.get("vehicle_no"))
        timeline = build_timeline(row)
        return {
            **trip,
            "timeline": timeline["steps"],
            "timeline_summary": timeline["summary"],
            "evidence_labels": EVIDENCE_MEANING,
            "documents": [
                {"trip_id": f"GD-{r['doc_id']}", "doc_id": r["doc_id"],
                 "doc_category": r["doc_category"], "doc_variant": r["doc_variant"],
                 "document_no": r["doc_ref"], "pin_no": r["pin_no"],
                 "container_no": r["container_no"], "vehicle_no": r["vehicle_no"],
                 "terminal_code": r.get("terminal_code"),
                 "doc_ts": _iso(r.get("doc_ts")),
                 "image_file": r.get("image_file"),
                 "data_origin": r.get("data_origin")}
                for r in related
            ],
            "share_path": f"/truck-visit?trip={trip['trip_id']}",
        }


def _searchable_keys() -> List[Dict[str, str]]:
    return [
        {"kind": "PLATE", "label": "Truck plate", "example": "MH43CK1959"},
        {"kind": "CONTAINER", "label": "Container number", "example": "MEDU1777575"},
        {"kind": "ESEAL", "label": "e-seal / customs seal", "example": "5826371"},
        {"kind": "DOCUMENT_NO", "label": "Form 13 e-gate no / EIR no", "example": "16497850"},
        {"kind": "PIN", "label": "PIN pickup code", "example": "230283"},
    ]


def _doc_id(trip_id: str) -> Optional[int]:
    raw = (trip_id or "").strip().upper()
    if raw.startswith("GD-"):
        raw = raw[3:]
    try:
        return int(raw)
    except ValueError:
        return None


def build_timeline(row: Dict[str, Any]) -> Dict[str, Any]:
    """The ten checkpoints for one visit, each with its evidence label.

    Pure — takes a document row, returns the timeline. No I/O, so the labelling
    rules are unit-testable without a database.

    A step is VERIFIED only when the document prints a TIME for it. A step the
    document evidences without a time is KEY_ONLY and shows the evidence instead
    of a time. Everything else is NOT_IN_CORPUS and shows neither.
    """
    doc_ts = row.get("doc_ts")
    truck_in = row.get("truck_in_ts")
    truck_out = row.get("truck_out_ts")
    attrs = parse_attrs(row.get("attrs"))

    def step(key: str, label: str, ts: Any, evidence: str,
             source: Optional[str], detail: Optional[str] = None) -> Dict[str, Any]:
        return {"key": key, "label": label, "ts": _iso(ts), "evidence": evidence,
                "source": source, "detail": detail, "dwell_minutes": None}

    steps: List[Dict[str, Any]] = [
        step("documents_ready", "Documents ready", doc_ts,
             VERIFIED if doc_ts else NOT_IN_CORPUS,
             f"core.gate_document.doc_ts ({row.get('doc_variant')})" if doc_ts else None,
             "Timestamp printed on the gate document." if doc_ts else None),
        # Gaps G6/G9: the corpus documents gate events only. No enroute or plaza
        # event exists for any container, so these four carry no time at all.
        step("corridor_entry", "Corridor entry", None, NOT_IN_CORPUS, None,
             "No corridor entry event in the corpus (gap G9)."),
        step("plaza_entry", "Plaza entry", None, NOT_IN_CORPUS, None,
             "No CPP occupancy feed in the corpus (gap G9)."),
        step("plaza_release", "Plaza release", None, NOT_IN_CORPUS, None,
             "No CPP release event in the corpus (gap G9)."),
        step("gate_queue_join", "Gate queue join", None, NOT_IN_CORPUS, None,
             "Queue join is not timestamped per vehicle in the corpus."),
        step("recognition_portal", "Recognition portal (ANPR arch)", truck_in,
             VERIFIED if truck_in else NOT_IN_CORPUS,
             "core.gate_document.truck_in_ts" if truck_in else None,
             ("Truck-in time printed on the slip — the read at the ANPR arch."
              if truck_in else "The slip prints no truck-in time.")),
    ]

    # Weighbridge: the slip prints a weight but never a weighing time.
    weight = row.get("gross_weight_kg")
    steps.append(step(
        "weighbridge", "Weighbridge", None,
        KEY_ONLY if weight is not None else NOT_IN_CORPUS,
        "core.gate_document.gross_weight_kg" if weight is not None else None,
        (f"Gross weight {float(weight):.0f} kg is printed; the slip prints no "
         "weighing time." if weight is not None else "No weight on the slip.")))

    # Security & documentation: the BAT number is the gate transaction id issued
    # at the documentation desk, so it evidences the step without timing it.
    bat = row.get("bat_no")
    steps.append(step(
        "security_documentation", "Security and documentation", None,
        KEY_ONLY if bat else NOT_IN_CORPUS,
        "core.gate_document.bat_no" if bat else None,
        (f"BAT {bat} was issued at the documentation desk; no time is printed."
         if bat else "No BAT number on the slip.")))

    # Yard service: a yard slot evidences the move without timing it.
    yard = row.get("yard_position") or attrs.get("YardPosition")
    steps.append(step(
        "yard_service", "Yard service", None,
        KEY_ONLY if yard else NOT_IN_CORPUS,
        "core.gate_document.yard_position" if yard else None,
        (f"Yard position {yard} is printed; no service time." if yard
         else "No yard position on the slip.")))

    steps.append(step(
        "gate_out", "Gate out", truck_out,
        VERIFIED if truck_out else NOT_IN_CORPUS,
        "core.gate_document.truck_out_ts" if truck_out else None,
        ("Truck-out time printed on the slip." if truck_out
         else "The slip prints no truck-out time.")))

    # Dwell between CONSECUTIVE TIMED steps only. Dwell across a NOT_IN_CORPUS
    # gap would be a made-up duration, so it is left null.
    prev_ts, prev_idx = None, None
    for i, s in enumerate(steps):
        if s["ts"] is None:
            continue
        cur = datetime.fromisoformat(s["ts"])
        if prev_ts is not None and prev_idx == i - 1:
            s["dwell_minutes"] = round((cur - prev_ts).total_seconds() / 60.0, 1)
        prev_ts, prev_idx = cur, i

    counts = {VERIFIED: 0, KEY_ONLY: 0, NOT_IN_CORPUS: 0}
    for s in steps:
        counts[s["evidence"]] = counts.get(s["evidence"], 0) + 1

    total_minutes = None
    if truck_in and truck_out:
        total_minutes = round((truck_out - truck_in).total_seconds() / 60.0, 1)

    return {
        "steps": steps,
        "summary": {
            "total_steps": len(steps),
            "verified": counts[VERIFIED],
            "key_only": counts[KEY_ONLY],
            "not_in_corpus": counts[NOT_IN_CORPUS],
            "in_gate_minutes": total_minutes,
            "note": ("No container crosses all ten checkpoints in the corpus. Steps "
                     "without a source are labelled, never back-filled with a "
                     "plausible time."),
        },
    }
