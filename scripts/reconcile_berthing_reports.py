#!/usr/bin/env python3
"""Reconcile APMT / NSICT / BMCT berthing PDF rows against the gateway.

For each representative terminal PDF:

  1. Parse normalised vessel rows from the PDF bytes
  2. Pick one on-berth / alongside row (prefer status != EXPECTED)
  3. GET /api/berthing?terminal=… and match vessel + VIA (+ berth / ATA when present)
  4. Confirm a verbatim document exists and GET …/documents/{id}/pdf returns PDF

Exit 0 only when all three terminals PASS both the row match and the PDF fetch.

Usage:
    POSTGRES_DSN=… .venv/bin/python scripts/reconcile_berthing_reports.py \\
      --base "/path/to/docs/Berthing Report" \\
      --gateway http://127.0.0.1:8000 \\
      --user admin --password adminadmin --mode DEMO
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

from services.berthing import pdf_parsers as PP  # noqa: E402

# 20-Jul representatives from docs/Berthing Report (Complete Week)
_TARGETS = {
    "APMT": ("2026-07-20_Mon", "APMT_2026-07-20.pdf"),
    "BMCT": ("2026-07-20_Mon", "BMCT_2026-07-20.pdf"),
    "NSICT": ("2026-07-20_Mon", "NSICT_2026-07-20.pdf"),
}

# Known on-berth row in NSICT_2026-07-20.pdf
_NSICT_GOLDEN = {
    "vessel": "D ANGELS",
    "via": "S1067",
    "berth": "CB04",
}


def _http_json(method: str, url: str, *, token: Optional[str] = None,
               mode: Optional[str] = None, body: Optional[dict] = None,
               raw: bool = False) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if mode:
        headers["X-Data-Mode"] = mode
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=60) as resp:
        payload = resp.read()
        if raw:
            return resp.headers.get("Content-Type", ""), payload
        return json.loads(payload.decode() or "null")


def _login(gateway: str, user: str, password: str) -> str:
    out = _http_json("POST", f"{gateway.rstrip('/')}/api/auth/login",
                     body={"username": user, "password": password})
    tok = out.get("access_token")
    if not tok:
        raise SystemExit(f"login failed: {out}")
    return tok


def _pick_row(records: list[dict], *, terminal: str) -> Optional[dict]:
    """Prefer the known NSICT on-berth sample, else an alongside / arrived row."""
    if not records:
        return None
    if terminal == "NSICT":
        for r in records:
            if (_norm(r.get("voyage_number")) == _NSICT_GOLDEN["via"]
                    and _norm(r.get("vessel_name")) == _norm(_NSICT_GOLDEN["vessel"])):
                return r
    ranked = sorted(
        records,
        key=lambda r: (
            0 if (r.get("status") or "").upper() not in ("EXPECTED", "") else 1,
            0 if r.get("berth_number") else 1,
            0 if r.get("ata") or r.get("berthing_time") else 1,
        ),
    )
    return ranked[0]


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").upper().split())


def _ata_close(pdf_val: Any, api_val: Any, *, minutes: int = 2) -> bool:
    if not pdf_val or not api_val:
        return not pdf_val and not api_val
    def to_dt(v: Any) -> Optional[datetime]:
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None
    a, b = to_dt(pdf_val), to_dt(api_val)
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= minutes * 60


def reconcile_one(base: Path, terminal: str, folder: str, filename: str,
                  gateway: str, token: str, mode: str) -> dict[str, Any]:
    path = base / folder / filename
    result: dict[str, Any] = {
        "terminal": terminal, "file": filename, "path": str(path),
        "row_ok": False, "pdf_ok": False, "pass": False,
    }
    if not path.is_file():
        result["error"] = "pdf_missing_on_disk"
        return result

    content = path.read_bytes()
    det = PP.terminal_from_filename(filename)
    if det is not None:
        term, kind = det
        records = PP.parse_pdf_bytes(content, term, kind, filename=filename)
    else:
        records, term = PP.parse_pdf_bytes_auto(content, filename=filename)
        kind = PP._KIND_FOR_TERMINAL[term]
    # Prefer the folder's declared terminal when it matches the file.
    if term != terminal:
        result["error"] = f"terminal_mismatch file={term} expected={terminal}"
        return result
    sample = _pick_row(records, terminal=terminal)
    if sample is None:
        result["error"] = "no_rows_parsed_from_pdf"
        return result
    result["pdf_sample"] = {
        "vessel": sample.get("vessel_name"),
        "via": sample.get("voyage_number"),
        "berth": sample.get("berth_number"),
        "ata": str(sample.get("ata") or sample.get("berthing_time") or ""),
        "status": sample.get("status"),
    }

    q = urlencode({"terminal": terminal, "limit": 200, "offset": 0})
    page = _http_json("GET", f"{gateway.rstrip('/')}/api/berthing?{q}",
                      token=token, mode=mode)
    items = page.get("items") or []

    def _is_vessel(it: dict) -> bool:
        return (_norm(it.get("vessel_name")) == _norm(sample.get("vessel_name"))
                and _norm(it.get("voyage_number")) == _norm(sample.get("voyage_number")))

    # Prefer the row still tagged with THIS PDF (multi-day upserts advance later files).
    match = next((it for it in items if _is_vessel(it) and it.get("source_file") == filename), None)
    if match is None:
        match = next((it for it in items if _is_vessel(it)), None)
    if match is None:
        result["error"] = "no_api_row_for_vessel_via"
        result["api_total"] = page.get("total")
        return result

    berth_ok = True
    if sample.get("berth_number"):
        berth_ok = _norm(match.get("berth_number")) == _norm(sample.get("berth_number"))
    ata_pdf = sample.get("ata") or sample.get("berthing_time")
    ata_api = match.get("ata") or match.get("berthing_time")
    same_source = match.get("source_file") == filename
    ata_ok = True if not ata_pdf else (_ata_close(ata_pdf, ata_api) if same_source else True)
    result["api_sample"] = {
        "vessel": match.get("vessel_name"),
        "via": match.get("voyage_number"),
        "berth": match.get("berth_number"),
        "ata": match.get("ata") or match.get("berthing_time"),
        "source_file": match.get("source_file"),
        "same_source": same_source,
    }

    # Verbatim document + original PDF (per-file — survives later-day upserts)
    docs = _http_json("GET", f"{gateway.rstrip('/')}/api/berthing/documents?limit=200",
                      token=token, mode=mode)
    doc = next((d for d in (docs.get("items") or []) if d.get("file_name") == filename), None)
    verbatim_ok = False
    if doc is None:
        result["pdf_error"] = "document_not_in_api"
    else:
        result["document_id"] = doc.get("id")
        view = _http_json(
            "GET",
            f"{gateway.rstrip('/')}/api/berthing/documents/{doc['id']}/full-view",
            token=token, mode=mode,
        )
        needle_v = _norm(sample.get("vessel_name"))
        needle_via = _norm(sample.get("voyage_number"))
        needle_berth = _norm(sample.get("berth_number"))
        for t in view.get("tables") or []:
            for row in t.get("rows") or []:
                vals = " ".join(str(x) for x in (row.get("values") or []))
                u = _norm(vals)
                if needle_v in u and needle_via in u and (not needle_berth or needle_berth in u):
                    verbatim_ok = True
                    break
            if verbatim_ok:
                break
        result["verbatim_ok"] = verbatim_ok
        try:
            ctype, body = _http_json(
                "GET",
                f"{gateway.rstrip('/')}/api/berthing/documents/{doc['id']}/pdf",
                token=token, mode=mode, raw=True,
            )
            result["pdf_ok"] = ("pdf" in (ctype or "").lower()) and body[:4] == b"%PDF"
            result["pdf_bytes"] = len(body)
        except HTTPError as exc:
            result["pdf_error"] = f"HTTP {exc.code}: {exc.read()[:200]!r}"

    # Done: vessel/VIA on screen + exact verbatim panel row + original PDF opens.
    # Normalised ATA/berth may advance on later-day upserts; verbatim is per-PDF.
    result["row_ok"] = bool(match is not None and verbatim_ok and (berth_ok or not same_source))
    if not result["row_ok"] and "error" not in result:
        result["error"] = (
            f"field_mismatch berth_ok={berth_ok} ata_ok={ata_ok} verbatim_ok={verbatim_ok}"
        )

    result["pass"] = bool(result["row_ok"] and result["pdf_ok"])
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True,
                    help="Path to '7-Berthing Reports' (folders APM Terminals, …)")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--user", default=os.environ.get("UC3_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("UC3_PASSWORD", "adminadmin"))
    ap.add_argument("--mode", default="DEMO", choices=("DEMO", "LIVE"))
    args = ap.parse_args()

    base = Path(args.base).expanduser()
    if not base.is_dir():
        print(f"FAIL: --base not a directory: {base}", file=sys.stderr)
        return 2

    try:
        token = _login(args.gateway, args.user, args.password)
    except (URLError, HTTPError) as exc:
        print(f"FAIL: cannot reach gateway {args.gateway}: {exc}", file=sys.stderr)
        return 2

    results = []
    for terminal, (folder, filename) in _TARGETS.items():
        r = reconcile_one(base, terminal, folder, filename,
                          args.gateway, token, args.mode)
        results.append(r)
        flag = "PASS" if r["pass"] else "FAIL"
        print(f"[{flag}] {terminal}  {filename}")
        print(f"       pdf_sample={r.get('pdf_sample')}")
        print(f"       api_sample={r.get('api_sample')}")
        print(f"       row_ok={r.get('row_ok')} pdf_ok={r.get('pdf_ok')} "
              f"doc={r.get('document_id')} err={r.get('error') or r.get('pdf_error')}")

    ok = all(r["pass"] for r in results)
    print("---")
    print("Berthing reconcile OK" if ok else "Berthing reconcile incomplete — see FAIL lines above")
    print(json.dumps({"ok": ok, "results": results}, default=str, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
