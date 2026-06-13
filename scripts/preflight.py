#!/usr/bin/env python3
"""Hard-coded sanity checks for the JNPA UC-III PoC demo (Prompt 12, Deliverable 4).

Refuse to launch the demo if any prerequisite is missing. Run as a module
(``python -m scripts.preflight``) or imported by ``scripts/demo_drive.py`` and
``tests/e2e/test_full_pipeline.py`` so the rules live in exactly one place.

Checks (all must pass):
  * GOOGLE_MAPS_API_KEY *or* HERE_API_KEY is set      (need at least one)
  * OPENWEATHER_API_KEY is set
  * At least one sample ANPR clip exists in ./data/clips/

On failure: print a human-readable error pointing at the README section that
explains how to fix it, and exit non-zero. Designed to be import-safe (no
side effects beyond reading .env.local / os.environ at call time).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env.local"
CLIPS_DIR = REPO_ROOT / "data" / "clips"

# README anchor the operator should read when a check fails. GitHub slugifies
# headings, so these match the README sections added in Prompt 12.
README = "README.md"
DOC_API_KEYS = f"{README} → \"Where to put API keys\""
DOC_CLIPS = f"{README} → \"ANPR ingestion service\" (run scripts/download_anpr_samples.sh)"
DOC_DEMO = f"{README} → \"Demo & evaluator evidence pack\""

# Clip extensions we accept as a "sample ANPR clip".
CLIP_EXTS = (".mp4", ".mov", ".mkv", ".avi")


def load_env_local() -> None:
    """Populate os.environ from .env.local without clobbering anything already set.

    Mirrors the loader in scripts/bootstrap_check.py so the preflight sees the
    same keys the stack does. Missing .env.local is not fatal here — the per-key
    checks below report the specific missing value with a fix hint.
    """
    if not ENV_FILE.is_file():
        return
    try:
        from dotenv import dotenv_values
    except Exception:  # noqa: BLE001 - dotenv is a dev dep; degrade to manual parse
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        return
    for k, v in dotenv_values(str(ENV_FILE)).items():
        if v is not None:
            os.environ.setdefault(k, v)


def _has(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def check_map_key() -> Tuple[bool, str]:
    ok = _has("GOOGLE_MAPS_API_KEY") or _has("HERE_API_KEY")
    if ok:
        which = "GOOGLE_MAPS_API_KEY" if _has("GOOGLE_MAPS_API_KEY") else "HERE_API_KEY"
        return True, f"map/traffic key present ({which})"
    return (
        False,
        "neither GOOGLE_MAPS_API_KEY nor HERE_API_KEY is set — at least one is "
        f"required for live map/traffic.\n      Fix: set one in {ENV_FILE.name}. "
        f"See {DOC_API_KEYS}.",
    )


def check_openweather_key() -> Tuple[bool, str]:
    if _has("OPENWEATHER_API_KEY"):
        return True, "OPENWEATHER_API_KEY present"
    return (
        False,
        "OPENWEATHER_API_KEY is not set — required for the ANPR weather tagging.\n"
        f"      Fix: set OPENWEATHER_API_KEY in {ENV_FILE.name}. See {DOC_API_KEYS}.",
    )


def check_clips() -> Tuple[bool, str]:
    clips = (
        [p for p in CLIPS_DIR.glob("*") if p.suffix.lower() in CLIP_EXTS]
        if CLIPS_DIR.is_dir()
        else []
    )
    if clips:
        names = ", ".join(sorted(p.name for p in clips)[:4])
        more = f" (+{len(clips) - 4} more)" if len(clips) > 4 else ""
        return True, f"{len(clips)} ANPR clip(s) in data/clips/: {names}{more}"
    return (
        False,
        "no sample ANPR clips found in ./data/clips/.\n"
        "      Fix: run scripts/download_anpr_samples.sh (fetches CC clips, or "
        f"synthesizes 30s MP4s). See {DOC_CLIPS}.",
    )


CHECKS = [
    ("map/traffic key (Google or HERE)", check_map_key),
    ("OpenWeather key", check_openweather_key),
    ("sample ANPR clips", check_clips),
]


def run(verbose: bool = True) -> Tuple[bool, List[Tuple[str, bool, str]]]:
    """Run all checks. Returns ``(all_ok, rows)`` where each row is
    ``(name, ok, detail)``. ``verbose`` prints a PASS/FAIL line per check."""
    load_env_local()
    rows: List[Tuple[str, bool, str]] = []
    for name, fn in CHECKS:
        ok, detail = fn()
        rows.append((name, ok, detail))
        if verbose:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return all(ok for _, ok, _ in rows), rows


def ensure_ready_or_exit() -> None:
    """Run the preflight and exit(2) with a human-readable error if anything
    is missing. Called at the top of scripts/demo_drive.py."""
    print("Preflight sanity checks (Prompt 12, Deliverable 4):\n")
    ok, rows = run(verbose=True)
    if ok:
        print("\nPreflight OK — all demo prerequisites satisfied.\n")
        return
    failures = [(n, d) for n, ok_, d in rows if not ok_]
    print("\n" + "=" * 72)
    print(f"REFUSING TO LAUNCH THE DEMO — {len(failures)} prerequisite(s) missing:")
    print("=" * 72)
    for name, detail in failures:
        print(f"\n  ✗ {name}\n      {detail}")
    print(f"\n  Full setup guide: {DOC_DEMO}\n")
    sys.exit(2)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    ensure_ready_or_exit()
