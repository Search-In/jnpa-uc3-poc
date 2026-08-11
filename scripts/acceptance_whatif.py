#!/usr/bin/env python3
"""JNPA what-if acceptance run.

Every scenario is called with the parameters the Notice ITSELF states — the dates
and percentages are quoted beside each case — and then checked against what that
clause actually asks for, not merely against "did it return 200".

A check fails loudly. The point of this run is to find what is not ready.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/cargo/simulate"


def call(name, body):
    req = urllib.request.Request(
        f"{BASE}/{name}", data=json.dumps(body).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def nonempty(v):
    if v is None:
        return False
    if isinstance(v, (list, dict, str)):
        return len(v) > 0
    return True


# (id, jnpa ref, notice quote, params, [(check name, fn(result)->bool)])
CASES = [
    ("vessel-bunching", "I-A",
     '"On 6 August 2026 ... Propose a berthing order ... State the objective ... '
     'and show what an alternative order would cost against the same objective."',
     {"as_of": "2026-08-06T00:00:00Z", "objective": "waiting_time", "horizon_hours": 24},
     [("proposes a berthing order",
       lambda r: nonempty(r["result"].get("recommended", {}).get("sequence"))),
      ("names the objective on screen",
       lambda r: nonempty(r["result"].get("objective", {}).get("description"))),
      ("costs >=1 alternative on the SAME objective",
       lambda r: len(r["result"].get("alternatives", [])) >= 1
       and all("cost_vs_recommended" in a for a in r["result"]["alternatives"])),
      ("reports terminal imbalance (the Notice's premise)",
       lambda r: nonempty(r["result"].get("load_by_terminal")))]),

    ("berth-cascade", "I-B",
     '"On 2nd August 2026, a vessel\'s operation is overrun by six hours. Identify '
     'which subsequent calls at that terminal are displaced, by how long, and state '
     'the cumulative delay across the berth queue over the following forty-eight hours."',
     {"as_of": "2026-08-02T00:00:00Z", "delay_hours": 6, "horizon_hours": 48},
     [("names WHICH calls are displaced",
       lambda r: nonempty(r["result"].get("displaced_calls"))),
      ("gives each displaced call its own push-out",
       lambda r: all("delay_hours" in c for c in r["result"].get("displaced_calls", []))
       and len(r["result"].get("displaced_calls", [])) > 0),
      ("states the cumulative 48h queue delay",
       lambda r: r["figures"].get("cumulative_delay_hours") is not None),
      ("horizon is 48 hours",
       lambda r: r["result"].get("window", {}).get("hours") == 48
       or r["figures"].get("horizon_hours") in (48, None))]),

    ("modal-shift", "II-A",
     '"Twenty per cent of containers currently evacuated by rail are moved to road '
     'instead for period 1st August 2026 to 3rd August 2026. Determine whether the '
     'gate absorbs the additional load. Present the hourly gate profile before and '
     'after the shift, and identify the first constraint to saturate."',
     {"from_date": "2026-08-01", "to_date": "2026-08-03", "shift_pct": 0.20},
     [("hourly gate profile BEFORE",
       lambda r: nonempty(r["result"].get("baseline_profile"))),
      ("hourly gate profile AFTER",
       lambda r: nonempty(r["result"].get("shifted_profile"))),
      ("yes/no absorption verdict",
       lambda r: isinstance(r["result"].get("gate_absorbs_load"), bool)),
      ("names the FIRST constraint to saturate",
       lambda r: nonempty((r["result"].get("first_constraint") or {}).get("constraint"))
       or r["result"].get("gate_absorbs_load") is True)]),

    ("crane-productivity", "II-B",
     '"Derive the effective crane productivity implied by the data for each vessel '
     'call, expressed as gross moves per hour worked. Model a twenty-five per cent '
     'reduction ... state the effect on turnaround and on the berth queue behind it. '
     'Take up a vessel on 6th August 2026."',
     {"as_of": "2026-08-06T00:00:00Z", "reduction_pct": 0.25, "window_hours": 48},
     [("derives productivity PER VESSEL CALL",
       lambda r: nonempty(r["result"].get("baseline_by_call"))),
      ("expressed as gross moves per hour worked",
       lambda r: any(c.get("moves_per_hour") is not None
                     for c in r["result"].get("baseline_by_call", []))),
      ("effect on TURNAROUND",
       lambda r: r["figures"].get("turnaround_increase_hours") is not None),
      ("effect on the BERTH QUEUE behind it",
       lambda r: "berth_queue_impact" in r["result"]
       and r["figures"].get("cumulative_berth_delay_hours") is not None)]),

    ("gate-slotting", "III-A",
     '"Using vehicle arrival times at the gate, characterise the arrival pattern '
     'across the day and identify the periods in which arrivals exceed the rate the '
     'gate sustains. Propose an appointment or slotting arrangement that flattens '
     'the peak and quantify what it would achieve against the observed pattern."',
     {"from_ts": "2026-08-03T00:00:00Z", "to_ts": "2026-08-04T00:00:00Z"},
     [("characterises the arrival pattern",
       lambda r: nonempty((r["result"].get("arrival_pattern") or {}).get("hourly"))),
      ("identifies periods exceeding the sustained rate",
       lambda r: "saturated_periods" in r["result"]),
      ("proposes a slotting arrangement",
       lambda r: nonempty(r["result"].get("proposed_slots"))),
      ("quantifies it against the observed pattern",
       lambda r: r["figures"].get("peak_reduction_pct") is not None
       and r["figures"].get("observed_peak") is not None)]),

    ("driver-shortage", "III-B",
     '"A shortage reduces the number of trips each vehicle can complete in a day by '
     'one third. Determine the effect on evacuation throughput, and identify which '
     'transporters and which cargo flows are most exposed. Also show how best '
     'evacuation strategy is determined. Consider 1st to 3rd August and show state '
     'on 4th August 2026."',
     {"from_date": "2026-08-01", "to_date": "2026-08-03",
      "state_date": "2026-08-04", "reduction_pct": 0.3333},
     [("effect on evacuation throughput",
       lambda r: r["figures"].get("throughput_loss_pct") is not None),
      ("transporters: ranked, OR attribution declared unavailable",
       lambda r: (nonempty((r["result"].get("exposed_transporters") or {}).get("by_absolute_loss"))
                  and nonempty((r["result"].get("exposed_transporters") or {})
                               .get("by_structural_dependence")))
       or ((r["result"].get("exposed_transporters") or {}).get("attribution_available") is False
           and any(a["field"] == "transporter_attribution" for a in r["assumptions"]))),
      ("which CARGO FLOWS are most exposed",
       lambda r: nonempty(r["result"].get("exposed_cargo_flows"))),
      ("states how the best evacuation strategy is determined",
       lambda r: any(a["field"] == "evacuation_priority_rule" for a in r["assumptions"])),
      ("reports the state on the report date (4 Aug)",
       lambda r: nonempty(r["result"].get("state_on_report_date")))]),

    ("channel-closure", "N-1", "bidder-proposed",
     {"as_of": "2026-08-06T06:00:00Z", "closure_hours": 12},
     [("reports whether berth-lock is reached",
       lambda r: r["figures"].get("berth_lock_reached") is not None),
      ("proposes a sailing order with a costed alternative",
       lambda r: "cost_vs_recommended" in (r["result"].get("sailing_order", {})
                                           .get("alternative", {})))]),

    ("yard-feedback", "N-2", "bidder-proposed",
     {"from_date": "2026-08-01", "to_date": "2026-08-05", "evacuation_drop_pct": 0.5},
     [("reports which regime it is in",
       lambda r: r["figures"].get("regime") in ("CONVERGING", "SATURATING")),
      ("gives a day-by-day trajectory",
       lambda r: nonempty(r["result"].get("with_shortfall")))]),

    ("degraded-gate", "N-3", "bidder-proposed",
     {"from_ts": "2026-08-03T00:00:00Z", "to_ts": "2026-08-04T00:00:00Z",
      "outage_hours": 4, "degraded_fraction": 0.4},
     [("reports the peak queue",
       lambda r: r["figures"].get("peak_queue_with_outage") is not None),
      ("reports recovery time (or says it did not clear)",
       lambda r: "recovery_hours_after_restore" in r["figures"])]),
]

# Notice §1 applies to every answer.
CONTRACT = [
    ("§1.a method stated", lambda r: nonempty(r.get("method"))),
    ("§1.b figures support it", lambda r: nonempty(r.get("figures"))),
    ("§1.c assumptions declared separately", lambda r: nonempty(r.get("assumptions"))),
    ("§1.d queries traceable", lambda r: nonempty(r.get("queries"))),
]

ok_total = fail_total = 0
blocked = []
report = []

for scenario, ref, quote, params, checks in CASES:
    res, err = call(scenario, params)
    print(f"\n{'=' * 78}\n{ref}  {scenario}\n{'=' * 78}")
    if quote != "bidder-proposed":
        print(f"  Notice: {quote[:150]}…" if len(quote) > 150 else f"  Notice: {quote}")
    if err:
        print(f"  ERROR — {err}")
        fail_total += 1
        report.append((ref, scenario, "ERROR", 0, len(checks) + len(CONTRACT)))
        continue

    avail = res.get("data_available")
    print(f"  data_available: {avail}")
    if not avail:
        note = (res.get("notes") or ["(no reason given)"])[0]
        print(f"  BLOCKED — {note[:220]}")
        blocked.append((ref, scenario, note))

    passed = failed = 0
    for label, fn in checks + CONTRACT:
        try:
            good = bool(fn(res))
        except Exception as e:  # noqa: BLE001
            good = False
            label = f"{label}  [{type(e).__name__}]"
        print(f"    {'PASS' if good else 'FAIL'}  {label}")
        passed += good
        failed += not good
    ok_total += passed
    fail_total += failed
    report.append((ref, scenario, "ANSWERS" if avail else "NO DATA", passed, passed + failed))

SAMPLE = {
    "vessel-bunching": ("vessels contending", "vessels_contending", 8),
    "berth-cascade": ("calls in window", "calls_in_window", 8),
    "modal-shift": ("observed gate trips", "baseline_trips", 30),
    "crane-productivity": ("calls with derivable productivity",
                           "calls_with_derivable_productivity", 8),
    "gate-slotting": ("gate arrivals", "total_arrivals", 200),
    "driver-shortage": ("observed trips", "baseline_trips", 30),
}
print(f"\n{'=' * 78}\nDATA SUFFICIENCY — is the sample big enough to present?\n{'=' * 78}")
print(f"  {'ref':<7}{'measure':<38}{'n':>7}  {'min':>5}  verdict")
for ref, scenario, state, p, t in report:
    if scenario not in SAMPLE:
        continue
    label, key, floor = SAMPLE[scenario]
    res, _ = call(scenario, dict(next(c[3] for c in CASES if c[0] == scenario)))
    n = (res or {}).get("figures", {}).get(key)
    n = n if isinstance(n, (int, float)) else 0
    verdict = "OK" if n >= floor else "TOO THIN — figures not presentable"
    print(f"  {ref:<7}{label:<38}{n:>7}  {floor:>5}  {verdict}")

print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
print(f"  {'ref':<7}{'scenario':<21}{'state':<10}checks")
for ref, scenario, state, p, t in report:
    flag = "" if p == t else "   <-- gaps"
    print(f"  {ref:<7}{scenario:<21}{state:<10}{p}/{t}{flag}")
print(f"\n  checks passed {ok_total}, failed {fail_total}")
if blocked:
    print(f"\n  {len(blocked)} scenario(s) could not be answered from the data:")
    for ref, scenario, note in blocked:
        print(f"    {ref} {scenario}: {note[:160]}")
sys.exit(0 if fail_total == 0 else 1)
