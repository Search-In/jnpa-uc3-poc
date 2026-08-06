# UC-3 JNPA Demo Readiness Review

**Date:** 06 August 2026 · **Branch:** `dev_aniket`
**Method:** executed, not inspected. Migrations checked against the live ledger, scenarios run against the live RDS (`jnpa_schema_v3`), UI checked by source search. Read-only throughout.

# VERDICT: NOT READY

Two blocking classes, in priority order:

1. **The demo-window data does not exist** for 4 of the 5 scenarios. This is not a code problem and no code change fixes it.
2. **The UI has zero integration** with the what-if APIs. Nothing is demoable through a screen — only through curl/Postman.

The engine itself is sound: two real defects surfaced during this review and were fixed, and every scenario now either produces a correct answer or correctly refuses to.

---

## 1. Backend Verification

### Migrations — **NOT APPLIED**

The live ledger (`core.schema_migrations`) tops out at **0124**. Verified absent on RDS:

```
core.vessel_call_moves   → does not exist
core.yard_block          → does not exist
core.cargo.evacuation_mode → does not exist
lifecycle CHECK allows PENDENCY → false
```

Wider than my previous report stated: **0125, 0126 and 0127 are also unapplied**, not just 0128–0131. `0125_edi_vessel_container` is unapplied yet `core.edi_vessel_container` exists with 288,619 rows — it was created by the runtime DDL in `services/edi_vessel/repository.py` at gateway boot. That is the migration-vs-runtime-DDL drift flagged as A1 in the original audit, now demonstrated: **the ledger is not a reliable statement of what the schema contains.**

### APIs — all registered and responding

All 8 simulation routes plus `/api/gate/hourly-profile` are mounted and reachable. Route-shadowing against `GET /api/cargo/{container_number}` verified clear.

### Response contract — verified live

A real run (I-B, 5 June) returned all seven contract elements populated: `method`, `result`, `figures`, `assumptions` (5), `queries` (1, with SQL + bound params + row count), `recommendations` (2), `data_available`, `notes`.

### Two defects found by live execution — both fixed

Neither was reachable by the fixture-backed tests; both required a real asyncpg connection.

**D1 — every scenario silently returned zero rows.**
`(:terminal IS NULL OR terminal = :terminal)` raises `AmbiguousParameterError` under asyncpg — Postgres cannot infer a bare parameter's type. The first fix, `:terminal::text`, then failed with a syntax error because SQLAlchemy's `text()` reads the second colon as a bind marker. Correct form is `CAST(:terminal AS text)`, now used in all 6 nullable filters.

**D2 — a failed query was reported as "no data".**
The fail-soft path returned `[]` on error, indistinguishable from an empty table. I-B against **June — where 132 calls exist** — reported *"core.berthing_record returned no calls"*. A confidently wrong answer, which is precisely what this layer is built to never produce. `QueryTrace` now carries an `error` field, and `SimulationResult.to_dict()` promotes any failure into a visible `QUERY FAILED` note and clears `data_available`.

Both are pinned by regression tests. **Suite: 144 passed, 2 skipped.**

---

## 2. Database Verification — 01–06 Aug 2026

| Table | Total rows | In window | Actual coverage | Verdict |
|---|---:|---:|---|---|
| `core.cargo` | 11,944 | **11,925** | 26 Jul – 05 Aug | ✅ |
| `core.gate_event` | 335,967 | **288,591** | 01 Aug – 06 Aug | ✅ |
| `core.edi_vessel_container` | 288,619 | 104 VCNs | berthing_ts 11 Jul – 05 Aug | ✅ present |
| `core.anpr_read` | 317,968 | 264,459 | in window | ✅ |
| `core.eir` | **5** | **0** | 06–12 Jun only | ❌ **blocks III-B** |
| `core.berthing_record` | 185 | **0** | 05 May – 06 Jul | ❌ **blocks I-B, II-B** |
| `core.perf_daily_traffic` | 1,296 | **0** | 01 Feb – 26 May | ❌ **blocks II-A** |
| `core.tas_appointment` | 16 | 8 | stub, capacity=10 | ⚠️ **corrupts III-A** |

Supporting tables for III-B are equally empty: `transporter_vehicle` 3 rows, `tt_trip` 2, `trt_record` 1.

**`core.eir` holding 5 rows is the single most consequential number here.** It is the sole source for III-B and the preferred source for II-A and III-A.

---

## 3. Scenario Demo Verification — live runs against RDS

