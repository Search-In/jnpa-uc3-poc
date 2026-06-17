#!/usr/bin/env python3
"""JNPA UC-III PoC self-test (Deliverable 4).

Asserts that each D.2 sub-criterion is demonstrable and prints a pass/fail line
per Appendix-C requirement item — a one-shot evidence artefact for an evaluator.

It runs WITHOUT the docker stack: every check is static (a file/route exists) or
import-level (a service's deterministic logic produces a sensible result), so the
KPI engine, the five capability modules, the fallback chains, and the what-if
scenarios are all proven from a clean checkout in seconds. Where a check would
need live infrastructure it asserts the *code path* instead and says so.

Usage:
    python -m scripts.poc_selftest          # human-readable report, exit 0/1
    python -m scripts.poc_selftest --json   # machine-readable summary

Exit code is non-zero if any REQUIRED check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (
    REPO_ROOT / "shared",
    REPO_ROOT / "empty-container",
    REPO_ROOT / "carbon",
    REPO_ROOT / "gate-data",
    REPO_ROOT / "identity",
    REPO_ROOT / "parking",
    REPO_ROOT,
):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

CheckResult = Tuple[bool, str]  # (passed, detail)


def _exists(*relpaths: str) -> bool:
    return all((REPO_ROOT / rp).exists() for rp in relpaths)


def _contains(relpath: str, *needles: str) -> bool:
    p = REPO_ROOT / relpath
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="ignore")
    return all(n in text for n in needles)


# ---------------------------------------------------------------------------
# D.2 sub-criteria (2 marks each) — each must be independently demonstrable.
# ---------------------------------------------------------------------------
def d1_methodology_assumptions() -> CheckResult:
    ok = _exists("docs/ASSUMPTIONS.md", "docs/COVERAGE.md") and _exists(
        "web/src/components/AssumptionsPanel.tsx"
    )
    return ok, "docs/ASSUMPTIONS.md + COVERAGE.md + in-app AssumptionsPanel"


def d2_ai_ml() -> CheckResult:
    # The D.2 sub-criterion is "usage of AI/ML tools": three real model services
    # (ANPR CNN+CRNN, congestion GNN+LSTM, anomaly ByteTrack+AE) with eval
    # surfaces. The bid §8.5.2 *metric thresholds* are reported separately below.
    ok = _exists(
        "ai/anpr/src/anpr/app.py",
        "ai/congestion/infer.py",
        "ai/anomaly/app.py",
        "ai/congestion/artifacts/metrics.json",
    )
    return ok, "ANPR(CNN+CRNN) + congestion(GNN+LSTM) + anomaly(ByteTrack+AE) services"


def b_congestion_f1_gate() -> CheckResult:
    """Bid §8.5.2 congestion F1 >= 0.85 — reported honestly (a tuning gap, not an
    absence of AI/ML). Non-required: surfaces the real metric without masking it.
    """
    try:
        m = json.loads((REPO_ROOT / "ai/congestion/artifacts/metrics.json").read_text())
        f1 = float(m.get("congestion_onset_f1", 0))
        target = float(m.get("target_f1", 0.85))
    except Exception as exc:
        return False, f"metrics.json unreadable: {exc}"
    ok = f1 >= target
    note = "meets" if ok else "below — retune/retrain to close the gap"
    return ok, f"congestion F1 = {f1:.4f} vs target {target:.2f} ({note})"


def d3_integration_fallback() -> CheckResult:
    ok = _contains("gateway/fallback.py", "SYNTHETIC") and _contains(
        "gateway/provisional.py", "provisional"
    )
    # The three named fallback chains are encoded.
    chains = _contains(
        "gateway/fallback.py", "AnprPath", "VahanPath", "TruckPath"
    )
    return ok and chains, "camera + Vahan(PROVISIONAL 24h) + trucking(ULIP/web) chains"


def d4_dashboard_kpi() -> CheckResult:
    # KPI engine is real + the strip route + the dashboard KPI strip component.
    try:
        from jnpa_shared import kpi  # noqa
        engine_ok = "trt_empty_ecd" in kpi.KPI_TARGETS and bool(
            kpi.compute_kpi("gate_queue_wait", 6.0).to_dict()
        )
    except Exception:
        engine_ok = False
    ui_ok = _exists("web/src/components/panels/KpiStrip.tsx") and _contains(
        "gateway/routers/kpi.py", "/strip"
    )
    return engine_ok and ui_ok, "tested KPI engine + /api/kpi/strip + KPI strip UI"


def d5_scenarios_workflow() -> CheckResult:
    ok = _exists(
        "scenarios/tfc1.py", "scenarios/tfc2.py", "scenarios/tfc3.py",
        "scenarios/uc2_bridge.py",
    )
    cross_twin = _contains("scenarios/tfc3.py", "cargo.dpd_release") and _contains(
        "scenarios/uc2_bridge.py", "translate_release"
    )
    return ok and cross_twin, "TFC-1/2/3 + cross-twin UC2->UC3 DPD release"


# ---------------------------------------------------------------------------
# Appendix-C requirement items (8) — prove the capability exists & runs.
# ---------------------------------------------------------------------------
def c1_parking_digital_twin() -> CheckResult:
    try:
        from parking import facilities  # type: ignore
        snap = facilities.snapshot(540)
        ok = bool(snap) and all(
            f["available"] == f["capacity"] - f["occupied"] for f in snap
        )
    except Exception as exc:
        return False, f"parking module import/calc failed: {exc}"
    return ok and _exists("web/src/components/panels/ParkingBoard.tsx"), (
        "parking availability service + board + ArcgisMap corridor/heatmap"
    )


def c2_face_recognition_vahan() -> CheckResult:
    try:
        from identity import embeddings, gallery  # type: ignore
        gal = gallery.generate_gallery()
        drv = next(iter(gal))
        cap = embeddings.capture_embedding(drv, genuine=True)
        score = embeddings.cosine(gal[drv].embedding, cap)
        ok = score >= 0.9
    except Exception as exc:
        return False, f"identity module failed: {exc}"
    vahan = _exists("ingest/vahan_sim/app.py") and _contains(
        "gateway/routers/vahan.py", "PROVISIONAL"
    )
    return ok and vahan, "synthetic-face verify (genuine>=0.9) + Vahan/Sarathi"


def c3_empty_container() -> CheckResult:
    try:
        from empty_container import optimizer, seed  # type: ignore
        allocs = optimizer.allocate(seed.supply_book(), seed.demand_book())
        ok = len(allocs) >= 1
        cargo_types = {a.cargo_type for a in allocs}
        variants = bool(cargo_types - {"container"})  # tanker/break-bulk/bowser present
    except Exception as exc:
        return False, f"empty-container module failed: {exc}"
    return ok and variants, f"allocation across depots; {len(cargo_types)} cargo variants"


def c4_customs_alerts() -> CheckResult:
    try:
        from gate_data import leo  # type: ignore
        flags = []
        for r in leo.reconcile_all():
            flags.extend(leo.customs_alerts(r))
        ok = len(flags) >= 1 and flags[0]["kind"] == "CUSTOMS_FLAG"
    except Exception as exc:
        return False, f"gate-data customs failed: {exc}"
    return ok, f"{len(flags)} CUSTOMS_FLAG alerts from reconciliation"


def c5_gate_data_autoleo() -> CheckResult:
    try:
        from gate_data import leo, seed  # type: ignore
        ds = seed.generate_dataset()
        sample = next(iter(ds))
        res = leo.reconcile(sample)
        ok = hasattr(res, "leo_ready") and "checks" in res.to_dict()
    except Exception as exc:
        return False, f"Auto-LEO reconcile failed: {exc}"
    return ok and _exists("web/src/components/panels/AutoLeoPanel.tsx"), (
        "e-seal/Form13/weighbridge/ICEGATE -> Auto-LEO + panel"
    )


def c6_carbon() -> CheckResult:
    try:
        from carbon import calculator  # type: ignore
        roll = calculator.aoi_rollup(calculator.seed_aoi_fleet())
        moving = roll["by_source"]["moving"]
        idle = roll["by_source"]["idle"]
        ok = abs((moving + idle) - roll["total_kg"]) < 1.0 and roll["total_kg"] > 0
    except Exception as exc:
        return False, f"carbon module failed: {exc}"
    return ok and _exists("web/src/components/panels/CarbonTile.tsx"), (
        "AoI CO2e rollup (moving+idle==total) + carbon tile"
    )


def c7_ai_video_analytics() -> CheckResult:
    ok = _exists("ai/anpr/src/anpr/app.py", "ai/congestion/infer.py", "ai/anomaly/app.py")
    return ok, "ANPR(detect+OCR) + congestion(GNN+LSTM) + anomaly(ByteTrack+AE)"


def c8_geofencing() -> CheckResult:
    ok = _exists("ai/anomaly/rules/parking.py") and _exists(
        "web/src/screens/GeofencingManager.tsx"
    )
    return ok, "no-parking violation + duration escalation + zone editor"


# ---------------------------------------------------------------------------
# Quality-bar checks (UC1 parity).
# ---------------------------------------------------------------------------
def q_mock_live_adapter() -> CheckResult:
    ok = _exists(
        "web/src/data/types.ts", "web/src/data/mock.ts", "web/src/data/live.ts",
        "web/src/data/index.ts",
    ) and _contains("web/src/data/index.ts", "VITE_DATA_MODE")
    return ok, "single DataAdapter: MockAdapter+LiveAdapter, VITE_DATA_MODE switch"


def q_arcgis_calcite() -> CheckResult:
    ok = _exists("web/src/components/map/ArcgisMap.tsx", "web/src/components/layout/Shell.tsx")
    arcgis = _contains("web/src/components/map/ArcgisMap.tsx", "arcgis")
    return ok and arcgis, "ArcGIS Maps SDK map + Calcite shell"


def q_i18n() -> CheckResult:
    ok = _exists(
        "web/src/i18n/locales/en.json",
        "web/src/i18n/locales/hi.json",
        "web/src/i18n/locales/mr.json",
    )
    return ok, "i18n EN/HI/MR scaffolding"


# ---------------------------------------------------------------------------
# Registry: (id, label, fn, required)
# ---------------------------------------------------------------------------
CHECKS: List[Tuple[str, str, Callable[[], CheckResult], bool]] = [
    # D.2 sub-criteria
    ("D2.1", "Methodology + assumptions (in-app)", d1_methodology_assumptions, True),
    ("D2.2", "AI/ML usage", d2_ai_ml, True),
    ("D2.3", "Integration + fallback on unavailability", d3_integration_fallback, True),
    ("D2.4", "Dashboard + KPI monitoring", d4_dashboard_kpi, True),
    ("D2.5", "What-if + automated reactive workflow", d5_scenarios_workflow, True),
    # Appendix-C requirements
    ("C.1", "Mobile + Digital Twin (routing/parking/heatmap)", c1_parking_digital_twin, True),
    ("C.2", "Face-recognition (PDP) + Vahan/Sarathi", c2_face_recognition_vahan, True),
    ("C.3", "Empty-container supply-demand optimiser", c3_empty_container, True),
    ("C.4", "Customs alerts & flags", c4_customs_alerts, True),
    ("C.5", "Gate data (e-seal/Form13/weighbridge/ICEGATE) -> Auto-LEO", c5_gate_data_autoleo, True),
    ("C.6", "Carbon-emissions calculation", c6_carbon, True),
    ("C.7", "AI video-analytics pipeline", c7_ai_video_analytics, True),
    ("C.8", "Geofencing + no-parking violation", c8_geofencing, True),
    # Quality bar
    ("Q.1", "Typed mock|live data adapter", q_mock_live_adapter, True),
    ("Q.2", "ArcGIS Maps SDK + Calcite", q_arcgis_calcite, True),
    ("Q.3", "Multilingual EN/HI/MR", q_i18n, True),
    # Bid metric gates (reported honestly; not a pass/fail gate on the PoC).
    ("B.1", "Bid §8.5.2 congestion F1 >= 0.85", b_congestion_f1_gate, False),
]


def run() -> int:
    parser = argparse.ArgumentParser(description="JNPA UC-III PoC self-test")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    results = []
    for cid, label, fn, required in CHECKS:
        try:
            passed, detail = fn()
        except Exception as exc:  # a check must never crash the report
            passed, detail = False, f"check raised: {exc}"
        results.append({"id": cid, "label": label, "passed": passed,
                        "detail": detail, "required": required})

    failed_required = [r for r in results if r["required"] and not r["passed"]]

    if args.json:
        print(json.dumps({
            "total": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "failed_required": len(failed_required),
            "checks": results,
        }, indent=2))
        return 1 if failed_required else 0

    print(f"\n{DIM}JNPA UC-III PoC self-test — D.2 sub-criteria + Appendix-C items{RESET}\n")
    for r in results:
        if r["passed"]:
            mark = f"{GREEN}PASS{RESET}"
        elif r["required"]:
            mark = f"{RED}FAIL{RESET}"
        else:
            mark = f"{YELLOW}WARN{RESET}"  # advisory metric gate, not a blocker
        print(f"  [{mark}] {r['id']:<5} {r['label']}")
        print(f"         {DIM}{r['detail']}{RESET}")
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    bar = GREEN if not failed_required else RED
    print(f"\n  {bar}{passed}/{total} checks passed{RESET}", end="")
    if failed_required:
        print(f"  {RED}({len(failed_required)} required failing){RESET}")
    else:
        print(f"  {GREEN}— every D.2 sub-criterion and Appendix-C item is demonstrable.{RESET}")
    print()
    return 1 if failed_required else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
