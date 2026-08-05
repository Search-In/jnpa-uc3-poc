"""Shipping-Lines <- marine lifecycle key resolution. Mirrors berthing/lifecycle.py.

Shipping-line records identify a call by ``vessel_visit`` (and sometimes ``voyage``), the
marine spine identifies it by ``via_no``. This module turns the former into VIA candidates
so the shared projection can be asked. It derives NO lifecycle state — the caller reads
progress straight off :class:`~services.marine.projection.CallProjection`.

THE KEY SHAPES, AND WHY THE STRIP IS NOT A GUESS
------------------------------------------------
Terminals emit the visit two ways:

    'S0276'      a plain VIA
    'KMIS0276'   a COMPOSITE: 3-char vessel/line code + the VIA

Doc 01 §1.9 documents the composite form explicitly ("note DP-World composite VIA form
``CGKS0504``/``AGLS0540`` — strip 3-char vessel code"), and doc 01 §2.5 cites the same
shape on the advance lists ("EAL↔IAL same visit KMIS0276"). The strip is therefore a
documented corpus convention, not an invented rule.

It is still applied CONSERVATIVELY: the exact value is always tried first and always wins,
the stripped form is only a fallback, and a match reports WHICH form resolved it so a
composite match is never mistaken for an exact one.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from services.marine.projection import CallProjection

#: A bare VIA: one letter + four digits (S0276, R3029).
_VIA_RE = re.compile(r"^[A-Z]\d{4}$")
#: A composite: exactly three letters then a bare VIA.
_COMPOSITE_RE = re.compile(r"^[A-Z]{3}([A-Z]\d{4})$")

EXACT = "exact"
COMPOSITE = "composite"


def via_candidates(vessel_visit: Any) -> list[str]:
    """VIA forms to try for one vessel_visit, most trustworthy first.

    Returns [] for anything that is neither a VIA nor a composite — a value that cannot be
    a VIA must not be turned into one.
    """
    s = str(vessel_visit or "").strip().upper()
    if not s:
        return []
    out: list[str] = []
    if _VIA_RE.match(s):
        out.append(s)
    m = _COMPOSITE_RE.match(s)
    if m:
        out.append(s)          # exact first: the composite may itself be a stored via_no
        out.append(m.group(1))
    return out


def all_candidates(rows: Iterable[Mapping[str, Any]],
                   field: str = "vessel_visit") -> list[str]:
    """Every VIA candidate across a page, de-duplicated — for one batched lookup."""
    seen: set[str] = set()
    for r in rows:
        seen.update(via_candidates(r.get(field)))
    return sorted(seen)


def resolve(vessel_visit: Any,
            states: Mapping[str, CallProjection]) -> tuple[Optional[CallProjection], Optional[str]]:
    """Best projection for one vessel_visit, plus HOW it matched (exact / composite).

    Exact always wins. ``(None, None)`` when nothing resolved — a real finding (the call
    was never ingested, or the visit code is a shape this corpus has not shown), never an
    error and never a fabricated match.
    """
    cands = via_candidates(vessel_visit)
    for i, c in enumerate(cands):
        hit = states.get(c)
        if hit is not None:
            return hit, (EXACT if i == 0 else COMPOSITE)
    return None, None