| Scenario | Notice date | Result | Detail |
|---|---|---|---|
| **I-B** berth cascade | 2 Aug | ❌ NO DATA | no calls in window |
| **I-B** berth cascade | **5 Jun** | ✅ **WORKS** | 17 calls, 5 displaced, **184.32h cumulative**, max 91.2h, 2 recommendations |
| **II-A** modal shift | 1–3 Aug | ❌ BLOCKED | no rail TEU; `evacuation_mode` column absent |
| **II-B** crane productivity | 6 Aug / 5 Jun | ❌ BLOCKED | see below — **migration alone will not fix this** |
| **III-A** gate slotting | 3 Aug | ⚠️ RUNS, MISLEADING | 2,920 arrivals, 24 hours, peak 284 — but see below |
| **III-B** driver shortage | 1–3 Aug | ❌ NO DATA | `core.eir` empty; no fallback exists |

### I-B is genuinely demoable — on a June date

The only scenario that produces a complete, defensible answer today. Per-berth cascade, real vessel names, cumulative delay, recommendations. **The Notice specifies 2 August; the data is June.**

### II-B will NOT be fixed by applying 0129

I assumed the migration would unblock this. Verified against live data — it will not:

- **Terminal codes do not match.** EDI uses UN/LOCODE (`INNSA1BMC1`, `INNSA1NSI1`); `berthing_record` uses short codes (`BMCT`, `NSICT`). **Zero matches.**
- **The natural key cannot match.** DERIVED rows carry `vcn` but NULL `vessel_name`/`voyage_number`.
- **Decisively: zero temporal overlap.** `berthing_record` ends **06 Jul**; EDI `berthing_ts` begins **11 Jul**. Even a perfect join key would find no common call.

Migration 0129 would create 104 move-count rows joining to **zero** berthing calls. II-B has **no derivable date at all** — not just no demo date. My previous report was wrong to list this as merely "unverified"; it is blocked, and the fix is data, not code.

### III-A runs but produces a misleading figure

The sustained-rate chain prefers declared TAS capacity as `MEASURED` — a policy figure beating an inference. Correct in principle; wrong here, because the TAS table is a 16-row demo stub:

| | |
|---|---:|
| TAS declared capacity | **10 /hour** |
| Real p90 GATE_IN | **4,010 /hour** |
| Real mean GATE_IN | 621 /hour |

Result: **21 of 24 hours "saturated", 91.8% excess.** Technically correct given the declared capacity, and the response says which basis it used — but on screen it reads as a broken system.

Also note the synthetic gate data is internally inconsistent (26,397 GATE_ARRIVAL vs 78,768 GATE_IN over the window — more entries than arrivals) and the hourly shape is **flat** (~230–280/hr), so even with a sane ceiling III-A would honestly report `shape: FLAT` and find no peak to flatten. The Notice asks to "characterise the arrival pattern"; this data has no pattern to characterise.

---

## 4. Frontend / UI Audit

**Integration: none. Screens: none. Charts: none.**

A source search for `simulate`, `berth-cascade`, `modal-shift`, `crane-productivity`, `gate-slotting`, `driver-shortage`, `hourly-profile` across `web/src/` returns **zero** API bindings. The only matches are unrelated: mock `simulated: true` magnitudes in `whatif/causalGraph.ts` and "simulated" provenance labels in `FollowTheBox.tsx`.

`web/src/screens/WhatIfConsole.tsx` (569 lines) exists but is a **different thing** — it triggers the TFC-1/2/3 live-injection scenarios and paints a WebSocket storyline. It is a demo harness, not a calculator front-end, and shares no data shape with `/api/cargo/simulate/*`.

`web/src/lib/api.ts` (1,836 lines) has no simulation methods.

### Required build — Cargo What-If Dashboard

Everything needed is already in the project: **recharts 2.13** is a dependency, and `src/components/ui/` has `card`, `badge`, `button`, `select`, `dialog`, `CollapsibleCard`, `dtccc`. No new dependency is required.

