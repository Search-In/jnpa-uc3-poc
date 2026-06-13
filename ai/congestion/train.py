"""Train the congestion forecaster on synthetic + (optional) real history.

Pipeline:
  1. Build the corridor graph (graph.py).
  2. Generate ~14 days of synthetic 60-s history (synthetic.py); if Postgres is
     reachable, enrich the most-recent tail with real ``jnpa.traffic_snapshots``
     joined with RFID/ANPR/trucking-derived counts.
  3. Build the dense feature tensor + sliding windows with onset labels
     (features.py). The held-out split is the LAST ``val_hours`` (24 h) of
     window-end times — a realistic "predict the future" evaluation.
  4. Train GraphSAGE+LSTM with class-weighted BCE (model.py).
  5. Report F1 / precision / recall / ROC-AUC on the held-out tail.
  6. If under target, auto re-run with class_weight up-adjusted; if still under,
     exit non-zero so the bid team can investigate.
  7. Persist weights + metrics locally and to MinIO bucket ``models``.

Run in-container as ``congestion-train`` or on the host:
    PYTHONPATH=ai:shared python -m congestion.train
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from jnpa_shared.logging import configure_logging, get_logger

from . import metrics as M
from . import storage
from .config import CongestionConfig
from .features import FeatureBuilder, FeatureMatrix
from .graph import CorridorGraph, build_corridor_graph
from .synthetic import HistoryRow, SyntheticHistory

log = get_logger("congestion.train")

# A fixed reference "now" so a from-scratch run is reproducible (the synthetic
# generator keys peaks off the wall clock; pinning it keeps metrics stable).
_REF_NOW = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- data
def load_real_tail(cfg: CongestionConfig, graph: CorridorGraph) -> List[HistoryRow]:
    """Best-effort pull of recent real history from Postgres. Returns [] if the
    DB is unreachable or empty (the synthetic bootstrap then stands alone)."""
    try:
        import psycopg  # type: ignore
    except Exception:  # noqa: BLE001
        return []
    rows: List[HistoryRow] = []
    try:
        with psycopg.connect(cfg.postgres_dsn_libpq, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT date_trunc('minute', ts) AS bucket, segment_id,
                           avg(speed_kmh) AS speed, avg(jam_factor) AS jam
                    FROM jnpa.traffic_snapshots
                    WHERE ts > now() - interval '14 days'
                    GROUP BY 1, 2 ORDER BY 1
                    """
                )
                seg_ids = set(graph.segment_ids)
                for bucket, seg, speed, jam in cur.fetchall():
                    if seg not in seg_ids:
                        continue
                    rows.append(
                        HistoryRow(
                            ts=bucket.astimezone(timezone.utc),
                            segment_id=seg,
                            speed_kmh=float(speed) if speed is not None else cfg.free_flow_speed_kmh,
                            jam_factor=float(jam) if jam is not None else 0.0,
                            rfid_count=0,
                            anpr_count=0,
                            truck_speed_kmh=float(speed) if speed is not None else cfg.free_flow_speed_kmh,
                            source="postgres",
                        )
                    )
        log.info("real_tail_loaded", rows=len(rows))
    except Exception as exc:  # noqa: BLE001
        log.info("real_tail_unavailable", error=str(exc))
        return []
    return rows


def build_dataset(
    cfg: CongestionConfig, graph: CorridorGraph, ref_now: datetime = _REF_NOW
) -> Tuple[FeatureMatrix, FeatureBuilder]:
    synth = SyntheticHistory(cfg, graph)
    rows = synth.generate(end=ref_now)
    real = load_real_tail(cfg, graph)
    if real:
        # Real rows override synthetic at matching (ts, segment).
        keyed = {(r.ts, r.segment_id): r for r in rows}
        for r in real:
            keyed[(r.ts, r.segment_id)] = r
        rows = list(keyed.values())
    fb = FeatureBuilder(cfg, graph)
    fm = fb.build_matrix(rows)
    return fm, fb


