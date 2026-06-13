"""Builds the corridor graph from ``jnpa_shared.corridor``.

Nodes are corridor **segments** (``SEG-00`` … from the shared geometry). Edges
connect physically adjacent segments (segment *i* ↔ *i+1*) — an undirected
adjacency so a GraphSAGE node can pool from both its upstream and downstream
neighbour (congestion propagates both ways: a jam spills back, and a clearing
front moves forward). Each edge carries two static attributes used by the
encoder's message function:

  * ``lane_count``  — multi-lane segments diffuse congestion differently;
  * ``signalised``  — a junction at the boundary (Y-junction / Karal Phata)
    meters flow and is where onset most often starts.

The lane/signal attributes are derived from the corridor geometry (the gate
approach and the Karal Phata junction are signalised; lane count tapers from the
4-lane port approach to a 2-lane single carriageway near the junction). They are
deterministic so the graph is identical across train.py and infer.py.

``build_corridor_graph()`` returns a :class:`CorridorGraph` holding the static
tensors. If PyTorch-Geometric is importable, :meth:`CorridorGraph.to_pyg`
returns a ``torch_geometric.data.Data``; otherwise the raw ``edge_index`` /
``edge_attr`` tensors drive the from-scratch GraphSAGE in ``model.py`` (the PoC
runs CPU-only and must not hard-depend on PyG wheels being available).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from jnpa_shared import corridor

# Edge attribute layout (kept in sync with model.SAGEConv expectations).
EDGE_ATTR_NAMES: Tuple[str, ...] = ("lane_count_norm", "signalised")

# Junction waypoints that are signal-controlled / metered. Segments whose start
# or end sits near these get signalised=1.
_SIGNALISED_POINTS: Tuple[Tuple[float, float], ...] = (
    (18.9489, 72.9492),  # JNPA Gate-1 approach
    (18.9215, 72.9705),  # Y-junction toward NH-348
    (18.7800, 73.0800),  # Karal Phata junction
)
_SIGNAL_RADIUS_KM = 1.2


def _lane_count_for(seg_index: int, n: int) -> int:
    """Lanes per direction, tapering from the 4-lane port approach (north end)
    to a 2-lane carriageway near the Karal Phata junction (south end)."""
    frac = seg_index / max(1, n - 1)
    if frac < 0.25:
        return 4
    if frac < 0.6:
        return 3
    return 2


def _is_signalised(seg) -> bool:
    for pt in _SIGNALISED_POINTS:
        if (
            corridor.haversine_km(seg.start, pt) <= _SIGNAL_RADIUS_KM
            or corridor.haversine_km(seg.end, pt) <= _SIGNAL_RADIUS_KM
        ):
            return True
    return False


@dataclass(frozen=True)
class SegmentMeta:
    """Per-node static metadata exposed to features + the API."""

    id: str
    index: int
    length_km: float
    lane_count: int
    signalised: bool
    lat: float
    lon: float


@dataclass
class CorridorGraph:
    """Static corridor graph: node order, adjacency, and edge attributes.

    ``edge_index`` is a ``(2, E)`` long tensor (COO, both directions for each
    physical adjacency, plus a self-loop per node so a degree-0 graph still
    propagates). ``edge_attr`` is ``(E, len(EDGE_ATTR_NAMES))``.
    """

    segment_ids: List[str]
    meta: List[SegmentMeta]
    edge_index: "object"   # torch.LongTensor (2, E)
    edge_attr: "object"    # torch.FloatTensor (E, 2)
    max_lanes: int

    @property
    def num_nodes(self) -> int:
        return len(self.segment_ids)

    def index_of(self, segment_id: str) -> Optional[int]:
        try:
            return self.segment_ids.index(segment_id)
        except ValueError:
            return None

    def meta_map(self) -> Dict[str, SegmentMeta]:
        return {m.id: m for m in self.meta}

    def to_pyg(self):
        """Return a ``torch_geometric.data.Data`` if PyG is installed, else None."""
        try:
            from torch_geometric.data import Data  # type: ignore
        except Exception:  # noqa: BLE001 - PyG optional
            return None
        return Data(edge_index=self.edge_index, edge_attr=self.edge_attr, num_nodes=self.num_nodes)


def build_corridor_graph() -> CorridorGraph:
    """Build the corridor graph from the shared segment geometry."""
    import torch

    segs = list(corridor.segments)
    n = len(segs)
    if n == 0:
        raise RuntimeError("jnpa_shared.corridor produced no segments")

    meta: List[SegmentMeta] = []
    for i, s in enumerate(segs):
        lanes = _lane_count_for(i, n)
        mid = s.midpoint
        meta.append(
            SegmentMeta(
                id=s.id,
                index=i,
                length_km=s.length_km,
                lane_count=lanes,
                signalised=_is_signalised(s),
                lat=round(mid[0], 6),
                lon=round(mid[1], 6),
            )
        )

    max_lanes = max(m.lane_count for m in meta)

    src: List[int] = []
    dst: List[int] = []
    attrs: List[List[float]] = []

    def _add_edge(a: int, b: int) -> None:
        # Edge attribute reflects the destination node's road character (lanes +
        # whether the boundary it feeds is signalised).
        lane_norm = meta[b].lane_count / max_lanes
        signal = 1.0 if (meta[a].signalised or meta[b].signalised) else 0.0
        src.append(a)
        dst.append(b)
        attrs.append([lane_norm, signal])

    for i in range(n):
        # self-loop (keeps isolated nodes well-defined in the conv)
        _add_edge(i, i)
        if i + 1 < n:
            _add_edge(i, i + 1)   # downstream
            _add_edge(i + 1, i)   # upstream

    edge_index = torch.tensor([src, dst], dtype=torch.long)
    edge_attr = torch.tensor(attrs, dtype=torch.float32)

    return CorridorGraph(
        segment_ids=[m.id for m in meta],
        meta=meta,
        edge_index=edge_index,
        edge_attr=edge_attr,
        max_lanes=max_lanes,
    )


__all__ = ["CorridorGraph", "SegmentMeta", "build_corridor_graph", "EDGE_ATTR_NAMES"]
