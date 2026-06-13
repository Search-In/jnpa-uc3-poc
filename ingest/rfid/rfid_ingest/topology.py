"""RFID reader topology + tag pool for the JNPA UC-III PoC.

Places 25 logical UHF readers:
  * 10 at the 4 gates (G-NSICT, G-JNPCT, G-NSIGT, G-BMCT) — a couple of lanes
    each. These carry a ``gate_id`` so the correlator can join per gate.
  * 15 along the 40-km NH-348 corridor, dropped onto evenly-spaced segment
    midpoints from ``jnpa_shared.corridor``. These have ``gate_id = None``.

Reader ids are ``R-01`` … ``R-25``. The gate readers come first so a fixed slice
is stable across restarts.

The tag pool is a fixed list of 12,000 UHF-style EPC hex tag ids. Tags are drawn
deterministically (seeded) so the *same* truck (tag) shows up at multiple readers
as it moves down the corridor — which is exactly what the correlator relies on.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional

from jnpa_shared import corridor

# The 4 JNPA gates (ids mirror jnpa.gates in infra/postgres/init.sql).
GATE_IDS: List[str] = ["G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT"]

# Gate coordinates (mirror the seed rows so emitted positions are realistic).
GATE_COORDS = {
    "G-NSICT": (18.9489, 72.9492),
    "G-JNPCT": (18.9512, 72.9505),
    "G-NSIGT": (18.9457, 72.9531),
    "G-BMCT": (18.9420, 72.9560),
}

# UHF EPC tags look like Gen2 96-bit EPCs rendered as 24 hex chars. JNPA FASTag /
# UHF windscreen tags commonly start with the "E280 1160" prefix (the example in
# the spec). We keep that prefix and randomise the remaining 16 hex nibbles.
EPC_PREFIX = "E2801160"
EPC_BODY_NIBBLES = 16  # -> 24-char total EPC


@dataclass(frozen=True)
class Reader:
    """A logical RFID reader and where it sits."""

    id: str
    kind: str                 # "gate" | "corridor"
    lat: float
    lon: float
    gate_id: Optional[str]    # set for gate readers, None along the corridor
    segment_id: Optional[str] # set for corridor readers, None at gates


def _gate_readers(n: int) -> List[Reader]:
    """Distribute ``n`` readers round-robin across the 4 gates (lanes)."""
    readers: List[Reader] = []
    for i in range(n):
        gate = GATE_IDS[i % len(GATE_IDS)]
        lat, lon = GATE_COORDS[gate]
        # Nudge each lane slightly so co-located readers are not identical points.
        lane = i // len(GATE_IDS)
        readers.append(
            Reader(
                id=f"R-{i + 1:02d}",
                kind="gate",
                lat=round(lat + lane * 0.0001, 6),
                lon=round(lon + lane * 0.0001, 6),
                gate_id=gate,
                segment_id=None,
            )
        )
    return readers


def _corridor_readers(n: int, start_idx: int) -> List[Reader]:
    """Place ``n`` readers on evenly-spaced corridor segment midpoints."""
    segs = corridor.segments
    readers: List[Reader] = []
    if not segs:
        return readers
    for j in range(n):
        # Spread picks across the available segments (last reader hits the end).
        frac = j / max(1, n - 1)
        seg = segs[min(len(segs) - 1, round(frac * (len(segs) - 1)))]
        lat, lon = seg.midpoint
        readers.append(
            Reader(
                id=f"R-{start_idx + j + 1:02d}",
                kind="corridor",
                lat=round(lat, 6),
                lon=round(lon, 6),
                gate_id=None,
                segment_id=seg.id,
            )
        )
    return readers


def build_readers(num_gate: int = 10, num_corridor: int = 15) -> List[Reader]:
    """Build the full reader fleet (gate readers first, then corridor)."""
    gate = _gate_readers(num_gate)
    cor = _corridor_readers(num_corridor, start_idx=num_gate)
    return gate + cor


def build_tag_pool(size: int = 12000, seed: int = 42) -> List[str]:
    """Deterministically generate a fixed pool of UHF EPC tag ids.

    Same ``size``/``seed`` -> same ordered pool across every process, so the
    emulator, tests, and any replay see identical truck identities.
    """
    rng = random.Random(seed)
    pool: List[str] = []
    seen: set[str] = set()
    while len(pool) < size:
        body = "".join(rng.choice("0123456789ABCDEF") for _ in range(EPC_BODY_NIBBLES))
        epc = EPC_PREFIX + body
        if epc in seen:  # vanishingly rare, but keep the pool unique
            continue
        seen.add(epc)
        pool.append(epc)
    return pool


# Map reader_id -> gate_id for the readers that sit at a gate (used by the
# correlator to scope the rfid<->anpr join per gate).
def reader_gate_map(readers: List[Reader]) -> dict[str, str]:
    return {r.id: r.gate_id for r in readers if r.gate_id is not None}
