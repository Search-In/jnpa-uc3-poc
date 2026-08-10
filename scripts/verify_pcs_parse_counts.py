#!/usr/bin/env python3
"""UC1-007 — re-verify PCS journal parse counts and quarantine visibility.

Measures the frozen Digital Twin NLP Marine corpus:

  Inbound  CSV  891 rows → 864 records (651 VESPRO + 136 CALINF + 77 BERMAN)
  Outbound CSV  997 rows → 1,274 records
                (BERALT 728 = 364 calls + 364 BERTH_ALLOTTED events; CALINV 546)
  Quarantine    ~10 failed-transmission rows (empty / non-XML REQUEST, or
                failed VESARR/VESDEP logs)
  Stage Q&A     14 BERMAN .xml FILES  ≠  77 BERMAN journal RECORDS

Usage
-----
    cd jnpa-uc3-poc
    .venv/bin/python scripts/verify_pcs_parse_counts.py \\
      --nlp "/path/to/…/1-NLP Marine"

    # After UC1-002 ingest, also probe the frozen DB:
    POSTGRES_DSN='postgresql+asyncpg://postgres:jnpa_pw@127.0.0.1:5433/jnpa_v3_local' \\
      .venv/bin/python scripts/verify_pcs_parse_counts.py --nlp "$NLP" --dsn "$POSTGRES_DSN"

Exit 0 only when the parse-side ticket targets match (DB checks are reported,
never fatal unless --strict-db).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

from services.marine.parsers import parse_marine  # noqa: E402
from services.marine.parsers.beralt import EVENT_BERTH_ALLOTTED  # noqa: E402

# Ticket-locked targets (UC1-007 Done).
TARGET_IN_ROWS = 891
TARGET_IN_RECORDS = 864
TARGET_IN_BY_MSG = {"VESPRO": 651, "CALINF": 136, "BERMAN": 77}
TARGET_OUT_ROWS = 997
TARGET_OUT_RECORDS = 1274
TARGET_BERALT_CALLS = 364
TARGET_BERALT_EVENTS = 364
TARGET_CALINV = 546
TARGET_BERMAN_XML_FILES = 14
TARGET_QUARANTINE = 10
GOLDEN_VOYAGE = "IG2610W"
GOLDEN_IMO = "9939888"


def _resolve_nlp(explicit: Optional[str]) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("MARINE_DATA_DIR") or os.environ.get("UC1_NLP_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    # Local drop locations used by this PoC laptop.
    candidates.append(_ROOT / "client-data" / "1-NLP Marine")
    candidates.append(_ROOT / "client-data" / "NLP Marine")
    poc1 = _ROOT.parent / "jnpa_poc_1" / "data" / "NLP Marine"
    candidates.append(poc1)
    base = os.environ.get("UC1_CORPUS_BASE") or os.environ.get("CORPUS_BASE")
    if base:
        b = Path(base).expanduser()
        candidates.append(b / "1-NLP Marine")
        candidates.append(b / "NLP Marine")
        if _norm_name(b.name) in ("nlp marine", "1-nlp marine"):
            candidates.append(b)
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "NLP Marine corpus not found. Pass --nlp or set MARINE_DATA_DIR.\n"
        f"Tried:\n  {tried}"
    )


def _norm_name(name: str) -> str:
    return " ".join(name.lower().replace("_", " ").replace("-", " ").split())


def _missing_journals(nlp: Path) -> list[str]:
    """Full-corpus journals required for the 891/997 ticket targets."""
    need = [
        nlp / "Inbound_CALINF_BERMAN" / "NLP Inbound Data Report.csv",
        nlp / "Outbound_CALINV_BERALT" / "NLP Outbound Data Report.csv",
    ]
    return [str(p) for p in need if not p.is_file()]


def _parse_folder(folder: Path) -> dict[str, Any]:
    """Parse every *.csv journal in a folder → aggregate tallies."""
    msgs: Counter[str] = Counter()
    targets: Counter[str] = Counter()
    rows = 0
    records = 0
    invalid = 0
    errors: list[dict[str, Any]] = []
    files = 0
    names: list[str] = []
    if not folder.is_dir():
        return {
            "files": 0, "rows": 0, "records": 0, "invalid": 0,
            "by_message": {}, "by_target": {}, "errors": [], "names": [],
        }
    for f in sorted(folder.glob("*.csv")):
        files += 1
        names.append(f.name)
        res = parse_marine(f.read_bytes(), f.name)
        rows += res.row_count
        records += len(res.records)
        invalid += res.invalid_count
        for e in res.errors:
            errors.append({**e, "_file": f.name})
        for r in res.records:
            msgs[str(r.get("_message") or "?")] += 1
            targets[str(r.get("_target") or "?")] += 1
    return {
        "files": files,
        "rows": rows,
        "records": records,
        "invalid": invalid,
        "by_message": dict(msgs),
        "by_target": dict(targets),
        "errors": errors,
        "names": names,
    }


def _count_xml(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.rglob("*.xml"))


def _vesarr_vesdep_failures(nlp: Path) -> list[dict[str, Any]]:
    """Parse standalone VESARR/VESDEP transmission logs; collect failures."""
    out: list[dict[str, Any]] = []
    for name in ("VESARR", "VESDEP"):
        folder = nlp / name
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in {".log", ".xml", ".hsp"}:
                continue
            res = parse_marine(f.read_bytes(), f.name)
            if res.rejected or res.invalid_count or not res.records:
                out.append({
                    "file": str(f.relative_to(nlp)),
                    "rejected": res.rejected,
                    "invalid": res.invalid_count,
                    "records": len(res.records),
                    "errors": res.errors[:5],
                })
    return out


def _find_golden(inbound: dict[str, Any], nlp: Path) -> dict[str, Any]:
    """Locate CALINF IG2610W / IMO 9939888 and any scientific COMMON_REF_NO."""
    hit: dict[str, Any] = {"voyage": None, "scientific_ref": None}
    # Journals first.
    folder = nlp / "Inbound_CALINF_BERMAN"
    sources: list[Path] = []
    if folder.is_dir():
        sources.extend(sorted(folder.glob("*.csv")))
    calinf_dir = nlp / "CALINF"
    if calinf_dir.is_dir():
        sources.extend(sorted(calinf_dir.glob("*.xml")))
    for f in sources:
        res = parse_marine(f.read_bytes(), f.name)
        for r in res.records:
            voy = str(r.get("voyage_no") or "")
            imo = str(r.get("imo_no") or "")
            note = str(r.get("source_note") or r.get("vespro_ref") or "")
            if GOLDEN_VOYAGE in voy or imo == GOLDEN_IMO:
                hit["voyage"] = {
                    "file": f.name,
                    "voyage_no": voy,
                    "imo_no": imo,
                    "source_note": note,
                    "message": r.get("_message"),
                    "eta": str(r.get("eta") or ""),
                }
            if "E+" in note.upper() or "e+" in note:
                hit["scientific_ref"] = {"file": f.name, "source_note": note}
        for e in res.errors:
            raw = str(e.get("raw_value") or "")
            if "E+" in raw.upper():
                hit.setdefault("quarantine_scientific", []).append(
                    {"file": f.name, "row": e.get("row_number"), "raw": raw})
    return hit


def _check(label: str, got: Any, want: Any, lines: list[str]) -> bool:
    ok = got == want
    mark = "PASS" if ok else "FAIL"
    lines.append(f"  [{mark}] {label}: got {got!r} want {want!r}")
    return ok


async def _db_probe(dsn: str, lines: list[str]) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            files = (await conn.execute(text(
                "SELECT count(*) FROM core.marine_import_files"
            ))).scalar() or 0
            errs = (await conn.execute(text(
                "SELECT count(*) FROM core.marine_import_errors"
            ))).scalar() or 0
            sample = (await conn.execute(text(
                """
                SELECT e.row_number, e.error_message, left(e.raw_data, 120) AS raw, f.filename
                FROM core.marine_import_errors e
                JOIN core.marine_import_files f ON f.id = e.import_file_id
                ORDER BY e.id
                LIMIT 20
                """
            ))).mappings().all()
            golden = (await conn.execute(text(
                """
                SELECT call_id, voyage_no, imo_no, source_note, status
                FROM core.vessel_call
                WHERE voyage_no ILIKE :voy OR imo_no = :imo
                LIMIT 5
                """
            ), {"voy": f"%{GOLDEN_VOYAGE}%", "imo": GOLDEN_IMO})).mappings().all()
        lines.append(f"  DB marine_import_files = {files}")
        lines.append(f"  DB marine_import_errors = {errs}"
                     f"  (ticket quarantine ≈ {TARGET_QUARANTINE})")
        for s in sample:
            lines.append(
                f"    · row {s['row_number']}: "
                f"{(s['error_message'] or '')[:80]}  [{s['filename']}]"
                f" raw={(s['raw'] or '')[:40]!r}"
            )
        if golden:
            for g in golden:
                lines.append(
                    f"  DB golden call: voyage={g['voyage_no']} imo={g['imo_no']} "
                    f"note={g['source_note']} status={g['status']}"
                )
        else:
            lines.append(
                f"  DB golden {GOLDEN_VOYAGE}/{GOLDEN_IMO}: not found "
                "(run ingest_uc1_corpus.py first)"
            )
    finally:
        await eng.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nlp", default=None,
                    help="Path to …/1-NLP Marine (or set MARINE_DATA_DIR)")
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN") or None,
                    help="Optional asyncpg DSN for frozen-DB probe")
    ap.add_argument("--strict-db", action="store_true",
                    help="Also fail when DB quarantine count ≠ 10")
    args = ap.parse_args()

    nlp = _resolve_nlp(args.nlp)
    inbound_dir = nlp / "Inbound_CALINF_BERMAN"
    outbound_dir = nlp / "Outbound_CALINV_BERALT"
    if not inbound_dir.is_dir() or not outbound_dir.is_dir():
        raise SystemExit(
            f"Expected Inbound_CALINF_BERMAN/ and Outbound_CALINV_BERALT/ under {nlp}"
        )

    print(f"NLP root: {nlp}")
    missing = _missing_journals(nlp)
    if missing:
        print("=== Corpus completeness ===")
        print("  [FAIL] Full journals required for UC1-007 ticket targets are missing:")
        for m in missing:
            print(f"    · {m}")
        print("  Present sample pack pieces (XML folders + small outbound CSV) are"
              " still measured below.")
        print()

    inbound = _parse_folder(inbound_dir)
    outbound = _parse_folder(outbound_dir)
    berman_xml = _count_xml(nlp / "BERMAN")
    ves_fail = _vesarr_vesdep_failures(nlp)
    golden = _find_golden(inbound, nlp)

    journal_quarantine = [
        e for e in (inbound["errors"] + outbound["errors"])
        if e.get("error_code") in ("empty_request", "no_xml")
    ]
    quarantine_n = len(journal_quarantine) + len(ves_fail)

    beralt_call_n = 0
    beralt_event_n = 0
    calinv_n = 0
    for f in sorted(outbound_dir.glob("*.csv")):
        res = parse_marine(f.read_bytes(), f.name)
        for r in res.records:
            if r.get("_message") == "BERALT" and r.get("_target") == "vessel_call":
                beralt_call_n += 1
            elif r.get("_message") == "BERALT" and r.get("_target") == "vessel_call_event":
                beralt_event_n += 1
            elif r.get("_message") == "CALINV":
                calinv_n += 1

    lines: list[str] = []
    ok = True
    lines.append(f"=== Inbound (csv files: {inbound.get('names') or 'NONE'}) ===")
    if missing:
        ok = False
        lines.append("  [FAIL] cannot assert 891→864 — inbound journal CSV absent")
        lines.append(f"  [info] inbound rows/records seen = {inbound['rows']}/{inbound['records']}")
    else:
        ok &= _check("inbound CSV data rows", inbound["rows"], TARGET_IN_ROWS, lines)
        ok &= _check("inbound records", inbound["records"], TARGET_IN_RECORDS, lines)
        for msg, want in TARGET_IN_BY_MSG.items():
            ok &= _check(f"inbound {msg}", inbound["by_message"].get(msg, 0), want, lines)

    lines.append(f"=== Outbound (csv files: {outbound.get('names') or 'NONE'}) ===")
    has_full_out = any(n == "NLP Outbound Data Report.csv" for n in outbound.get("names", []))
    has_small_out = any(
        n == "NLP Outbound Data_CALINV_BERALT.csv" for n in outbound.get("names", [])
    )
    if has_full_out:
        ok &= _check("outbound CSV data rows", outbound["rows"], TARGET_OUT_ROWS, lines)
        ok &= _check("outbound records", outbound["records"], TARGET_OUT_RECORDS, lines)
        ok &= _check("BERALT calls", beralt_call_n, TARGET_BERALT_CALLS, lines)
        ok &= _check(f"BERALT {EVENT_BERTH_ALLOTTED} events",
                     beralt_event_n, TARGET_BERALT_EVENTS, lines)
        ok &= _check("BERALT records (calls+events)",
                     beralt_call_n + beralt_event_n, 728, lines)
        ok &= _check("CALINV records", calinv_n, TARGET_CALINV, lines)
    elif has_small_out:
        # Sample-pack small journal (tests pin 45 BERALT + 59 CALINV).
        lines.append("  [info] only small outbound journal present"
                     " (NLP Outbound Data_CALINV_BERALT.csv) — not the full Report.csv")
        ok &= _check("small outbound rows", outbound["rows"], 104, lines)
        ok &= _check("small BERALT calls", beralt_call_n, 45, lines)
        ok &= _check("small BERALT events", beralt_event_n, 45, lines)
        ok &= _check("small CALINV", calinv_n, 59, lines)
        ok = False  # still fail UC1-007 Done until full Report.csv is present
        lines.append("  [FAIL] full NLP Outbound Data Report.csv required for 997→1274")
    else:
        ok = False
        lines.append("  [FAIL] no outbound journal CSV found")

    lines.append("=== Counting bases (stage Q&A) ===")
    ok_files = _check("BERMAN standalone .xml files",
                      berman_xml, TARGET_BERMAN_XML_FILES, lines)
    # File count is independently true even on the sample pack.
    if not ok_files:
        ok = False
    lines.append(
        f"  [info] BERMAN journal records = "
        f"{inbound['by_message'].get('BERMAN', 0)}  "
        f"(WS3 counted FILES={TARGET_BERMAN_XML_FILES}; "
        f"journal counts RECORDS — different bases)"
    )

    lines.append("=== Quarantine ===")
    lines.append(f"  [info] journal empty/no_xml rows = {len(journal_quarantine)}")
    lines.append(f"  [info] VESARR/VESDEP failed files = {len(ves_fail)}")
    lines.append(f"  [info] quarantine total = {quarantine_n}  "
                 f"(ticket ≈ {TARGET_QUARANTINE})")
    for e in journal_quarantine[:15]:
        lines.append(
            f"    · {_file_row(e)} {e.get('error_code')}: "
            f"{(e.get('error_detail') or '')[:90]}"
        )
    for v in ves_fail[:10]:
        lines.append(f"    · {v['file']}: invalid={v['invalid']} rejected={v['rejected']}")

    lines.append("=== Golden anomaly (IG2610W) ===")
    if golden.get("voyage"):
        g = golden["voyage"]
        lines.append(
            f"  [PASS] {g['voyage_no']} IMO {g['imo_no']} in {g['file']} "
            f"note={g['source_note']!r} eta={g['eta']}"
        )
    elif any("Inbound" in m for m in missing):
        lines.append(
            f"  [FAIL] {GOLDEN_VOYAGE} not in journals/CALINF XML pack"
        )
        ok = False
    else:
        lines.append(f"  [FAIL] {GOLDEN_VOYAGE} / {GOLDEN_IMO} not found in inbound parse")
        ok = False
    if golden.get("scientific_ref") or golden.get("quarantine_scientific"):
        lines.append(f"  [info] scientific COMMON_REF evidence: {golden}")

    print("\n".join(lines))

    if args.dsn:
        print("=== Frozen DB ===")
        db_lines: list[str] = []
        asyncio.run(_db_probe(args.dsn, db_lines))
        print("\n".join(db_lines))
        if args.strict_db:
            # Re-query count for strict mode
            async def _err_count() -> int:
                from sqlalchemy import text
                from sqlalchemy.ext.asyncio import create_async_engine
                eng = create_async_engine(args.dsn, pool_pre_ping=True)
                try:
                    async with eng.connect() as conn:
                        return int((await conn.execute(text(
                            "SELECT count(*) FROM core.marine_import_errors"
                        ))).scalar() or 0)
                finally:
                    await eng.dispose()
            n = asyncio.run(_err_count())
            if n != TARGET_QUARANTINE:
                print(f"  [FAIL] DB quarantine {n} ≠ {TARGET_QUARANTINE}")
                ok = False

    print()
    print("UC1-007 verify:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _file_row(e: dict[str, Any]) -> str:
    return f"{e.get('_file', '?')}#{e.get('row_number', '?')}"


if __name__ == "__main__":
    raise SystemExit(main())
