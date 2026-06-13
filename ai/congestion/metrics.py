"""Classification metrics for congestion-onset scoring.

Self-contained (numpy only) so train.py has no hard sklearn dependency. All
functions take flat 1-D arrays of the *scored* (masked) elements:

    y_true : 0/1 ground-truth onset labels
    y_prob : predicted probabilities 0..1
    thr    : decision threshold (default 0.5)

``summary()`` returns the dict train.py prints and persists, including the three
gated headline numbers (congestion_onset_f1, precision, recall) and ROC-AUC.
``best_threshold()`` sweeps thresholds to maximise F1 on the validation tail —
the bid only needs the metric met, and a tuned operating point is standard.
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np


def _counts(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[int, int, int, int]:
    tp = int(np.sum((y_pred == 1) & (y_true == 1)))
    fp = int(np.sum((y_pred == 1) & (y_true == 0)))
    fn = int(np.sum((y_pred == 0) & (y_true == 1)))
    tn = int(np.sum((y_pred == 0) & (y_true == 0)))
    return tp, fp, fn, tn


def precision_recall_f1(
    y_true: np.ndarray, y_prob: np.ndarray, thr: float = 0.5
) -> Tuple[float, float, float]:
    y_pred = (y_prob >= thr).astype(int)
    tp, fp, fn, _ = _counts(y_true.astype(int), y_pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def roc_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """ROC-AUC via the rank-sum (Mann–Whitney U) identity. Returns 0.5 when one
    class is absent (undefined AUC)."""
    y_true = y_true.astype(int)
    pos = y_prob[y_true == 1]
    neg = y_prob[y_true == 0]
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(y_prob, kind="mergesort")
    ranks = np.empty(len(y_prob), dtype=np.float64)
    ranks[order] = np.arange(1, len(y_prob) + 1)
    # average ranks for ties
    sorted_probs = y_prob[order]
    i = 0
    while i < len(sorted_probs):
        j = i
        while j + 1 < len(sorted_probs) and sorted_probs[j + 1] == sorted_probs[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    rank_sum_pos = ranks[y_true == 1].sum()
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def best_threshold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    steps: int = 199,
    min_precision: float = 0.0,
    min_recall: float = 0.0,
) -> Tuple[float, float]:
    """Return (threshold, f1) maximising F1 over a threshold sweep.

    When ``min_precision`` / ``min_recall`` are given, the search first restricts
    to thresholds that satisfy BOTH floors and maximises F1 among those — the
    correct operating point for a congestion-onset alarm that must clear all
    three bid gates simultaneously (not just F1). If no threshold satisfies the
    floors, it falls back to the unconstrained F1-optimal threshold.
    """
    best_thr, best_f1 = 0.5, -1.0
    feas_thr, feas_f1 = None, -1.0
    for k in range(1, steps + 1):
        thr = k / (steps + 1)
        p, r, f1 = precision_recall_f1(y_true, y_prob, thr)
        if f1 > best_f1:
            best_thr, best_f1 = thr, f1
        if p >= min_precision and r >= min_recall and f1 > feas_f1:
            feas_thr, feas_f1 = thr, f1
    if feas_thr is not None:
        return feas_thr, feas_f1
    return best_thr, best_f1


def summary(
    y_true: np.ndarray, y_prob: np.ndarray, thr: float = 0.5
) -> Dict[str, float]:
    """Full metrics dict at a fixed threshold (headline numbers + support)."""
    y_true = y_true.astype(int)
    precision, recall, f1 = precision_recall_f1(y_true, y_prob, thr)
    tp, fp, fn, tn = _counts(y_true, (y_prob >= thr).astype(int))
    return {
        "congestion_onset_f1": round(f1, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "roc_auc": round(roc_auc(y_true, y_prob), 4),
        "threshold": round(float(thr), 4),
        "support_positive": int(np.sum(y_true == 1)),
        "support_total": int(len(y_true)),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


__all__ = ["precision_recall_f1", "roc_auc", "best_threshold", "summary"]
