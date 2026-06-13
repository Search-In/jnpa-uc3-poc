#!/usr/bin/env python3
"""End-to-end smoke test for the JNPA UC-III PoC (Prompt 12, Deliverable 1).

Assumes the whole stack is already up (``make up``). It waits ~60 s for steady
state, then sequentially verifies the full causal chain an evaluator cares about:

  a) ANPR ingestion is emitting at least 5 events/s   (anpr-ingest /metrics rate)
  b) Vahan adapter chain serves a known plate via LIVE_FALLBACK (gateway)
  c) RFID + ANPR correlator emits vehicle.confirmed   (Kafka topic)
  d) Congestion forecaster /metrics shows F1 >= 0.85
  e) ANPR /eval reports OCR_TARGET_MET = true
  f) Each of TFC-1, TFC-2, TFC-3 runs and resets cleanly

Run two ways:

  * As a script — the evaluator's gate. **Exit code 0 == every assertion passed.**
        make up && sleep 60 && python tests/e2e/test_full_pipeline.py
        # add --no-wait to skip the 60s steady-state wait on a warm stack

  * Under pytest — each step is its own test; the whole module is skipped (not
    failed) when the gateway is not reachable, so `make test` stays green
    without the stack.
        make up && pytest tests/e2e/test_full_pipeline.py
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

# Host-published ports (docker-compose.yml). Containers use the jnpa network
# names; from the host everything is localhost:<published port>.
GATEWAY = "http://localhost:8000"
ANPR_AI = "http://localhost:8301"
ANPR_INGEST_METRICS = "http://localhost:9108/metrics"
CONGESTION = "http://localhost:8311"
ANOMALY = "http://localhost:8321"
SCENARIOS = "http://localhost:8400"
KAFKA_BOOTSTRAP = "localhost:29092"  # EXTERNAL listener (host-reachable)

# Steady-state warm-up before asserting. Override with --no-wait / --wait N.
STEADY_STATE_S = 60

# The known plate the demo queries (data/fixtures/known_plates.json[0]).
KNOWN_PLATE = "MH04AB1234"  # canonical demo plate; present in the sim's 25k set

# Targets (mirror the README KPI table).
ANPR_MIN_EVENTS_PER_S = 5.0
CONGESTION_MIN_F1 = 0.85


# --------------------------------------------------------------------------- helpers
def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_up() -> bool:
    """True only if the *gateway* answers on :8000 — not just any TCP listener.

    A bare socket probe gives false positives when an unrelated process (an SSH
    tunnel, another dev server) happens to hold the port, so we require a real
    HTTP 200 from /healthz before treating the stack as up.
    """
    if not _port_open("localhost", 8000):
        return False
    try:
        return httpx.get(f"{GATEWAY}/healthz", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _get_json(url: str, timeout: float = 10.0, **kw) -> dict:
    r = httpx.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def _scrape_counter(metrics_text: str, metric: str) -> float:
    """Sum every sample of a (possibly labelled) Prometheus counter in an
    exposition payload. e.g. metric='plates_emitted_total'."""
    total = 0.0
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # `plates_emitted_total{camera_id="..."} 123.0`  or  `name 123.0`
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name != metric:
            continue
        try:
            total += float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
    return total


# --------------------------------------------------------------------------- checks
def check_anpr_throughput(window_s: float = 6.0) -> Tuple[bool, str]:
    """(a) anpr-ingest emits >= 5 events/s.

    Scrape `plates_emitted_total` twice `window_s` apart and divide the delta by
    the elapsed time. Robust to the gauge-vs-counter distinction (we use the
    monotonic counter) and to a multi-camera label set (we sum all label sets).
    """
    t0 = time.monotonic()
    c0 = _scrape_counter(httpx.get(ANPR_INGEST_METRICS, timeout=10).text, "plates_emitted_total")
    time.sleep(window_s)
    c1 = _scrape_counter(httpx.get(ANPR_INGEST_METRICS, timeout=10).text, "plates_emitted_total")
    dt = time.monotonic() - t0
    rate = (c1 - c0) / dt if dt > 0 else 0.0
    ok = rate >= ANPR_MIN_EVENTS_PER_S
    return ok, f"{rate:.2f} events/s over {dt:.1f}s (target >= {ANPR_MIN_EVENTS_PER_S})"


def check_vahan_live_fallback() -> Tuple[bool, str]:
    """(b) The Vahan chain serves a known plate via LIVE_FALLBACK.

    With no SUREPASS_API_TOKEN the orchestrator skips LIVE_PRIMARY and serves the
    sim as LIVE_FALLBACK. (CACHED is acceptable on a re-run — the first lookup of
    a fresh plate is the fallback; a warm cache is also a valid served path.)
    """
    data = _get_json(f"{GATEWAY}/api/vahan/rc/{KNOWN_PLATE}", timeout=15)
    path = data.get("decision_path")
    rec = data.get("record") or {}
    served = bool(rec)
    ok = served and path in {"LIVE_FALLBACK", "CACHED"}
    return ok, f"decision_path={path}, record_served={served}"


def check_vehicle_confirmed() -> Tuple[bool, str]:
    """(c) The RFID + ANPR correlator emits vehicle.confirmed on Kafka.

    Tail the `vehicle.confirmed` topic from the latest offset (or scan recent)
    for up to ~45 s. The correlator joins rfid.reads x anpr.reads in a 5 s
    window; with both feeds live a confirmation lands within a minute. If
    confluent_kafka is unavailable on the host we degrade to inspecting the
    correlator's emitted-confirmation Prometheus counter (host :9114).
    """
    try:
        from confluent_kafka import Consumer  # type: ignore
    except Exception:  # noqa: BLE001 - no kafka client on host -> metric fallback
        return _vehicle_confirmed_via_metric()

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": f"e2e-confirmed-{int(time.time())}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    try:
        consumer.subscribe(["vehicle.confirmed"])
        deadline = time.time() + 45
        while time.time() < deadline:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                rec = json.loads(msg.value())
            except Exception:  # noqa: BLE001
                continue
            if rec.get("plate"):
                return True, f"vehicle.confirmed plate={rec.get('plate')} gate={rec.get('gate_id')}"
        return False, "no vehicle.confirmed within 45s (is rfid-correlator running?)"
    finally:
        consumer.close()


def _vehicle_confirmed_via_metric() -> Tuple[bool, str]:
    """Fallback: the correlator exposes `vehicle_confirmed_total` on host :9114."""
    try:
        txt = httpx.get("http://localhost:9114/metrics", timeout=10).text
    except Exception as exc:  # noqa: BLE001
        return False, f"no kafka client and correlator /metrics unreachable: {exc!r}"
    n = _scrape_counter(txt, "vehicle_confirmed_total")
    return n > 0, f"vehicle_confirmed_total={n:.0f} (via correlator /metrics)"


def check_congestion_f1() -> Tuple[bool, str]:
    """(d) Congestion forecaster /metrics reports F1 >= 0.85."""
    m = _get_json(f"{CONGESTION}/metrics", timeout=20)
    f1 = m.get("congestion_onset_f1")
    target_met = m.get("TARGET_MET")
    ok = isinstance(f1, (int, float)) and f1 >= CONGESTION_MIN_F1
    return ok, f"congestion_onset_f1={f1} TARGET_MET={target_met} (target >= {CONGESTION_MIN_F1})"


def check_anpr_ocr_target() -> Tuple[bool, str]:
    """(e) ANPR /eval reports OCR_TARGET_MET == true."""
    m = _get_json(f"{ANPR_AI}/eval", timeout=180)  # /eval runs the benchmark; allow time
    met = m.get("OCR_TARGET_MET")
    combined = m.get("combined_weighted_accuracy_pct")
    engine = m.get("engine")
    by = {s.get("name"): s.get("exact_match") for s in m.get("slices", [])}
    detail = (
        f"OCR_TARGET_MET={met} combined={combined}% engine={engine} "
        f"clean={by.get('clean')} dust_haze={by.get('dust_haze')} night={by.get('night')}"
    )
    return bool(met) is True, detail


def check_scenarios_run_and_reset() -> Tuple[bool, str]:
    """(f) Each of TFC-1, TFC-2, TFC-3 runs and resets cleanly."""
    cases = [
        ("tfc1", {"gate_id": "G-NSICT", "duration_minutes": 120}),
        ("tfc2", {"camera_id": "C-KARAL-EXIT"}),
        ("tfc3", {"dpd_release_spike": 2.5}),
    ]
    results: List[str] = []
    all_ok = True
    with httpx.Client(timeout=90.0) as c:
        for name, params in cases:
            ok, note = _run_one_scenario(c, name, params)
            all_ok = all_ok and ok
            results.append(f"{name}:{'ok' if ok else 'FAIL'}({note})")
    return all_ok, " ".join(results)


def _run_one_scenario(c: httpx.Client, name: str, params: dict) -> Tuple[bool, str]:
    r = c.post(f"{SCENARIOS}/scenarios/{name}/run", json=params)
    if r.status_code != 200:
        return False, f"run HTTP {r.status_code}"
    handle_id = r.json().get("handle_id")
    if not handle_id:
        return False, "no handle_id"

    # Poll the timeline until the 5 dashboard steps have fired.
    steps: list = []
    for _ in range(30):
        tl = c.get(f"{SCENARIOS}/scenarios/{handle_id}/timeline")
        if tl.status_code == 200:
            steps = tl.json().get("steps", [])
            if len(steps) >= 5:
                break
        time.sleep(1.0)
    if len(steps) < 5:
        return False, f"only {len(steps)} steps"

    rr = c.post(f"{SCENARIOS}/scenarios/{name}/reset", json={"handle_id": handle_id})
    if rr.status_code != 200 or rr.json().get("ok") is not True:
        return False, f"reset HTTP {rr.status_code}"
    return True, f"{len(steps)} steps, reset ok"


# --------------------------------------------------------------------------- runner
ORDERED_CHECKS: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("a) ANPR ingestion >= 5 events/s", check_anpr_throughput),
    ("b) Vahan chain serves known plate via LIVE_FALLBACK", check_vahan_live_fallback),
    ("c) RFID+ANPR correlator emits vehicle.confirmed", check_vehicle_confirmed),
    ("d) Congestion /metrics F1 >= 0.85", check_congestion_f1),
    ("e) ANPR /eval OCR_TARGET_MET == true", check_anpr_ocr_target),
    ("f) TFC-1/2/3 run + reset cleanly", check_scenarios_run_and_reset),
]


def wait_for_steady_state(seconds: int) -> None:
    if seconds <= 0:
        return
    print(f"Waiting {seconds}s for steady state…", flush=True)
    # Gentle progress so an evaluator knows it's not hung.
    step = max(1, seconds // 6)
    waited = 0
    while waited < seconds:
        time.sleep(min(step, seconds - waited))
        waited += step
        print(f"  …{waited}/{seconds}s", flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JNPA UC-III end-to-end smoke test")
    ap.add_argument("--no-wait", action="store_true",
                    help="skip the 60s steady-state wait (warm stack)")
    ap.add_argument("--wait", type=int, default=None,
                    help="override the steady-state wait (seconds)")
    args = ap.parse_args(argv)

    print("JNPA UC-III — end-to-end smoke test (Prompt 12)\n")
    if not gateway_up():
        print("✗ FATAL: gateway not reachable on localhost:8000 — run `make up` first.",
              file=sys.stderr)
        return 1

    wait_s = 0 if args.no_wait else (args.wait if args.wait is not None else STEADY_STATE_S)
    wait_for_steady_state(wait_s)

    rows: List[Tuple[str, bool, str]] = []
    print("\nRunning checks:")
    for label, fn in ORDERED_CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - any failure is a check failure
            ok, detail = False, f"raised {exc!r}"
        rows.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} — {detail}", flush=True)

    width = max(len(n) for n, _, _ in rows)
    print("\n" + "=" * (width + 12))
    for name, ok, _ in rows:
        print(f"{name.ljust(width)}   {'PASS' if ok else 'FAIL'}")
    print("=" * (width + 12))

    all_ok = all(ok for _, ok, _ in rows)
    if all_ok:
        print("\nE2E OK — all assertions passed.")
        return 0
    failed = [n for n, ok, _ in rows if not ok]
    print(f"\nE2E FAILED — {len(failed)} check(s) failed: {', '.join(failed)}")
    return 1


# --------------------------------------------------------------------------- pytest
# Under pytest, expose each check as its own test, skipped when the stack is down.
import pytest  # noqa: E402

_pytest_gate = pytest.mark.skipif(
    not gateway_up(),
    reason="gateway not reachable on localhost:8000; run `make up` first.",
)


@pytest.fixture(scope="module", autouse=True)
def _warm():
    # Under pytest we assume a warm stack (CI brings it up before invoking).
    yield


@_pytest_gate
def test_anpr_throughput():
    ok, detail = check_anpr_throughput()
    assert ok, detail


@_pytest_gate
def test_vahan_live_fallback():
    ok, detail = check_vahan_live_fallback()
    assert ok, detail


@_pytest_gate
def test_vehicle_confirmed():
    ok, detail = check_vehicle_confirmed()
    assert ok, detail


@_pytest_gate
def test_congestion_f1():
    ok, detail = check_congestion_f1()
    assert ok, detail


@_pytest_gate
def test_anpr_ocr_target():
    ok, detail = check_anpr_ocr_target()
    assert ok, detail


@_pytest_gate
def test_scenarios_run_and_reset():
    ok, detail = check_scenarios_run_and_reset()
    assert ok, detail


if __name__ == "__main__":  # pragma: no cover - evaluator gate
    sys.exit(main())
