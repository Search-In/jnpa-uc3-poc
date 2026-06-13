"""PyTorch model: GraphSAGE encoder over the corridor graph -> 2-layer LSTM.

Architecture (per the bid spec):

    input  : x  of shape (T, N, F)   — T=window steps, N=segments, F=features
             a static corridor graph (edge_index, edge_attr) shared across T
    encoder: a 2-layer GraphSAGE applied INDEPENDENTLY at each of the T steps,
             producing a (T, N, gnn_out) sequence of spatial embeddings. Edge
             attributes (lane-count, signalised flag) modulate the neighbour
             message so multi-lane / signalised boundaries diffuse congestion
             differently.
    temporal: the per-node embedding sequence is fed (node-wise) into a shared
             2-layer LSTM over the T steps; the last hidden state -> a linear
             head -> one logit per segment.
    output : logits (N,) -> sigmoid = P(congested in next horizon_min minutes).

The GraphSAGE convolution is implemented from scratch (mean neighbour
aggregation with an edge-attribute gate) so the PoC has no hard dependency on
torch-geometric wheels, which are awkward to install CPU-only. If PyG *is*
installed, graph.CorridorGraph.to_pyg() is still available for downstream use;
the maths here matches PyG's mean-aggregator SAGEConv with an added edge gate.
"""
from __future__ import annotations

from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import CongestionConfig


class SAGEConv(nn.Module):
    """Mean-aggregation GraphSAGE layer with an edge-attribute gate.

    For target node *i* with neighbours *j* (including a self-loop), the
    aggregated message is the mean of ``g_ij * W_neigh x_j`` where the scalar
    gate ``g_ij = sigmoid(w · edge_attr_ij)`` lets lane-count / signalised edges
    pass more or less neighbour signal. The root embedding ``W_self x_i`` is
    concatenated, then linearly projected (the classic SAGE "concat" variant).

    Vectorised over a leading "graphs" dimension G (= batch * time): ``x`` is
    ``(G, N, in_dim)`` and the same static ``edge_index`` / ``edge_attr`` apply
    to every graph. Aggregation uses ``index_add_`` over the node axis so the
    whole window is one conv call (no Python loop over time/batch).
    """

    def __init__(self, in_dim: int, out_dim: int, edge_dim: int) -> None:
        super().__init__()
        self.lin_self = nn.Linear(in_dim, out_dim)
        self.lin_neigh = nn.Linear(in_dim, out_dim, bias=False)
        self.edge_gate = nn.Linear(edge_dim, 1)
        self.out = nn.Linear(out_dim * 2, out_dim)

    def forward(
        self,
        x: torch.Tensor,           # (G, N, in_dim)
        edge_index: torch.Tensor,  # (2, E)  rows = [src, dst]
        edge_attr: torch.Tensor,   # (E, edge_dim)
    ) -> torch.Tensor:
        g, n, _ = x.shape
        src, dst = edge_index[0], edge_index[1]
        gate = torch.sigmoid(self.edge_gate(edge_attr)).unsqueeze(0)  # (1, E, 1)
        neigh = self.lin_neigh(x)                                     # (G, N, out)
        messages = neigh.index_select(1, src) * gate                 # (G, E, out)

        agg = x.new_zeros((g, n, messages.size(-1)))
        agg.index_add_(1, dst, messages)                             # sum into targets
        deg = x.new_zeros((1, n, 1))
        deg.index_add_(1, dst, torch.ones((1, dst.size(0), 1), dtype=x.dtype, device=x.device))
        agg = agg / deg.clamp(min=1.0)                               # mean

        h = torch.cat([self.lin_self(x), agg], dim=-1)               # (G, N, 2*out)
        return self.out(h)


class CorridorGraphEncoder(nn.Module):
    """2-layer GraphSAGE producing a per-segment spatial embedding."""

    def __init__(self, cfg: CongestionConfig, edge_dim: int = 2) -> None:
        super().__init__()
        self.conv1 = SAGEConv(cfg.in_features, cfg.gnn_hidden, edge_dim)
        self.conv2 = SAGEConv(cfg.gnn_hidden, cfg.gnn_out, edge_dim)
        self.dropout = nn.Dropout(0.1)

    def forward(self, x, edge_index, edge_attr) -> torch.Tensor:
        # x: (G, N, F) -> (G, N, gnn_out), G = batch*time graphs.
        h = F.relu(self.conv1(x, edge_index, edge_attr))
        h = self.dropout(h)
        h = F.relu(self.conv2(h, edge_index, edge_attr))
        return h


class CongestionForecaster(nn.Module):
    """GraphSAGE encoder + 2-layer LSTM congestion-onset classifier.

    forward(window, edge_index, edge_attr):
        window     : (B, T, N, F)  or  (T, N, F)  (a single sample)
        returns    : logits (B, N)  or  (N,)
    """

    def __init__(self, cfg: CongestionConfig, edge_dim: int = 2) -> None:
        super().__init__()
        self.cfg = cfg
        self.encoder = CorridorGraphEncoder(cfg, edge_dim)
        self.lstm = nn.LSTM(
            input_size=cfg.gnn_out,
            hidden_size=cfg.lstm_hidden,
            num_layers=cfg.lstm_layers,
            batch_first=True,
            dropout=0.1 if cfg.lstm_layers > 1 else 0.0,
        )
        self.head = nn.Linear(cfg.lstm_hidden, 1)

    def forward(
        self,
        window: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor,
    ) -> torch.Tensor:
        squeeze_batch = window.dim() == 3
        if squeeze_batch:
            window = window.unsqueeze(0)            # (1, T, N, F)
        b, t, n, f = window.shape

        # Run the GNN at every (batch, time) step in ONE vectorised conv: fold
        # (B, T) into a single graphs dimension G, all sharing the static edges.
        graphs = window.reshape(b * t, n, f)         # (G, N, F)
        emb = self.encoder(graphs, edge_index, edge_attr)  # (G, N, gnn_out)
        seq = emb.reshape(b, t, n, self.cfg.gnn_out)       # (B, T, N, gnn_out)

        # LSTM is shared across nodes: fold N into the batch dimension.
        seq = seq.permute(0, 2, 1, 3).reshape(b * n, t, self.cfg.gnn_out)
        out, _ = self.lstm(seq)                      # (B*N, T, H)
        last = out[:, -1, :]                         # (B*N, H)
        logits = self.head(last).reshape(b, n)       # (B, N)

        return logits.squeeze(0) if squeeze_batch else logits

    @torch.no_grad()
    def predict_proba(self, window, edge_index, edge_attr) -> torch.Tensor:
        self.eval()
        return torch.sigmoid(self.forward(window, edge_index, edge_attr))


def build_model(cfg: Optional[CongestionConfig] = None, edge_dim: int = 2) -> CongestionForecaster:
    cfg = cfg or CongestionConfig()
    return CongestionForecaster(cfg, edge_dim)


__all__ = [
    "SAGEConv",
    "CorridorGraphEncoder",
    "CongestionForecaster",
    "build_model",
]
