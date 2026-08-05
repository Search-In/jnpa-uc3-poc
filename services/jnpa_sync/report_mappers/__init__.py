"""Tolerant mappers for the JNPA report-group JSON items (land-raw-then-map).

The report-group item schema is UNDOCUMENTED (spec defect D10) — the captured
report envelope is a 5-field JSON blob whose ``items`` are raw dicts of no
promised shape. Phase 3 therefore lands the raw snapshot FIRST (``report_ingest``
persists the whole answer into ``core.api_report_snapshot``) and only THEN tries
to map it onto a validated pipeline. The mappers here are the "then map" half:

  * they are PURE (no DB, no HTTP, no exceptions) — a mapper is handed a list of
    raw item dicts and returns a :class:`MapOutcome`;
  * they are ALIAS-DICTIONARY based — the same normalise-header-then-match idea
    the dump-upload parsers use — so ``berth`` / ``berthNo`` / ``berth_no`` all
    resolve to one field and an upstream rename does not silently drop data;
  * they DEGRADE, never raise: an unrecognised item shape (fewer than two known
    keys) comes back as ``RAW_ONLY`` (the raw snapshot is still the evidence),
    junk comes back as ``RAW_ONLY``/``MAP_FAILED`` — never a traceback that would
    fail a sync run.

The sim's synthetic report keys (``seed.synthetic_report_items``) are among the
aliases on purpose, so the MAPPED path is exercised end-to-end offline.

``render_berthing_csv`` / ``render_daily_csv`` turn mapped rows into the exact
CSV template the existing upload services accept, so mapped data lands through
the SAME validated ledger+dedup pipeline the manual dump upload uses — no new
SQL, no new core tables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class MapOutcome:
    """Outcome of mapping one report answer's items.

    status        MAPPED     — rows recognised and keyable into a pipeline
                  RAW_ONLY   — unrecognised/insufficient shape; keep the raw
                               snapshot as the evidence, map nothing
                  MAP_FAILED — a genuine fault while mapping (defensive; the
                               mappers themselves never raise)
    rows          canonical, pipeline-ready dicts (empty unless MAPPED)
    unmapped_keys normalised item keys no alias consumed (drift signal)
    notes         human-readable one-liner for the snapshot's mapped_detail
    """

    status: str
    rows: List[Dict] = field(default_factory=list)
    unmapped_keys: List[str] = field(default_factory=list)
    notes: str = ""


from .berthing import map_berthing_items, render_berthing_csv  # noqa: E402
from .daily import map_daily_items, render_daily_csv  # noqa: E402

__all__ = [
    "MapOutcome",
    "map_berthing_items",
    "map_daily_items",
    "render_berthing_csv",
    "render_daily_csv",
]
