"""OCR accuracy metrics: edit distance, CER, WER, exact-match, and the combined
weighted accuracy used for the OCR_TARGET_MET gate.

Pure functions, no external deps beyond the stdlib — imported by both the
``/eval`` endpoint and the standalone ``eval/bench.py`` runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insertions/deletions/substitutions)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def char_error_rate(pred: str, truth: str) -> float:
    """CER = edit_distance / len(truth). 0.0 when both empty."""
    if not truth:
        return 0.0 if not pred else 1.0
    return levenshtein(pred, truth) / len(truth)


def word_error_rate(pred: str, truth: str) -> float:
    """For single-token plates WER is 0 on exact match, else 1."""
    return 0.0 if pred == truth else 1.0


@dataclass
class SliceMetrics:
    name: str
    n: int
    exact_match: float        # fraction exactly correct
    mean_cer: float
    mean_wer: float
    char_accuracy: float      # 1 - mean_cer

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "n": self.n,
            "exact_match": round(self.exact_match, 4),
            "mean_cer": round(self.mean_cer, 4),
            "mean_wer": round(self.mean_wer, 4),
            "char_accuracy": round(self.char_accuracy, 4),
        }


def score_slice(name: str, preds: Sequence[str], truths: Sequence[str]) -> SliceMetrics:
    assert len(preds) == len(truths), "preds/truths length mismatch"
    n = len(truths)
    if n == 0:
        return SliceMetrics(name, 0, 0.0, 0.0, 0.0, 0.0)
    cers = [char_error_rate(p, t) for p, t in zip(preds, truths)]
    exact = sum(1 for p, t in zip(preds, truths) if p == t) / n
    mean_cer = sum(cers) / n
    mean_wer = sum(word_error_rate(p, t) for p, t in zip(preds, truths)) / n
    return SliceMetrics(
        name=name,
        n=n,
        exact_match=exact,
        mean_cer=mean_cer,
        mean_wer=mean_wer,
        char_accuracy=1.0 - mean_cer,
    )


# Weighting for the combined accuracy gate: clean conditions dominate but the
# degraded slices (the bid's hard cases) carry real weight.
DEFAULT_WEIGHTS: Dict[str, float] = {"clean": 0.5, "dust_haze": 0.25, "night": 0.25}


def combined_weighted_accuracy(
    slices: List[SliceMetrics], weights: Dict[str, float] = DEFAULT_WEIGHTS
) -> float:
    """Weighted mean of per-slice exact-match accuracy, as a percentage."""
    num = 0.0
    den = 0.0
    for sm in slices:
        w = weights.get(sm.name, 0.0)
        num += w * sm.exact_match
        den += w
    if den == 0:
        return 0.0
    return 100.0 * num / den


__all__ = [
    "levenshtein",
    "char_error_rate",
    "word_error_rate",
    "SliceMetrics",
    "score_slice",
    "combined_weighted_accuracy",
    "DEFAULT_WEIGHTS",
]
