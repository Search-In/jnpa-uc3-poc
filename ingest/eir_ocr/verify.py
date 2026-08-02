"""Batch verifier: OCR images under a directory and compare to expected fixtures.

Reuses the docs/ocr_gate_docs.py verdict vocabulary
(FOUND_EXACT / FOUND_CONFUSION / FOUND_FUZZY / NOT_FOUND / …).

Usage:
    eir-ocr-verify --images-dir /path/to/EIR \\
                   --expected fixtures/eir_expected.json \\
                   --out /tmp/eir_ocr_report.md --json /tmp/eir_ocr_report.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import OcrEngine, sha256_hex
from .extract import FieldValue, field_order_for, flat_values
from .normalize import fold_confusion, norm_alnum, norm_spaced

HERE = Path(__file__).resolve().parent
DEFAULT_EXPECTED = HERE / "fixtures" / "eir_expected.json"

SENTINELS = {"NIL", "EMPTY", "NOSEAL", "READ SMS", "NA", "N/A", ""}
FUZZY_THRESHOLD = 0.80
MIN_MATCH_CHARS = 3
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


def best_window_ratio(needle: str, hay: str) -> float:
    n, m = len(needle), len(hay)
    if n == 0 or m == 0:
        return 0.0
    if n >= m:
        return difflib.SequenceMatcher(None, needle, hay).ratio()
    best = 0.0
    stride = max(1, n // 4)
    for i in range(0, m - n + 1, stride):
        r = difflib.SequenceMatcher(None, needle, hay[i : i + n + 2]).ratio()
        if r > best:
            best = r
            if best >= 0.99:
                break
    return best


def verdict_for(value: str, text_alnum: str, text_spaced: str) -> Tuple[str, float]:
    v = str(value).strip()
    if norm_spaced(v) in SENTINELS:
        return "SKIPPED_SENTINEL", 0.0
    va = norm_alnum(v)
    if len(va) < MIN_MATCH_CHARS:
        return "SKIPPED_SHORT", 0.0
    if va in text_alnum or norm_spaced(v) in text_spaced:
        return "FOUND_EXACT", 1.0
    if fold_confusion(v) in fold_confusion(text_alnum):
        return "FOUND_CONFUSION", 1.0
    tokens = [t for t in norm_spaced(v).split() if len(t) >= MIN_MATCH_CHARS]
    if len(tokens) >= 4:
        hit = sum(1 for t in tokens if t in text_spaced or norm_alnum(t) in text_alnum)
        ratio = hit / len(tokens)
        if ratio >= FUZZY_THRESHOLD:
            return "FOUND_FUZZY", round(ratio, 2)
    ratio = best_window_ratio(va, text_alnum)
    if ratio >= FUZZY_THRESHOLD:
        return "FOUND_FUZZY", round(ratio, 2)
    return "NOT_FOUND", round(ratio, 2)


def load_expected(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def discover_images(images_dir: Path) -> List[Path]:
    files = []
    for p in sorted(images_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
    return files


def match_stem(sha12: str, expected: Dict[str, Any]) -> Optional[str]:
    by_sha = expected.get("by_sha256_12") or {}
    if sha12 in by_sha:
        return by_sha[sha12]
    for stem, meta in (expected.get("documents") or {}).items():
        if meta.get("sha256_12") == sha12:
            return stem
    return None


def write_report(
    path: Path,
    *,
    engine_ver: str,
    results: Dict[str, Any],
    selftest: List[Dict[str, Any]],
) -> None:
    lines: List[str] = []
    w = lines.append
    w("# EIR OCR Verification Report\n")
    w(f"- engine: tesseract {engine_ver}")
    w(f"- photos OCR'd: {len(results)}")
    w("")
    # summary
    counts: Dict[str, int] = {}
    for doc in results.values():
        for row in doc.get("expected_rows") or []:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    w("## Summary\n")
    w("| verdict | fields |")
    w("|---|---|")
    for k in sorted(counts):
        w(f"| {k} | {counts[k]} |")
    w("")
    w("## Self-test (key fields)\n")
    w("| doc | field | expect | got | pass |")
    w("|---|---|---|---|---|")
    for t in selftest:
        mark = "PASS" if t["ok"] else "FAIL"
        w(f"| {t['stem']} | {t['field']} | {t['expect']} | {t['got']} | {mark} |")
    w("")
    for stem, doc in results.items():
        w(f"### {stem}\n")
        w(f"- photo: `{doc['photo']}`")
        w(f"- sha256[:12]: `{doc['sha256_12']}`")
        w(f"- early_exit: {doc.get('early_exit')} · variants_run: {doc.get('variants_run')}")
        w(f"- extracted: `{json.dumps(doc.get('extracted') or {}, ensure_ascii=False)}`")
        if doc.get("extras"):
            w(f"- extras (unmapped labels): `{json.dumps(doc.get('extras') or {}, ensure_ascii=False)}`")
        w("")
        if doc.get("expected_rows"):
            w("| field | expected | verdict | ratio | extracted |")
            w("|---|---|---|---|---|")
            for row in doc["expected_rows"]:
                w(
                    f"| {row['field']} | {row['value']} | {row['verdict']} | "
                    f"{row['ratio']} | {row.get('extracted', '')} |"
                )
            w("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_verify(
    images_dir: Path,
    expected_path: Path,
    out_md: Path,
    out_json: Optional[Path] = None,
) -> int:
    expected = load_expected(expected_path)
    engine = OcrEngine()
    if not engine.ready:
        print(f"error: tesseract not ready ({engine.version})", file=sys.stderr)
        return 2

    results: Dict[str, Any] = {}
    for img in discover_images(images_dir):
        print(f"ocr: {img.name}", file=sys.stderr)
        raw = img.read_bytes()
        digest = sha256_hex(raw)
        sha12 = digest[:12]
        stem = match_stem(sha12, expected) or img.stem
        ocr = engine.infer_bytes(raw, doc_type="EIR")
        extracted = flat_values(ocr.fields)
        extras = flat_values(ocr.extras) if ocr.extras else {}
        text_alnum = norm_alnum(ocr.raw_text)
        text_spaced = norm_spaced(ocr.raw_text)

        meta = (expected.get("documents") or {}).get(stem) or {}
        exp_fields: Dict[str, Any] = meta.get("fields") or {}
        # Sort expected rows like the printed slip for this terminal.
        order = field_order_for(
            {"Terminal": FieldValue(value=extracted.get("Terminal", ""), conf=1.0, evidence="")}
        )
        order_index = {name: i for i, name in enumerate(order)}
        fixture_index = {name: i for i, name in enumerate(exp_fields.keys())}

        rows = []
        for field, value in sorted(
            exp_fields.items(),
            key=lambda kv: (order_index.get(kv[0], 10_000), fixture_index.get(kv[0], 10_000)),
        ):
            if value is None or isinstance(value, (dict, list)):
                continue
            # blank expectation: field should be absent from extraction
            if value == "" or str(value).upper() == "__BLANK__":
                got = extracted.get(field, "")
                verdict = "FIELD_ABSENT" if not got else "UNEXPECTED_VALUE"
                rows.append({
                    "field": field,
                    "value": "(blank)",
                    "verdict": verdict,
                    "ratio": 0.0,
                    "extracted": got,
                })
                continue
            verdict, ratio = verdict_for(str(value), text_alnum, text_spaced)
            got_ext = extracted.get(field, "")
            # Recovered fields (plate repair, JE→UE, ALVY→4L10, YES/NO flags)
            # often won't appear literally in raw OCR — credit exact extracted match.
            if got_ext:
                if norm_alnum(got_ext) == norm_alnum(str(value)):
                    verdict, ratio = "FOUND_EXACT", 1.0
                elif fold_confusion(got_ext) == fold_confusion(str(value)):
                    verdict, ratio = "FOUND_CONFUSION", 1.0
                elif norm_spaced(got_ext) == norm_spaced(str(value)):
                    verdict, ratio = "FOUND_EXACT", 1.0
            rows.append({
                "field": field,
                "value": str(value),
                "verdict": verdict,
                "ratio": ratio,
                "extracted": got_ext,
            })

        results[stem] = {
            "photo": str(img),
            "sha256_12": sha12,
            "extracted": extracted,
            "extras": extras,
            "raw_text": ocr.raw_text,
            "confidence": ocr.confidence,
            "early_exit": ocr.early_exit,
            "variants_run": ocr.variants_run,
            "expected_rows": rows,
        }

    # Self-test: key fields from fixture
    selftest: List[Dict[str, Any]] = []
    for item in expected.get("selftest") or []:
        stem = item["stem"]
        field = item["field"]
        expect = item["expect"]  # found | blank | best-effort
        doc = results.get(stem) or {}
        row = next(
            (r for r in doc.get("expected_rows") or [] if r["field"] == field),
            None,
        )
        got = row["verdict"] if row else "FIELD_ABSENT"
        if expect == "found":
            ok = got in ("FOUND_EXACT", "FOUND_FUZZY", "FOUND_CONFUSION")
            # Also pass when extracted value matches fixture even if OCR text skipped short tokens.
            if not ok and row and row.get("extracted"):
                exp_val = (expected.get("documents") or {}).get(stem, {}).get("fields", {}).get(field)
                if exp_val and norm_alnum(str(row["extracted"])) == norm_alnum(str(exp_val)):
                    ok = True
                    got = "EXTRACTED_MATCH"
        elif expect == "blank":
            ok = got in ("NOT_FOUND", "FIELD_ABSENT")
        else:
            ok = True
        selftest.append({
            "stem": stem,
            "field": field,
            "expect": expect,
            "got": got,
            "ok": ok,
        })

    write_report(out_md, engine_ver=engine.version, results=results, selftest=selftest)
    print(f"report: {out_md}")
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "tesseract": engine.version,
                    "results": results,
                    "selftest": selftest,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"json:   {out_json}")

    failures = [t for t in selftest if not t["ok"]]
    if failures:
        print(
            f"self-test: {len(selftest) - len(failures)}/{len(selftest)} passed "
            f"({', '.join(t['stem'] + '.' + t['field'] for t in failures)} failed)",
            file=sys.stderr,
        )
        return 1
    print(f"self-test: {len(selftest)}/{len(selftest)} passed")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_images = os.environ.get("EIR_IMAGES_DIR", "")
    ap.add_argument(
        "--images-dir",
        default=default_images,
        help="Directory of EIR JPEGs (or set EIR_IMAGES_DIR)",
    )
    ap.add_argument("--expected", default=str(DEFAULT_EXPECTED))
    ap.add_argument("--out", default=str(HERE / "fixtures" / "eir_ocr_report.md"))
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    if not args.images_dir:
        # Heuristic: workspace ../../EIR relative to jnpa-uc3-poc
        cand = HERE.parents[2] / "EIR"  # .../JNPA/EIR when package is in jnpa-uc3-poc/ingest/eir_ocr
        if not cand.is_dir():
            cand = HERE.parents[1].parent.parent / "EIR"
        args.images_dir = str(cand)

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        print(f"error: images dir not found: {images_dir}", file=sys.stderr)
        return 2
    expected = Path(args.expected)
    if not expected.is_file():
        print(f"error: expected fixture not found: {expected}", file=sys.stderr)
        return 2

    return run_verify(
        images_dir,
        expected,
        Path(args.out),
        Path(args.json_out) if args.json_out else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
