#!/usr/bin/env python3
"""Generate the JNPA Port-Data API integration evidence pack.

Emits, under ``evidence/jnpa_api/`` (or --out), the artefacts the submission
pack needs for the per-use-case "Data Integration & Fallback" item and the
D1-3 "API Management & Security" criterion:

  api_ingest_runs.jsonl     one line per sync run (core.api_ingest_run) — the
                            audit trail: auth flow exercised, records/files/
                            checksum-skips, rate-limit floor, defects.
  api_records_summary.csv   per-group counts + routed-status breakdown
                            (core.api_record) — what was ingested and how it
                            was consumed.
  api_report_snapshots.csv  report-group land-raw-then-map outcomes.
  defect_report.md          the static 45-item register (docs/JNPA_API_DEFECTS.md)
                            merged with the runtime observations
                            (core.api_defect_log) — the report JNPA's
                            31-Jul-2026 notice requires.
  evidence_manifest.json    what was produced, when, from which DB/mode.

Degrades gracefully: with no reachable DB it still emits the static defect
report and a manifest noting the DB was unavailable, so the pack is never
empty. Timestamps are passed in / stamped by the caller's clock via --now
(kept explicit so the artefacts are reproducible).

Usage:
  python scripts/jnpa_evidence.py --dsn "$POSTGRES_DSN" --out evidence/jnpa_api
  python scripts/jnpa_evidence.py --static-only     # no DB, catalogue only
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFECTS_DOC = REPO_ROOT / "docs" / "JNPA_API_DEFECTS.md"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _collect(dsn: str) -> Dict[str, Any]:
    """Pull the evidence rows via the tested SyncRepository DAO."""
    from services.jnpa_sync.repository import SyncRepository

    repo = SyncRepository(dsn)
    runs = await repo.list_runs(limit=10_000)
    records = await repo.list_records(limit=100_000)
    reports = await repo.list_report_snapshots(limit=100_000)
    defects = await repo.list_defects(limit=100_000)
    return {"runs": runs, "records": records, "reports": reports,
            "defects": defects}


def _write_runs_jsonl(runs: List[Dict[str, Any]], out: Path) -> int:
    path = out / "api_ingest_runs.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for run in runs:
            fh.write(json.dumps(run, default=str) + "\n")
    return len(runs)


def _write_records_summary(records: List[Dict[str, Any]], out: Path) -> int:
    """Per-group × routed_status counts + byte totals."""
    summary: Dict[tuple, Dict[str, Any]] = {}
    for rec in records:
        key = (rec.get("group_slug"), rec.get("routed_status"))
        row = summary.setdefault(key, {"group": key[0],
                                       "routed_status": key[1],
                                       "records": 0, "bytes": 0})
        row["records"] += 1
        row["bytes"] += int(rec.get("size_bytes") or 0)
    path = out / "api_records_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["group", "routed_status", "records", "bytes"])
        writer.writeheader()
        for row in sorted(summary.values(),
                          key=lambda r: (r["group"] or "", r["routed_status"] or "")):
            writer.writerow(row)
    return len(summary)


def _write_reports_summary(reports: List[Dict[str, Any]], out: Path) -> int:
    path = out / "api_report_snapshots.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["group_slug", "report_date", "terminal",
                            "item_count", "mapped_status", "fetched_at"])
        writer.writeheader()
        for r in reports:
            writer.writerow({k: r.get(k) for k in
                             ("group_slug", "report_date", "terminal",
                              "item_count", "mapped_status", "fetched_at")})
    return len(reports)


def _write_defect_report(defects: List[Dict[str, Any]], out: Path) -> None:
    """Static catalogue + runtime observations, one document."""
    parts: List[str] = []
    if DEFECTS_DOC.exists():
        parts.append(DEFECTS_DOC.read_text(encoding="utf-8").rstrip())
    else:
        parts.append("# JNPA Port-Data API — Defect Report\n\n"
                     "_(static catalogue docs/JNPA_API_DEFECTS.md not found)_")
    parts.append("\n\n---\n\n## Runtime observations (core.api_defect_log)\n")
    if defects:
        parts.append("| Observed (UTC) | Code | Severity | Endpoint | Detail |")
        parts.append("|---|---|---|---|---|")
        for d in defects:
            detail = str(d.get("description") or "").replace("|", "\\|")
            parts.append(
                f"| {d.get('observed_at')} | {d.get('defect_code')} "
                f"| {d.get('severity')} | {d.get('endpoint') or ''} "
                f"| {detail} |")
        # Frequency roll-up — what recurred, and how often.
        counts: Dict[str, int] = {}
        for d in defects:
            counts[d.get("defect_code")] = counts.get(d.get("defect_code"), 0) + 1
        parts.append("\n### Observation frequency\n")
        parts.append("| Code | Times observed |")
        parts.append("|---|---|")
        for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            parts.append(f"| {code} | {n} |")
    else:
        parts.append("_No runtime observations recorded yet — run a sync "
                     "(`POST /api/integrations/jnpa/sync`) against the live "
                     "endpoint or the local simulator first._")
    (out / "defect_report.md").write_text("\n".join(parts) + "\n",
                                          encoding="utf-8")


def _write_manifest(out: Path, *, dsn_used: bool, mode: str,
                    counts: Dict[str, int], now: str) -> None:
    manifest = {
        "generated_at": now,
        "database_available": dsn_used,
        "api_mode": mode,
        "artifacts": {
            "api_ingest_runs.jsonl": counts.get("runs", 0),
            "api_records_summary.csv": counts.get("record_groups", 0),
            "api_report_snapshots.csv": counts.get("reports", 0),
            "defect_report.md": "static catalogue + "
                                f"{counts.get('defects', 0)} runtime observations",
        },
        "note": ("Evidence for per-use-case Data Integration & Fallback and "
                 "D1-3 API Management & Security. The static defect catalogue "
                 "is docs/JNPA_API_DEFECTS.md; runtime observations accrue in "
                 "core.api_defect_log on every sync."),
    }
    (out / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN", ""),
                    help="SQLAlchemy async DSN (default $POSTGRES_DSN)")
    ap.add_argument("--out", default="evidence/jnpa_api",
                    help="output directory (default evidence/jnpa_api)")
    ap.add_argument("--static-only", action="store_true",
                    help="skip the DB; emit the static defect report only")
    ap.add_argument("--now", default=_utcnow_iso(),
                    help="ISO timestamp to stamp the manifest (reproducibility)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    counts: Dict[str, int] = {}
    dsn_used = False
    mode = "STATIC"
    data = {"runs": [], "records": [], "reports": [], "defects": []}

    if not args.static_only and args.dsn and "127.0.0.1:1" not in args.dsn:
        try:
            data = asyncio.run(_collect(args.dsn))
            dsn_used = True
            mode = (data["runs"][0].get("api_mode")
                    if data["runs"] else "LIVE")
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the pack
            print(f"[warn] DB unavailable ({exc}); emitting static-only pack",
                  file=sys.stderr)

    counts["runs"] = _write_runs_jsonl(data["runs"], out)
    counts["record_groups"] = _write_records_summary(data["records"], out)
    counts["reports"] = _write_reports_summary(data["reports"], out)
    counts["defects"] = len(data["defects"])
    _write_defect_report(data["defects"], out)
    _write_manifest(out, dsn_used=dsn_used, mode=mode, counts=counts,
                    now=args.now)

    print(f"[ok] evidence written to {out}/ "
          f"(db={'yes' if dsn_used else 'no'}, runs={counts['runs']}, "
          f"defect-observations={counts['defects']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
