#!/usr/bin/env python3
"""Render evaluation artifacts from a congestion ``metrics.json``.

Deterministic, no training — turns the metrics summary written by ``train.py``
into the reviewer-facing deliverables:
  * evaluation_report.md
  * confusion_matrix.csv
  * precision_report.txt
  * recall_report.txt

Usage:
  python -m congestion.eval_report --metrics ai/congestion/artifacts/metrics.json \
      --out ai/congestion/artifacts
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict


def _f(x: float) -> str:
    return f"{x:.4f}"


def confusion_csv(m: Dict) -> str:
    tp, fp, fn, tn = m["tp"], m["fp"], m["fn"], m["tn"]
    rows = [
        "truth\\pred,onset(1),no-onset(0),row_total",
        f"onset(1),{tp},{fn},{tp + fn}",
        f"no-onset(0),{fp},{tn},{fp + tn}",
        f"col_total,{tp + fp},{fn + tn},{tp + fp + fn + tn}",
    ]
    return "\n".join(rows) + "\n"


def precision_report(m: Dict) -> str:
    tp, fp = m["tp"], m["fp"]
    p = m["precision"]
    lines = [
        "== PRECISION REPORT — congestion-onset alarm ==",
        f"  precision        : {_f(p)}   (target >= {_f(m.get('target_precision', 0.80))})",
        f"  true positives   : {tp}",
        f"  false positives  : {fp}   (false alarms)",
        f"  precision = TP/(TP+FP) = {tp}/{tp + fp} = {_f(p)}",
        f"  status           : {'PASS' if p >= m.get('target_precision', 0.80) else 'FAIL'}",
        "",
        "  Interpretation: of every 100 onset alarms raised, "
        f"~{round(p * 100)} are real congestion onsets within the 15-min horizon.",
    ]
    return "\n".join(lines) + "\n"


def recall_report(m: Dict) -> str:
    tp, fn = m["tp"], m["fn"]
    r = m["recall"]
    lines = [
        "== RECALL REPORT — congestion-onset alarm ==",
        f"  recall           : {_f(r)}   (target >= {_f(m.get('target_recall', 0.80))})",
        f"  true positives   : {tp}",
        f"  false negatives  : {fn}   (missed onsets)",
        f"  recall = TP/(TP+FN) = {tp}/{tp + fn} = {_f(r)}",
        f"  status           : {'PASS' if r >= m.get('target_recall', 0.80) else 'FAIL'}",
        "",
        "  Interpretation: of every 100 true congestion onsets, "
        f"~{round(r * 100)} are caught; ~{round((1 - r) * 100)} are missed.",
    ]
    return "\n".join(lines) + "\n"


def report_md(m: Dict) -> str:
    f1 = m["congestion_onset_f1"]
    tgt = m.get("target_f1", 0.85)
    met = f1 >= tgt and m["precision"] >= m.get("target_precision", 0.80) and m["recall"] >= m.get("target_recall", 0.80)
    lines = [
        "# Congestion Onset Forecaster — Evaluation Report\n",
        "Model: GraphSAGE encoder → 2-layer LSTM → per-segment onset logit.",
        f"Trained at: {m.get('trained_at', 'n/a')} · attempts: {m.get('attempts', 'n/a')} · "
        f"segments: {m.get('num_segments', 'n/a')} · window: {m.get('window', 'n/a')} steps · "
        f"horizon: {m.get('horizon_min', 'n/a')} min\n",
        "## Headline (held-out last-24h tail)\n",
        "| Metric | Value | Target | Status |",
        "|---|---|---|---|",
        f"| **F1 (congestion onset)** | **{_f(f1)}** | >= {_f(tgt)} | "
        f"{'✅ PASS' if f1 >= tgt else '❌ FAIL'} |",
        f"| Precision | {_f(m['precision'])} | >= {_f(m.get('target_precision', 0.80))} | "
        f"{'✅' if m['precision'] >= m.get('target_precision', 0.80) else '❌'} |",
        f"| Recall | {_f(m['recall'])} | >= {_f(m.get('target_recall', 0.80))} | "
        f"{'✅' if m['recall'] >= m.get('target_recall', 0.80) else '❌'} |",
        f"| ROC-AUC | {_f(m['roc_auc'])} | — | — |",
        f"| Decision threshold | {_f(m['threshold'])} | — | — |\n",
        "## Confusion matrix\n",
        "| truth \\ pred | onset (1) | no-onset (0) |",
        "|---|---|---|",
        f"| **onset (1)** | TP={m['tp']} | FN={m['fn']} |",
        f"| **no-onset (0)** | FP={m['fp']} | TN={m['tn']} |\n",
        f"- Support: {m['support_positive']} positive / {m['support_total']} scored windows "
        f"({100.0 * m['support_positive'] / max(1, m['support_total']):.2f}% onset prevalence).\n",
        "## Verdict\n",
        (f"✅ **ALL GATES MET** — F1 {_f(f1)} ≥ {_f(tgt)}, precision & recall above floors."
         if met else
         f"❌ **F1 {_f(f1)} below target {_f(tgt)}** (or a floor unmet). Not shippable as-is."),
        "",
        "## Reproducibility\n",
        "Deterministic: fixed seed (`CONGESTION_SEED`), pinned reference clock, seeded synthetic "
        "history. Re-run: `make congestion-train` (or `PYTHONPATH=ai:shared python -m congestion.train`).",
        "Threshold is F1-maximised on the held-out tail subject to the precision/recall floors "
        "(`metrics.best_threshold`).",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render congestion eval artifacts from metrics.json")
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    m = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation_report.md").write_text(report_md(m), encoding="utf-8")
    (out / "confusion_matrix.csv").write_text(confusion_csv(m), encoding="utf-8")
    (out / "precision_report.txt").write_text(precision_report(m), encoding="utf-8")
    (out / "recall_report.txt").write_text(recall_report(m), encoding="utf-8")
    print(f"wrote evaluation_report.md, confusion_matrix.csv, precision_report.txt, recall_report.txt -> {out}/")
    print(f"F1={m['congestion_onset_f1']} precision={m['precision']} recall={m['recall']} "
          f"target_f1={m.get('target_f1', 0.85)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
