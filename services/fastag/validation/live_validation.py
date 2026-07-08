"""FASTag LIVE validation harness — runs the REAL ULIP client, no mocks.

Executes the Step-5 scenario matrix against the *authorised* FASTag provider and
prints + writes a report capturing, per scenario: correlation-id, endpoint,
outcome (HTTP status or failure category), latency, and retry/attempt count.

It refuses to run unless ``FASTAG_ULIP_URL`` is set — so it can never produce a
false "pass" against an unconfigured vendor.

Usage (from repo root, with the vendor env configured):

    export FASTAG_ULIP_URL="https://<provider-host>/api"
    export ULIP_API_KEY="<key>"
    export FASTAG_ULIP_BALANCE_PATH="/..."          # per provider spec
    export FASTAG_ULIP_TRANSACTION_PATH="/..."
    export FASTAG_ULIP_ENROUTE_PATH="/..."
    export FASTAG_ULIP_AUTH_SCHEME="bearer|apikey|none"

    PYTHONPATH="shared:." python -m services.fastag.validation.live_validation \
        --valid-rc MH12AB1234 \
        --unknown-rc MH01ZZ9999 \
        --source-state Maharashtra --source-name "Nhava Sheva" \
        --dest-state Maharashtra --dest-name Pune --vehicle-type TRUCK \
        --report ./fastag_live_report.json

Scenarios that a live vendor cannot be forced to emit on demand (empty / partial
/ 5xx) are attempted and, if not reproduced, reported as PENDING with guidance —
never faked. Use the provider's sandbox toggles or a fault-injection proxy to
drive those, then re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Optional

import httpx

# Import the REAL client — the same one the gateway uses. No mock transport.
from services.fastag.ulip_client import UlipFastagClient, UlipClientError, CORRELATION_HEADER


class _Telemetry:
    """httpx event hooks that count attempts per correlation-id (real retry count)."""

    def __init__(self) -> None:
        self.attempts: dict[str, int] = defaultdict(int)

    async def on_request(self, request: httpx.Request) -> None:
        self.attempts[request.headers.get(CORRELATION_HEADER, "")] += 1


def _new_cid() -> str:
    return str(uuid.uuid4())


def _make_client(*, api_key: Optional[str] = None, timeout_s: Optional[float] = None,
                 telem: Optional[_Telemetry] = None) -> UlipFastagClient:
    """Build a REAL client from env, optionally overriding key/timeout for a scenario."""
    hooks = {"request": [telem.on_request]} if telem else None
    inner = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s if timeout_s is not None
                              else float(os.environ.get("FASTAG_ULIP_TIMEOUT_S", 10))),
        event_hooks=hooks or {},
    )
    c = UlipFastagClient.from_env(client=inner)
    if api_key is not None:            # scenario override (e.g. auth-failure)
        c._api_key = api_key
    if timeout_s is not None:
        c._timeout = httpx.Timeout(timeout_s)
    return c


async def _run_call(coro, *, cid: str, endpoint: str, telem: _Telemetry) -> dict:
    """Await one client call; return a normalised result record (never raises)."""
    t0 = perf_counter()
    rec: dict[str, Any] = {"correlation_id": cid, "endpoint": endpoint}
    try:
        raw = await coro
        rec.update(outcome="SUCCESS", http_status=200,
                   response_keys=sorted(list(raw.keys())) if isinstance(raw, dict) else None)
    except UlipClientError as exc:
        rec.update(outcome="ULIP_ERROR", category=exc.category,
                   http_status=exc.status, reason=exc.reason)
    except Exception as exc:  # noqa: BLE001 — harness must never crash mid-matrix
        rec.update(outcome="UNEXPECTED", error=f"{type(exc).__name__}: {exc}")
    rec["latency_ms"] = round((perf_counter() - t0) * 1000, 1)
    rec["attempts"] = telem.attempts.get(cid, 0)
    return rec


async def run_matrix(args: argparse.Namespace) -> dict:
    if not os.environ.get("FASTAG_ULIP_URL", "").strip():
        raise SystemExit(
            "FASTAG_ULIP_URL is not set — refusing to run. Configure the authorised "
            "provider (FASTAG_ULIP_URL, paths, ULIP_API_KEY, auth scheme) first."
        )

    results: list[dict] = []
    enroute_payload = {
        "clientId": _new_cid(),
        "sourceState": args.source_state, "sourceName": args.source_name,
        "destinationState": args.dest_state, "destinationName": args.dest_name,
        "vehicleType": args.vehicle_type,
    }

    # --- Scenarios that CAN be driven deterministically against a real vendor ---
    telem = _Telemetry()
    async with _make_client(telem=telem) as c:
        # 1) Successful requests (happy path) for all three APIs.
        for name, endpoint, mk in (
            ("success_balance", "balance", lambda cid: c.balance(args.valid_rc, client_id=cid)),
            ("success_transactions", "transactions", lambda cid: c.transactions(args.valid_rc, client_id=cid)),
            ("success_enroute", "enroute", lambda cid: c.toll_enroute(enroute_payload, client_id=cid)),
        ):
            cid = _new_cid()
            r = await _run_call(mk(cid), cid=cid, endpoint=endpoint, telem=telem)
            r["scenario"] = name
            results.append(r)

        # 4) Invalid RC / 5) Invalid FASTag — a well-formed but unknown RC; record
        #    whatever the vendor returns (4xx / empty / domain error).
        cid = _new_cid()
        r = await _run_call(c.balance(args.unknown_rc, client_id=cid),
                            cid=cid, endpoint="balance", telem=telem)
        r["scenario"] = "invalid_rc_or_fastag"
        r["note"] = ("well-formed but unknown RC; classify the vendor's response "
                     "against its spec (invalid-RC vs no-tag).")
        results.append(r)

    # 6) Authentication failure — deliberately wrong credential.
    telem_auth = _Telemetry()
    async with _make_client(api_key="INVALID-KEY-FOR-AUTH-TEST", telem=telem_auth) as c:
        cid = _new_cid()
        r = await _run_call(c.balance(args.valid_rc, client_id=cid),
                            cid=cid, endpoint="balance", telem=telem_auth)
        r["scenario"] = "auth_failure"
        r["note"] = "expect vendor 401/403 -> category=http_error (no retry on 4xx)."
        results.append(r)

    # 7) Timeout + retry behaviour — sub-millisecond timeout forces TimeoutException,
    #    which is retryable, so attempts should equal retries+1 and category=timeout.
    telem_to = _Telemetry()
    async with _make_client(timeout_s=0.001, telem=telem_to) as c:
        cid = _new_cid()
        r = await _run_call(c.balance(args.valid_rc, client_id=cid),
                            cid=cid, endpoint="balance", telem=telem_to)
        r["scenario"] = "timeout_and_retry"
        r["note"] = ("expect category=timeout and attempts=FASTAG_ULIP_RETRIES+1 "
                     "(retry/backoff observed).")
        results.append(r)

    # --- Scenarios that need a vendor sandbox / fault-injection to reproduce ---
    for name, guidance in (
        ("empty_response",
         "Point at a sandbox endpoint that returns HTTP 200 with an empty/{} body; "
         "the mapper must yield status=success with empty arrays (no crash)."),
        ("partial_response",
         "Use a fixture omitting optional fields; verify unmapped_fields logging + "
         "null-safe persistence."),
        ("vendor_4xx",
         "Trigger a 400/404 from the provider (bad params); expect category=http_error "
         "-> gateway 502, no retry."),
        ("vendor_5xx",
         "Trigger a 500/503 (or via a fault-injection proxy); expect retries then "
         "category=unavailable -> gateway 502."),
    ):
        results.append({"scenario": name, "outcome": "PENDING_VENDOR_FIXTURE",
                        "note": guidance})

    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "vendor_base_url": os.environ.get("FASTAG_ULIP_URL"),
        "auth_scheme": os.environ.get("FASTAG_ULIP_AUTH_SCHEME", "bearer"),
        "retries_configured": int(os.environ.get("FASTAG_ULIP_RETRIES", 2)),
        "scenarios": results,
    }
    return report


def _print_table(report: dict) -> None:
    cols = ("scenario", "outcome", "category", "http_status", "latency_ms", "attempts")
    widths = {c: len(c) for c in cols}
    for r in report["scenarios"]:
        for c in cols:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    print("\n" + line)
    print("  ".join("-" * widths[c] for c in cols))
    for r in report["scenarios"]:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    print(f"\nvendor: {report['vendor_base_url']}  auth: {report['auth_scheme']}  "
          f"retries: {report['retries_configured']}  at: {report['generated_at']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="FASTag live vendor validation harness")
    ap.add_argument("--valid-rc", required=True, help="a real, enrolled RC number")
    ap.add_argument("--unknown-rc", required=True, help="well-formed but unknown RC")
    ap.add_argument("--source-state", default="Maharashtra")
    ap.add_argument("--source-name", default="Nhava Sheva")
    ap.add_argument("--dest-state", default="Maharashtra")
    ap.add_argument("--dest-name", default="Pune")
    ap.add_argument("--vehicle-type", default="TRUCK")
    ap.add_argument("--report", default="fastag_live_report.json")
    args = ap.parse_args()

    report = asyncio.run(run_matrix(args))
    _print_table(report)
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nreport written -> {args.report}")


if __name__ == "__main__":
    main()
