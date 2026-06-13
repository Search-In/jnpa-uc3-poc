#!/usr/bin/env python3
"""Demo driver — walk an operator through the on-screen JNPA UC-III demo
(Prompt 12, Deliverable 2).

A pretty CLI that narrates each step, auto-posts the TFC-1/2/3 scenario triggers
at the right moment, and (with ``--record``) takes a timestamp-stamped Playwright
screenshot of the relevant screen into ``./evidence/screenshots/{step}.png``.

Flow:
  0. Preflight sanity checks (refuse to launch if a prerequisite is missing).
  1. "Open http://localhost:3000/live — confirm map is live"
  2. "Now triggering TFC-1 in 5 s…"  -> auto-POST tfc1, screenshot the PWA reroute
  3. "Watch trucking-app re-route at http://localhost:3000/pwa"
  …and the same for TFC-2 (wrong-way + e-Challan) and TFC-3 (cargo surge).
  N. Build the evidence pack (metrics.json + POC_SUMMARY.md + Jaeger traces).

Usage:
    python scripts/demo_drive.py            # interactive walk-through
    python scripts/demo_drive.py --record   # + screenshots + evidence pack
    python scripts/demo_drive.py --record --yes   # non-interactive (CI / dry demo)

``--record`` is the evaluator-visit mode invoked by the verification command:
    make up && sleep 60 && python scripts/demo_drive.py --record
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from scripts.preflight import ensure_ready_or_exit  # noqa: E402

# Host endpoints (docker-compose published ports).
WEB = "http://localhost:3000"
GATEWAY = "http://localhost:8000"
SCENARIOS = "http://localhost:8400"

EVIDENCE_DIR = REPO_ROOT / "evidence"
SHOTS_DIR = EVIDENCE_DIR / "screenshots"
SCREENSHOT_JS = REPO_ROOT / "scripts" / "_screenshot.mjs"
# Playwright lives in the dashboard's node_modules (dev dep) — reuse it.
WEB_NODE_MODULES = REPO_ROOT / "web" / "node_modules"


# --------------------------------------------------------------------------- ANSI
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"

    @staticmethod
    def off() -> None:
        for k in ("RESET", "BOLD", "DIM", "CYAN", "GREEN", "YELLOW", "RED", "BLUE", "MAGENTA"):
            setattr(C, k, "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rule(title: str) -> None:
    bar = "─" * max(8, 72 - len(title) - 4)
    print(f"\n{C.CYAN}{C.BOLD}── {title} {bar}{C.RESET}")


def say(msg: str) -> None:
    print(f"   {msg}")


def action(msg: str) -> None:
    print(f"   {C.MAGENTA}▸{C.RESET} {msg}")


def ok(msg: str) -> None:
    print(f"   {C.GREEN}✓{C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"   {C.YELLOW}!{C.RESET} {msg}")


def watch(url: str, msg: str) -> None:
    print(f"   {C.BLUE}👁  {msg}{C.RESET}  {C.DIM}{url}{C.RESET}")


# --------------------------------------------------------------------------- step model
@dataclass
class Step:
    key: str                       # screenshot filename stem
    title: str
    url: str                       # screen to open + screenshot
    instruction: str
    countdown_s: int = 0           # "triggering in N s…" pause
    trigger: Optional[Callable[["DemoState"], None]] = None
    watch_msg: str = ""


@dataclass
class DemoState:
    record: bool
    interactive: bool
    client: httpx.Client
    shots: List[Path] = field(default_factory=list)
    triggers: dict = field(default_factory=dict)   # scenario name -> handle_id


# --------------------------------------------------------------------------- triggers
def _post_scenario(state: DemoState, name: str, params: dict) -> None:
    """POST a scenario to the runner and remember the handle for the reset script."""
    try:
        r = state.client.post(f"{SCENARIOS}/scenarios/{name}/run", json=params, timeout=90)
        r.raise_for_status()
        body = r.json()
        hid = body.get("handle_id")
        state.triggers[name] = hid
        ok(f"{name.upper()} launched — handle_id={hid} steps={body.get('steps')} "
           f"trace_id={body.get('trace_id')}")
    except Exception as exc:  # noqa: BLE001 - the demo continues; operator sees the warn
        warn(f"{name.upper()} trigger failed: {exc!r} (is the scenarios-runner up on :8400?)")


def trigger_tfc1(state: DemoState) -> None:
    _post_scenario(state, "tfc1", {"gate_id": "G-NSICT", "duration_minutes": 120})


def trigger_tfc2(state: DemoState) -> None:
    _post_scenario(state, "tfc2", {"camera_id": "C-KARAL-EXIT"})


def trigger_tfc3(state: DemoState) -> None:
    _post_scenario(state, "tfc3", {"dpd_release_spike": 2.5})


# --------------------------------------------------------------------------- steps
def build_steps() -> List[Step]:
    return [
        Step(
            key="01_live_map",
            title="Control room — live map",
            url=f"{WEB}/live",
            instruction="Open http://localhost:3000/live — confirm the corridor "
                        "map is live (gates, truck positions, congestion overlay).",
            watch_msg="Dashboard /live",
        ),
        Step(
            key="02_tfc1_gate_closure",
            title="TFC-1 — gate closure -> re-route",
            url=f"{WEB}/pwa?device=DEV-000001",
            instruction="Triggering TFC-1 (G-NSICT closure). The trucking-app PWA "
                        "should paint a full-screen re-route advisory within ~5 s.",
            countdown_s=5,
            trigger=trigger_tfc1,
            watch_msg="Trucking-app re-route /pwa",
        ),
        Step(
            key="03_tfc2_wrongway",
            title="TFC-2 — wrong-way -> e-Challan",
            url=f"{WEB}/live",
            instruction="Triggering TFC-2 (wrong-way at C-KARAL-EXIT). Watch the "
                        "WRONG_WAY alert + auto e-Challan land on the dashboard.",
            countdown_s=5,
            trigger=trigger_tfc2,
            watch_msg="Dashboard alert lane /live",
        ),
        Step(
            key="04_tfc3_cargo_surge",
            title="TFC-3 — cargo surge (cross-twin)",
            url=f"{WEB}/whatif",
            instruction="Triggering TFC-3 (2.5x DPD release -> 600 trucks/h). Watch "
                        "the congestion forecast + spillover re-routing react.",
            countdown_s=5,
            trigger=trigger_tfc3,
            watch_msg="What-If view /whatif",
        ),
        Step(
            key="05_decision_evidence",
            title="Fallback decision evidence",
            url=f"{GATEWAY}/api/debug/decisions",
            instruction="Show the gateway decision log (last 1000 fallback "
                        "decisions) — the auditable evidence trail.",
            watch_msg="Decision ring buffer /api/debug/decisions",
        ),
    ]


# --------------------------------------------------------------------------- screenshot
def _screenshot_available() -> Optional[str]:
    """Return the node binary if Playwright is usable, else None (degrade)."""
    node = shutil.which("node")
    if not node:
        return None
    if not SCREENSHOT_JS.is_file() or not WEB_NODE_MODULES.is_dir():
        return None
    return node


def take_screenshot(node: str, url: str, out: Path, caption: str) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [node, str(SCREENSHOT_JS), url, str(out), caption, _now_iso()],
            cwd=str(REPO_ROOT / "web"),  # so `import 'playwright'` resolves
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        warn(f"screenshot subprocess failed: {exc!r}")
        return False
    if proc.returncode != 0:
        warn(f"screenshot skipped ({url}): {proc.stderr.strip().splitlines()[-1:] or proc.stdout!r}")
        return False
    return out.is_file()


# --------------------------------------------------------------------------- pause
def pause(state: DemoState, prompt: str = "Press Enter to continue…") -> None:
    if state.interactive:
        try:
            input(f"   {C.DIM}{prompt}{C.RESET} ")
        except EOFError:
            pass
    else:
        time.sleep(1.0)


def countdown(seconds: int, what: str) -> None:
    for n in range(seconds, 0, -1):
        print(f"\r   {C.YELLOW}⏱  Now triggering {what} in {n} s… {C.RESET}", end="", flush=True)
        time.sleep(1.0)
    print(f"\r   {C.YELLOW}⏱  Triggering {what} now.{' ' * 20}{C.RESET}")


# --------------------------------------------------------------------------- run
def run_demo(state: DemoState) -> None:
    node = _screenshot_available() if state.record else None
    if state.record and not node:
        warn("Playwright/node not available — running without screenshots. "
             "Run `cd web && npm install` to enable them.")

    steps = build_steps()
    for i, step in enumerate(steps, 1):
        rule(f"Step {i}/{len(steps)} · {step.title}")
        say(step.instruction)
        watch(step.url, step.watch_msg or "Open")

        if step.countdown_s and step.trigger is not None:
            pause(state, "Press Enter when the operator is watching the screen…")
            countdown(step.countdown_s, step.title.split("—")[0].strip())
            action(f"POST scenario trigger ({step.title.split('—')[0].strip()})")
            step.trigger(state)
            # Give the chain a moment to paint before the screenshot.
            time.sleep(4.0)
        else:
            pause(state)

        if state.record and node:
            shot = SHOTS_DIR / f"{step.key}.png"
            if take_screenshot(node, step.url, shot, f"{step.title} — {step.url}"):
                state.shots.append(shot)
                ok(f"screenshot -> evidence/screenshots/{shot.name}")


# --------------------------------------------------------------------------- evidence
def build_evidence_pack(state: DemoState) -> int:
    rule("Building evidence pack")
    say("metrics.json · Jaeger traces · POC_SUMMARY.md -> ./evidence/")
    trace_ids = [h for h in state.triggers.values() if h]
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "build_evidence.py")]
    for hid in state.triggers.values():
        if hid:
            cmd += ["--handle", str(hid)]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
        return proc.returncode
    except Exception as exc:  # noqa: BLE001
        warn(f"evidence builder failed: {exc!r}")
        return 1


# --------------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JNPA UC-III demo driver")
    ap.add_argument("--record", action="store_true",
                    help="capture screenshots + build the evidence pack")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="non-interactive (no Enter prompts; auto-advance)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the hard-coded sanity checks (not recommended)")
    args = ap.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        C.off()

    print(f"{C.BOLD}JNPA Digital Twin — UC-III PoC · demo driver{C.RESET}")
    print(f"{C.DIM}Traffic Monitoring & Vehicular Decongestion · NH-348 corridor{C.RESET}\n")

    # Deliverable 4: refuse to launch if a prerequisite is missing.
    if not args.skip_preflight:
        ensure_ready_or_exit()

    with httpx.Client(timeout=30.0) as client:
        # Liveness: the gateway must answer or the whole demo is moot.
        try:
            client.get(f"{GATEWAY}/healthz", timeout=5).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"{C.RED}✗ gateway not reachable on :8000 ({exc!r}) — run `make up` "
                  f"first.{C.RESET}", file=sys.stderr)
            return 1

        state = DemoState(record=args.record, interactive=not args.yes, client=client)
        run_demo(state)

        rule("Demo complete")
        ok(f"Triggered scenarios: {', '.join(f'{k}={v}' for k, v in state.triggers.items()) or 'none'}")
        if state.record:
            ok(f"{len(state.shots)} screenshot(s) in evidence/screenshots/")
            rc = build_evidence_pack(state)
            print()
            if rc == 0:
                ok("Evidence pack ready -> ./evidence/  (open evidence/POC_SUMMARY.md)")
            else:
                warn("Evidence pack build reported a non-zero exit — see output above.")
            print(f"\n   Reset the stack after the evaluator leaves with: "
                  f"{C.BOLD}make demo-reset{C.RESET}")
            return rc

    print(f"\n   Re-run with {C.BOLD}--record{C.RESET} to capture the evidence pack.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