| # | File | Contents |
|---|---|---|
| 1 | `web/src/lib/api.ts` *(extend)* | `simulateScenarios()`, `simulate(name, body)`, `gateHourlyProfile(from,to,terminal?,groupBy?)` + TS types `SimulationResult`, `Assumption`, `QueryTrace` mirroring the envelope |
| 2 | `web/src/screens/CargoWhatIf.tsx` | Page shell; owns selected scenario + last result; `useMutation` for the run |
| 3 | `web/src/components/whatif/ScenarioSelector.tsx` | Cards from `GET /api/cargo/simulate/scenarios`; shows JNPA reference (I-B/II-A/…) and the question each answers |
| 4 | `web/src/components/whatif/ScenarioInputPanel.tsx` | Per-scenario form driven by the catalog's `required`/`optional`. Defaults preloaded to the Notice values (6h, 20%, 25%, ⅓, 48h) |
| 5 | `web/src/components/whatif/ResultCards.tsx` | KPI tiles from `figures`. **Must render `data_available: false` as a first-class state** showing `notes` — a blank panel would misrepresent a correct refusal |
| 6 | `web/src/components/whatif/BeforeAfterChart.tsx` | recharts. Grouped bars for II-A (`baseline` vs `shifted` per hour) and III-A (`original` vs `slotted`) with a `ReferenceLine` at the sustained rate; horizontal bars for I-B/II-B delay per vessel |
| 7 | `web/src/components/whatif/AssumptionsPanel.tsx` | Table of `assumptions` with a source badge — colour-code `ASSUMED` distinctly from `MEASURED`. **This is the panel that earns marks under Notice §1.c** |
| 8 | `web/src/components/whatif/QueryTracePanel.tsx` | Collapsible per query: `purpose`, `api`, `row_count`, SQL in `<pre>`, bound params. **Must render `error` in red** — D2 exists precisely because a failure looked like a zero |
| 9 | `web/src/components/whatif/RecommendationList.tsx` | `action` + `reason` + detail chips |
| 10 | `web/src/App.tsx`, `navConfig.tsx` | Route `/cargo-whatif` + nav entry, guarded like `/uc3-lifecycle` |

**Layout:** selector across the top → input panel left / result cards + chart right → assumptions and query-trace as collapsible sections below.

**Effort:** ~1.5–2 days for one frontend engineer. Items 1, 2, 5, 7, 8 are the minimum viable demo; 3, 4, 6, 9 make it presentable.

---

## 5. Final Report

### READY / NOT READY: **NOT READY**

### Remaining tasks

**Database — the critical path. Nothing else matters until this is resolved.**

1. **Load `core.eir` for 01–06 Aug 2026.** 5 rows today. Sole source for III-B, preferred for II-A/III-A. Without it III-B cannot run at all.
2. **Load `core.berthing_record` for 01–06 Aug.** Coverage ends 06 Jul. Blocks I-B and II-B on every Notice date.
3. **Load `core.perf_daily_traffic` DAY rows for 01–03 Aug.** Coverage ends 26 May. Blocks II-A's rail volume.
4. **Resolve II-B's move-count identity.** Applying 0129 is *not* sufficient. Either (a) obtain JNPA move counts keyed to berthing calls and insert with `data_origin='API'`, or (b) import berthing records for 11 Jul–05 Aug so they overlap the EDI manifests, *and* add a terminal-code bridge (`core.ref_terminal_alias` has 45 rows and can map `INNSA1BMC1`→`BMCT`). Without one of these, **drop II-B from the demo.**
5. **Fix or bypass the TAS stub.** Either seed `core.tas_appointment` with realistic hourly capacity for the window, or pass `sustained_rate` explicitly on the III-A call. Otherwise III-A shows 91.8% saturation against a 10/hour ceiling.

**Deployment**

6. Apply **0125 → 0131** (7 migrations, not 4). Verify with the ledger *and* `information_schema` — they disagree today.
7. Re-run the five scenarios against RDS after migrating; confirm II-A and II-B flip to `data_available: true`.

**UI**

8. Build items 1–10 above. Nothing is demoable through a screen today.

**Backend**

9. Nothing blocking. D1/D2 are fixed and tested. Optional hardening, all pre-existing and out of scope for the demo: W4 workflow ordering, `/api/cargo` read RBAC, `plan_rake` N+1, reefer allocator reconciliation, event-table retention.

---

### What I would demo tomorrow if the data does not move

**One scenario: I-B on 5 June.** It is complete, real and defensible — 17 vessel calls, 5 displaced, 184.32 hours cumulative delay, with assumptions and the SQL behind them.

Present it as the *method* — the cascade, the evidence contract, the refusal behaviour — and be direct that the August window has no berthing or gate-document data yet. The refusal behaviour is worth showing deliberately: a system that says *"core.eir records no gate trips between 01 and 03 August — none is invented"* is more credible than one that fills the gap. The Notice rewards exactly that posture.

What would not survive scrutiny is presenting III-A's 91.8% saturation, or claiming II-B works because the migration exists.
