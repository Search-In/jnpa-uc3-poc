#!/usr/bin/env python3
"""Standalone ANPR/OCR benchmark runner.

Runs the three held-out evaluation slices and writes ``metrics.json``:

    (a) Clean test set            — expect char accuracy >= 97% (CER < 3%) and
                                     exact-match >= 95%.
    (b) Synthetic dust+haze set   — expect exact-match >= 92%.
    (c) Synthetic night low-light — expect exact-match >= 90%.

Prints a final line ``OCR_TARGET_MET=true|false`` — true requires combined
weighted accuracy >= 95.0%.

Usage:
    python ai/anpr/eval/bench.py [--n 200] [--out ai/anpr/eval/metrics.json]

Runs fully offline (no stack required) using the in-process pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the service package importable when run as a plain script.
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
_SHARED = _HERE.parents[3] / "shared"
for p in (str(_SRC), str(_SHARED)):
    if p not in sys.path:
        sys.path.insert(0, p)

from anpr.config import AnprAiConfig  # noqa: E402
from anpr.evaluator import run_eval  # noqa: E402
from anpr.pipeline import AnprPipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ANPR/OCR benchmark")
    ap.add_argument("--n", type=int, default=None, help="benchmark size (plates)")
    ap.add_argument("--out", type=str, default=str(_HERE.parent / "metrics.json"))
    args = ap.parse_args(argv)

    cfg = AnprAiConfig.from_env()
    pipeline = AnprPipeline(cfg)
    pipeline.warm()

    metrics = run_eval(pipeline, cfg, n=args.n)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    # Human-readable summary.
    print(f"\n== ANPR/OCR benchmark (n={metrics['n']}, source={metrics['source']}, "
          f"engine={metrics['engine']}) ==")
    for sm in metrics["slices"]:
        print(
            f"  {sm['name']:<10} exact={sm['exact_match']:.3f} "
            f"char_acc={sm['char_accuracy']:.3f} CER={sm['mean_cer']:.3f} (n={sm['n']})"
        )
    print("  gates:")
    for gate, ok in metrics["gates"].items():
        print(f"    [{'PASS' if ok else 'FAIL'}] {gate}")
    print(f"  combined_weighted_accuracy = {metrics['combined_weighted_accuracy_pct']:.2f}% "
          f"(target {metrics['target_pct']:.1f}%)")
    print(f"  metrics.json -> {out_path}")

    # The contractual final line.
    print(f"OCR_TARGET_MET={'true' if metrics['OCR_TARGET_MET'] else 'false'}")
    return 0 if metrics["OCR_TARGET_MET"] else 1


if __name__ == "__main__":
    sys.exit(main())