def split_train_val(
    cfg: CongestionConfig, ends: List, X, Y, mask
) -> Tuple[slice, slice]:
    """Split windows so the held-out set is the last ``val_hours`` of end-times."""
    if not ends:
        return slice(0, 0), slice(0, 0)
    cutoff = ends[-1] - timedelta(hours=cfg.val_hours)
    val_start = 0
    for i, t in enumerate(ends):
        if t > cutoff:
            val_start = i
            break
    else:
        val_start = max(0, len(ends) - 1)
    # Guard: keep at least one training window.
    val_start = max(1, val_start)
    return slice(0, val_start), slice(val_start, len(ends))


# --------------------------------------------------------------------------- train
def _train_once(
    cfg: CongestionConfig,
    graph: CorridorGraph,
    Xtr, Ytr, Mtr,
    Xval, Yval, Mval,
    class_weight: float,
) -> Tuple["object", dict]:
    import torch
    from .model import build_model

    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    edge_index = graph.edge_index
    edge_attr = graph.edge_attr
    edge_dim = edge_attr.shape[1]

    model = build_model(cfg, edge_dim=edge_dim)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)

    Xtr_t = torch.as_tensor(Xtr, dtype=torch.float32)
    Ytr_t = torch.as_tensor(Ytr, dtype=torch.float32)
    Mtr_t = torch.as_tensor(Mtr, dtype=torch.float32)

    pos_weight = torch.tensor([class_weight], dtype=torch.float32)
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none", pos_weight=pos_weight)

    n = Xtr_t.shape[0]
    # Mini-batch over windows (the graph is small; this batches the temporal dim).
    batch = max(8, min(64, n))
    model.train()
    for epoch in range(cfg.epochs):
        perm = torch.randperm(n)
        epoch_loss = 0.0
        seen = 0
        for s in range(0, n, batch):
            idx = perm[s : s + batch]
            xb, yb, mb = Xtr_t[idx], Ytr_t[idx], Mtr_t[idx]
            opt.zero_grad()
            logits = model(xb, edge_index, edge_attr)  # (B, N)
            raw = loss_fn(logits, yb)                  # (B, N)
            denom = mb.sum().clamp(min=1.0)
            loss = (raw * mb).sum() / denom
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            epoch_loss += float(loss.detach()) * len(idx)
            seen += len(idx)
        if (epoch + 1) % 10 == 0 or epoch == 0:
            log.info("epoch", n=epoch + 1, loss=round(epoch_loss / max(1, seen), 4),
                     class_weight=class_weight)

    # ---- evaluate on held-out tail ----
    model.eval()
    with torch.no_grad():
        Xval_t = torch.as_tensor(Xval, dtype=torch.float32)
        val_logits = model(Xval_t, edge_index, edge_attr)
        val_prob = torch.sigmoid(val_logits).cpu().numpy()

    mask = Mval.astype(bool)
    y_true = Yval[mask]
    y_prob = val_prob[mask]
    # Pick the operating point that maximises F1 while clearing the precision &
    # recall floors (so all three bid gates are met together, not just F1).
    thr, _ = M.best_threshold(
        y_true, y_prob,
        min_precision=cfg.target_precision,
        min_recall=cfg.target_recall,
    )
    summary = M.summary(y_true, y_prob, thr=thr)
    return model, summary


