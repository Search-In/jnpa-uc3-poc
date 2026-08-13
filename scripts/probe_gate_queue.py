#!/usr/bin/env python3
"""Probe the Driver-Advisory gate-queue path and print one line per sample.

Reproduces the EXACT production request the Driver-Advisory queue makes
(``GET /api/trucks?limit=500&state=AT_GATE_QUEUE``) N times and, optionally, the
truck-sim call behind it (``GET /devices/list?limit=500&state=AT_GATE_QUEUE``),
so an intermittent queue can be attributed to a layer instead of guessed at.

The point is the DISTRIBUTION, not any single call: a gateway 200 with
``degraded=true`` means the truck-sim probe missed its 4 s budget, and the
latency column is what tells you whether the sim is slow or unreachable.

    # gateway path, 20 samples, 2 s apart (the Advisory's own cadence)
    python scripts/probe_gate_queue.py --gateway http://localhost:8080 -n 20

    # both layers, so a gateway miss can be blamed on the sim or not
    python scripts/probe_gate_queue.py --gateway http://localhost:8080 \
        --truck-sim http://localhost:8240 -n 20 --interval 2

Exit status is 1 when any sample failed to produce a live, state-filtered
answer, so this can gate a smoke test.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any, Dict, Optional

import httpx

STATE = "AT_GATE_QUEUE"
LIMIT = 500


async def _sample(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, params=params)
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return {
            "status": resp.status_code,
            "ms": ms,
            "count": body.get("count"),
            "degraded": body.get("degraded"),
            "source": body.get("source"),
            "decision_path": body.get("decision_path"),
            "state_filter_supported": body.get("state_filter_supported"),
            "filter_state": body.get("filter_state"),
            "hint": body.get("hint"),
        }
    except Exception as exc:  # noqa: BLE001 — a probe reports failures, never raises
        return {"status": None, "ms": (time.perf_counter() - t0) * 1000,
                "error": f"{type(exc).__name__}: {exc}"}


def _fmt(i: int, label: str, s: Dict[str, Any]) -> str:
    if s.get("error"):
        return f"#{i:02d} {label:8s} ERROR   {s['ms']:7.0f}ms  {s['error']}"
    bits = [f"#{i:02d} {label:8s} {s['status']:<3}  {s['ms']:7.0f}ms",
            f"count={s['count']}"]
    if s.get("degraded") is not None:
        bits.append(f"degraded={str(s['degraded']).lower()}")
    if s.get("source"):
        bits.append(f"source={s['source']}")
    if s.get("decision_path"):
        bits.append(f"path={s['decision_path']}")
    if s.get("state_filter_supported") is False:
        bits.append("state_filter_supported=false")
    if s.get("hint"):
        bits.append(f"hint={s['hint']!r}")
    return "  ".join(bits)


def _healthy(s: Dict[str, Any]) -> bool:
    """A sample that actually answered the question that was asked."""
    return (s.get("status") == 200 and not s.get("degraded")
            and s.get("state_filter_supported") is not False)


async def main(args: argparse.Namespace) -> int:
    gw_ok = sim_ok = 0
    gw_samples: list[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        for i in range(1, args.n + 1):
            if args.gateway:
                s = await _sample(client, args.gateway.rstrip("/") + "/api/trucks",
                                  {"limit": LIMIT, "state": STATE})
                gw_samples.append(s)
                gw_ok += int(_healthy(s))
                print(_fmt(i, "gateway", s), flush=True)
            if args.truck_sim:
                s = await _sample(client, args.truck_sim.rstrip("/") + "/devices/list",
                                  {"limit": LIMIT, "state": STATE})
                sim_ok += int(s.get("status") == 200)
                print(_fmt(i, "trucksim", s), flush=True)
            if i < args.n:
                await asyncio.sleep(args.interval)

    print()
    if args.gateway:
        lat = sorted(s["ms"] for s in gw_samples)
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0
        print(f"gateway : {gw_ok}/{args.n} live state-filtered answers  "
              f"(median {lat[len(lat)//2]:.0f} ms, p95 {p95:.0f} ms)")
        counts = {s.get("count") for s in gw_samples if s.get("status") == 200}
        print(f"          counts observed: {sorted(c for c in counts if c is not None)}")
    if args.truck_sim:
        print(f"trucksim: {sim_ok}/{args.n} HTTP 200")
    return 0 if (not args.gateway or gw_ok == args.n) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", help="gateway base URL, e.g. http://localhost:8080")
    ap.add_argument("--truck-sim", help="truck-sim base URL, e.g. http://localhost:8240")
    ap.add_argument("-n", type=int, default=20, help="samples (default 20)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between samples (default 2.0)")
    a = ap.parse_args()
    if not a.gateway and not a.truck_sim:
        ap.error("give --gateway and/or --truck-sim")
    raise SystemExit(asyncio.run(main(a)))
