#!/usr/bin/env python3
"""Live smoke test for all 13 granted ULIP APIs.

Run this the moment NLDSL confirms the egress IP is whitelisted — it is the
test that finally validates the credentials, because nothing else can. ULIP
gates ``POST /user/login`` on a source-IP allowlist and answers HTTP 412
"Access denied Please contact ULIP support!" **identically for a nonexistent
username**, so until an IP is registered a 412 says nothing whatsoever about
whether the username/password are correct.

Usage::

    ULIP_API_URL=https://www.ulipstaging.dpiit.gov.in/ulip/v1.0.0 \\
    ULIP_CLIENT_ID=... ULIP_CLIENT_SECRET=... \\
    python scripts/ulip_smoke.py [--state-id 27] [--json]

Exit codes: 0 every granted API answered; 1 at least one failed; 2 login
itself failed (nothing else was attempted).

Sample values are the ones the integration PDFs themselves publish for
STAGING, so a miss here is a real integration problem rather than a bad guess
at test data. A "not found" answer still counts as a PASS: it proves the call
was authenticated, routed and understood — which is exactly what this script
is checking. Only a transport/auth/format failure is a FAIL.

No credential or issued token is ever printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from integrations.ulip import (  # noqa: E402
    UlipAccessDenied,
    UlipClient,
    UlipError,
)
from integrations.ulip.schemas import (  # noqa: E402
    normalize_container_events,
    normalize_dl,
    normalize_rc,
    normalize_road_network,
    normalize_tag_status,
    normalize_toll_plazas,
    normalize_vahan_xml,
    normalize_vehicle_events,
)

# Staging sample values, verbatim from ulip-docs/*.pdf.
SAMPLES = {
    "fastag_vehicle": "CG07BC9186",            # FASTag doc §1.3 staging
    "fastag_tagid": "34161FA8203286140F4064E0",  # FASTag doc §1.4 staging
    "container": "NSST1234570",                # LDB doc §1.3 staging
    "vahan_vehicle": "UP32KH0320",             # VAHAN doc §1.4 staging
    "dl": "AP01620210000019",                  # SARATHI doc §1.4 staging
    "nh_no": "NH-5",                           # GATISHAKTI doc §1.3
    "state_id": "19",                          # GATISHAKTI doc §1.4 staging
}


class Check:
    def __init__(self, api: str, label: str, call: Callable, summarise: Callable) -> None:
        self.api, self.label, self.call, self.summarise = api, label, call, summarise


def _build(client: UlipClient, state_id: str) -> List[Check]:
    """One Check per granted API, in the order a reviewer reads the grant."""
    s = SAMPLES
    return [
        Check("FASTAG/01", f"toll crossings for {s['fastag_vehicle']}",
              lambda: client.fetch_vehicle_movement(s["fastag_vehicle"]),
              lambda e: f"{len(normalize_vehicle_events(e, s['fastag_vehicle']))} crossings "
                        f"(72 h retention — 0 is normal)"),
        Check("FASTAG/02", f"tag registry for {s['fastag_tagid'][:8]}…",
              lambda: client.fetch_tag_status(tag_id=s["fastag_tagid"]),
              lambda e: f"{len(normalize_tag_status(e))} tag(s)"),
        Check("LDB/01", f"container {s['container']}",
              lambda: client.fetch_container_tracking(s["container"]),
              lambda e: f"{len(normalize_container_events(e, s['container']))} movements"),
        Check("VAHAN/04", f"RC (JSON) for {s['vahan_vehicle']}",
              lambda: client.fetch_vehicle_by_rc(s["vahan_vehicle"]),
              lambda e: _rc_summary(normalize_rc(e))),
        Check("VAHAN/01", f"RC (XML) for {s['vahan_vehicle']}",
              lambda: client.fetch_vehicle_by_rc_xml(s["vahan_vehicle"]),
              lambda e: _rc_summary(normalize_vahan_xml(e))),
        Check("VAHAN/02", "RC by chassis (from the VAHAN/04 answer)",
              lambda: _by_chassis(client),
              lambda e: _rc_summary(normalize_vahan_xml(e))),
        Check("VAHAN/03", "RC by engine (from the VAHAN/04 answer)",
              lambda: _by_engine(client),
              lambda e: _rc_summary(normalize_vahan_xml(e))),
        Check("SARATHI/02", f"DL {s['dl']}",
              lambda: client.fetch_dl(s["dl"]),
              lambda e: _dl_summary(normalize_dl(e))),
        Check("SARATHI/01", f"DL {s['dl']} + DOB",
              lambda: client.fetch_dl_with_dob(s["dl"], "1987-05-26"),
              lambda e: "answered (response shape is in the unsupplied .docx)"),
        Check("GATISHAKTI/01", f"NH road {s['nh_no']}",
              lambda: client.fetch_nh_road(s["nh_no"]),
              lambda e: f"{len(normalize_road_network(e, nh_no=s['nh_no']))} rows"),
        Check("GATISHAKTI/02", f"state road network {state_id}",
              lambda: client.fetch_state_roads(state_id),
              lambda e: f"{len(normalize_road_network(e, state_id=state_id))} rows"),
        Check("GATISHAKTI/03", f"road points {state_id}",
              lambda: client.fetch_state_road_points(state_id),
              lambda e: f"{len(normalize_road_network(e, state_id=state_id))} points"),
        Check("GATISHAKTI/04", f"toll plazas {state_id}",
              lambda: client.fetch_toll_plazas(state_id),
              lambda e: f"{len(normalize_toll_plazas(e, state_id))} plazas"),
    ]


# VAHAN/02 and /03 need a chassis / engine number, which only VAHAN/04 can
# supply for a sample plate. Cached after the first lookup so the smoke run
# costs one extra call, not three.
_rc_cache: Dict[str, Any] = {}


async def _rc_fields(client: UlipClient) -> Dict[str, Any]:
    if "fields" not in _rc_cache:
        envelope = await client.fetch_vehicle_by_rc(SAMPLES["vahan_vehicle"])
        _rc_cache["fields"] = normalize_rc(envelope) or {}
    return _rc_cache["fields"]


async def _by_chassis(client: UlipClient):
    fields = await _rc_fields(client)
    chassis = (fields.get("chassis_number") or "").replace("*", "").strip()
    if not chassis:
        raise UlipError("VAHAN/04 returned no usable chassis number "
                        "(the field is masked or the vehicle is unknown)")
    return await client.fetch_vehicle_by_chassis(chassis)


async def _by_engine(client: UlipClient):
    fields = await _rc_fields(client)
    engine = (fields.get("engine_number") or "").replace("*", "").strip()
    if not engine:
        raise UlipError("VAHAN/04 returned no usable engine number "
                        "(the field is masked or the vehicle is unknown)")
    return await client.fetch_vehicle_by_engine(engine)


def _rc_summary(fields: Optional[Dict[str, Any]]) -> str:
    if not fields:
        return "no vehicle found (a valid 200 answer)"
    return f"{fields.get('rc_number')} · {fields.get('maker_model') or fields.get('vehicle_class')}"


def _dl_summary(fields: Optional[Dict[str, Any]]) -> str:
    if not fields:
        return "no licence found (a valid 200 answer)"
    return (f"{fields.get('dl_status')} · "
            f"{len(fields.get('vehicle_classes') or [])} class(es)")


async def run(state_id: str) -> tuple[int, List[Dict[str, Any]]]:
    client = UlipClient()
    if not client.configured:
        print("FATAL: no ULIP credential. Set ULIP_CLIENT_ID + ULIP_CLIENT_SECRET "
              "(or ULIP_API_KEY).", file=sys.stderr)
        return 2, []
    print(f"endpoint : {client.api_url}")
    print(f"auth     : {client.auth_mode}")
    print()

    results: List[Dict[str, Any]] = []
    failures = 0
    for check in _build(client, state_id):
        t0 = time.perf_counter()
        try:
            envelope = await check.call()
            detail = check.summarise(envelope)
            status = "PASS"
        except UlipAccessDenied:
            # The whole point of this script. Stop immediately: every remaining
            # call would fail the same way and add nothing but noise.
            print("BLOCKED: ULIP answered 412 — this deployment's egress IP is "
                  "not on NLDSL's allowlist.")
            print("         The credentials were never evaluated; a 412 is "
                  "identical for a nonexistent username.")
            print("         Ask NLDSL to whitelist the egress IP, then re-run.")
            return 2, results
        except UlipError as exc:
            detail, status = f"{type(exc).__name__}: {exc}", "FAIL"
            failures += 1
        ms = (time.perf_counter() - t0) * 1000
        results.append({"api": check.api, "label": check.label,
                        "status": status, "detail": detail,
                        "latency_ms": round(ms, 1)})
        print(f"{status:4}  {check.api:<14} {ms:7.0f}ms  {check.label}")
        print(f"        -> {detail}")
    print()
    print(f"{len(results) - failures}/{len(results)} granted APIs answered.")
    return (1 if failures else 0), results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-id", default=SAMPLES["state_id"],
                    help="LGD state code for the GATISHAKTI calls "
                         "(27 = Maharashtra; default is the doc's sample, 19)")
    ap.add_argument("--json", action="store_true",
                    help="also emit the results as JSON on stdout")
    args = ap.parse_args()
    code, results = asyncio.run(run(args.state_id))
    if args.json:
        print(json.dumps(results, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