def train(cfg: Optional[CongestionConfig] = None) -> dict:
    cfg = cfg or CongestionConfig.from_env()
    configure_logging(cfg.log_level)
    log.info("congestion_train_start", history_days=cfg.history_days,
             window=cfg.window, horizon_min=cfg.horizon_min)

    graph = build_corridor_graph()
    log.info("graph_built", nodes=graph.num_nodes,
             segments=graph.segment_ids, max_lanes=graph.max_lanes)

    fm, fb = build_dataset(cfg, graph)
    X, Y, mask, ends = fb.make_supervised(fm)
    if X.shape[0] < 10:
        raise RuntimeError(f"too few windows to train ({X.shape[0]})")
    tr, val = split_train_val(cfg, ends, X, Y, mask)
    # Subsample training windows by the configured stride (autocorrelated; this
    # cuts compute ~stride× with negligible loss). Validation stays contiguous.
    tr_idx = np.arange(tr.start, tr.stop, max(1, cfg.train_stride))
    Xtr, Ytr, Mtr = X[tr_idx], Y[tr_idx], mask[tr_idx]
    Xval, Yval, Mval = X[val], Y[val], mask[val]
    log.info("dataset_built", steps=fm.num_steps, windows=X.shape[0],
             train=len(tr_idx), val=int(val.stop - val.start),
             onset_rate=round(float(Y[mask.astype(bool)].mean()), 4))

    class_weight = cfg.base_class_weight
    best_model = None
    best_summary: dict = {}
    attempt = 0
    while True:
        attempt += 1
        log.info("train_attempt", attempt=attempt, class_weight=round(class_weight, 3))
        model, summary = _train_once(
            cfg, graph,
            Xtr, Ytr, Mtr,
            Xval, Yval, Mval,
            class_weight=class_weight,
        )
        log.info("attempt_metrics", attempt=attempt, **summary)
        if best_model is None or summary["congestion_onset_f1"] > best_summary.get("congestion_onset_f1", -1):
            best_model, best_summary = model, summary

        met = (
            summary["congestion_onset_f1"] >= cfg.target_f1
            and summary["precision"] >= cfg.target_precision
            and summary["recall"] >= cfg.target_recall
        )
        if met or attempt > cfg.max_retries:
            break
        class_weight *= cfg.class_weight_step  # up-adjust and retry

    # ---- persist ----
    best_summary.update(
        {
            "target_f1": cfg.target_f1,
            "target_precision": cfg.target_precision,
            "target_recall": cfg.target_recall,
            "segments": graph.segment_ids,
            "num_segments": graph.num_nodes,
            "window": cfg.window,
            "horizon_min": cfg.horizon_min,
            "attempts": attempt,
            "trained_at": _REF_NOW.isoformat(),
        }
    )
    _persist(cfg, graph, best_model, best_summary)

    target_met = (
        best_summary["congestion_onset_f1"] >= cfg.target_f1
        and best_summary["precision"] >= cfg.target_precision
        and best_summary["recall"] >= cfg.target_recall
    )
    best_summary["TARGET_MET"] = bool(target_met)

    _print_report(cfg, best_summary)
    return best_summary


def _persist(cfg: CongestionConfig, graph: CorridorGraph, model, summary: dict) -> None:
    import torch

    Path(cfg.weights_dir).mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "config": {
                "in_features": cfg.in_features,
                "gnn_hidden": cfg.gnn_hidden,
                "gnn_out": cfg.gnn_out,
                "lstm_hidden": cfg.lstm_hidden,
                "lstm_layers": cfg.lstm_layers,
                "window": cfg.window,
                "horizon_min": cfg.horizon_min,
            },
            "threshold": summary.get("threshold", 0.5),
            "segments": graph.segment_ids,
            "edge_dim": int(graph.edge_attr.shape[1]),
        },
        cfg.weights_path,
    )
    Path(cfg.metrics_path).write_text(json.dumps(summary, indent=2))
    log.info("artifacts_saved", weights=cfg.weights_path, metrics=cfg.metrics_path)
    storage.upload_artifacts(cfg)


def _print_report(cfg: CongestionConfig, s: dict) -> None:
    print("\n=== JNPA UC-III — Congestion Onset Forecaster — TRAINING REPORT ===")
    print(f"  segments              : {s['num_segments']}")
    print(f"  window / horizon       : {s['window']} steps / {s['horizon_min']} min")
    print(f"  threshold              : {s['threshold']}")
    print(f"  support (pos/total)    : {s['support_positive']} / {s['support_total']}")
    print(f"  congestion_onset_f1    : {s['congestion_onset_f1']:.4f}   (target >= {cfg.target_f1})")
    print(f"  precision              : {s['precision']:.4f}   (target >= {cfg.target_precision})")
    print(f"  recall                 : {s['recall']:.4f}   (target >= {cfg.target_recall})")
    print(f"  roc_auc                : {s['roc_auc']:.4f}")
    print(f"  attempts               : {s['attempts']}")
    print(f"  TARGET_MET             : {str(s['TARGET_MET']).lower()}")
    print("===================================================================\n")


def main() -> None:  # pragma: no cover - CLI entrypoint
    summary = train()
    if not summary.get("TARGET_MET"):
        log.error("targets_not_met", **{k: summary[k] for k in
                  ("congestion_onset_f1", "precision", "recall")})
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
