# UC-3 Cargo What-If Dashboard — UI Delivery Report

**Date:** 06 August 2026 · **Branch:** `dev_aniket` · **Route:** `/cargo-whatif`
**Constraint honoured:** no backend change, no mock calculations, no new dependencies. Every figure on screen is returned by `/api/cargo/simulate/*`.

**Verification:** typecheck exit 0 · eslint 0 errors 0 warnings · 146 unit tests pass (+8 new) · production build clean · **driven end-to-end with Playwright against the live RDS-backed gateway, zero page errors.**

---

## 1. Files created

| File | Lines | Purpose |
|---|---:|---|
| `web/src/screens/CargoWhatIf.tsx` | 400 | Page shell, layout, react-query wiring, per-scenario detail tables |
| `web/src/components/whatif/ScenarioSelector.tsx` | 85 | Scenario cards, built entirely from the backend catalog |
| `web/src/components/whatif/ScenarioInputPanel.tsx` | 205 | Per-scenario forms, Notice defaults preloaded, percent→fraction conversion |
| `web/src/components/whatif/ResultCards.tsx` | 180 | KPI tiles + the two refusal states (no-data / query-failed) |
| `web/src/components/whatif/BeforeAfterChart.tsx` | 99 | recharts before/after bars with the capacity reference line |
| `web/src/components/whatif/whatifSeries.ts` | 165 | Pure series-derivation (split out for testability + Fast Refresh) |
| `web/src/components/whatif/whatifSeries.test.ts` | 140 | 8 unit tests over the chart mapping |
| `web/src/components/whatif/AssumptionsPanel.tsx` | 100 | Notice §1.c evidence table with source badges |
| `web/src/components/whatif/QueryTracePanel.tsx` | 140 | Notice §1.d SQL + bound-params trace, failures in red |
| `web/src/components/whatif/RecommendationList.tsx` | 70 | Action / reason / detail chips |

## 2. Files modified

| File | Change |
|---|---|
| `web/src/lib/api.ts` | +3 methods (`simulateScenarios`, `simulate`, `gateHourlyProfile`) and 8 exported types mirroring the backend envelope exactly |
| `web/src/App.tsx` | Import + guarded `/cargo-whatif` route |
| `web/src/components/layout/navConfig.tsx` | Nav leaf in **Truck & Cargo Lifecycle** (it asks "what would change" about the journey the screens above show as-is) |
| `web/src/lib/auth.ts` | `"/cargo-whatif": [...CONTROL_ROOM, "CUSTOMS"]` — mirrors the gateway's `/api/cargo` write policy, so the screen is never visible to a role the API would refuse |
| `web/src/i18n/locales/{en,hi,mr}.json` | `nav.cargoWhatIf` |

**No backend file was touched in this phase.**

## 3. API integration

| Endpoint | Where | Notes |
|---|---|---|
| `GET /api/cargo/simulate/scenarios` | `CargoWhatIf` (react-query, 5-min `staleTime`) | Drives the selector, the question text, the "reads" footnote. **A scenario added to the backend registry appears with no frontend change.** |
| `POST /api/cargo/simulate/berth-cascade` | `useMutation` | I-B |
| `POST /api/cargo/simulate/crane-productivity` | ″ | II-B |
| `POST /api/cargo/simulate/modal-shift` | ″ | II-A |
| `POST /api/cargo/simulate/gate-slotting` | ″ | III-A |
| `POST /api/cargo/simulate/driver-shortage` | ″ | III-B |
| `GET /api/gate/hourly-profile` | `api.gateHourlyProfile` | Typed and exported; the III-A scenario already returns the same profile inline, so the screen does not double-fetch it. Available for a standalone gate view. |

Uses the existing `http()` wrapper — bearer token, `x-data-mode` header, 15s timeout and the shared error shape all come for free. State is TanStack Query, as everywhere else.

**Nothing is computed in the browser.** The one piece of logic, `buildSeries()`, only *reshapes* returned rows into chart series, and its 8 tests assert the reshaping is faithful rather than that any figure is right — the backend suite owns correctness.

## 4. UI flow

```
┌─ Cargo What-If ─────────────────────────────────────────────────────────┐
│ [I-B Extended Berth] [II-B Equipment] [II-A Modal Shift]                │  ← from catalog
│ [III-A Gate Congestion] [III-B Driver Shortage]                         │
├──────────────────┬──────────────────────────────────────────────────────┤
│ Parameters       │ Method  (the reproducible prose, verbatim)           │
│  Notice defaults │ ┌────┬────┬────┬────┬────┐                           │
│  preloaded       │ │ 5  │184h│91h │ 6h │ 17 │  ← figures                │
│ [Run simulation] │ └────┴────┴────┴────┴────┘                           │
│                  │ All computed figures (nothing hidden)                │
├──────────────────┴──────────────────────────────────────────────────────┤
│ Before vs After chart  (capacity ReferenceLine where one exists)        │
│ Scenario detail        (displaced calls / transporters / flows / …)     │
│ Assumptions (n)        MEASURED · DERIVED · PARAMETER · ASSUMED         │
│ Query trace (n)        purpose · api · rows · SQL · bound params        │
│ Recommendations (n)    action · reason · detail chips                   │
└─────────────────────────────────────────────────────────────────────────┘
```

**Per-scenario detail rendered** (all from `result`, none hardcoded):

- **I-B** — displaced-calls table: vessel, voyage, berth, original vs new time, delay hours, and a badge when the duration was *assumed* rather than reported.
- **II-A** — first-constraint callout (which ceiling, at which hour, by how much) + saturated-hour chips.
- **II-B** — before/after pair (moves per hour, hours worked, operation end) + per-call productivity table with a `not derivable` badge and the moves `data_origin` (DERIVED vs API).
- **III-A** — arrival-pattern strip (shape, peak hour, peak, peak/mean ratio) + saturated-period chips.
- **III-B** — state-on-report-date tiles, transporter exposure ranked **two ways** (absolute loss and structural dependence — they suggest different mitigations), and cargo-flow exposure.

### Verified against the live gateway (Playwright, RDS-backed)

| Check | Result |
|---|---|
| Scenario cards rendered from catalog | ✅ 5 |
| I-B (5 Jun) returns a real answer | ✅ 184.32h cumulative, 5 displaced, 17 calls |
| Method / Assumptions / Query trace / Recommendations all present | ✅ |
| III-B (1–3 Aug) shows the refusal state, not a blank panel | ✅ "No data available for selected period" + backend reason |
| Assumptions still shown on a refusal | ✅ |
| III-A chart + reference line render | ✅ observed peak 284, `Sustained 300/h` line |
| Console / page errors | ✅ **none** |

Screenshots: `/tmp/whatif-{1-landing,2-ib-answer,3-nodata,4-iiia-chart}.png`.

### The three states, deliberately distinguished

1. **Answered** — figures render.
2. **No data** — amber panel, *"No data available for selected period"*, plus the backend's own reason ("core.eir records no gate trips between 2026-08-01 and 2026-08-03 — none is invented"). Assumptions and query trace still render below, because they show what was asked of the database.
3. **Query failed** — red panel, explicitly worded *"this is not an empty result"*, and the failing trace auto-expands with its error.

State 3 exists because during the 06-Aug review a failed query was reported as "no calls in this window" for a window holding 132 calls. A UI that renders both identically would re-create that error at the presentation layer.

## 5. Remaining blockers

**None in the UI.** All blockers are the data/deployment ones from the readiness review, unchanged by this work:

| # | Blocker | Effect on the dashboard |
|---|---|---|
| 1 | `core.eir` has 5 rows, none in 1–6 Aug | III-B shows the refusal state on every Notice date |
| 2 | `core.berthing_record` ends 6 Jul | I-B and II-B refuse on Notice dates; **I-B demoes correctly on 5 June** |
| 3 | `core.perf_daily_traffic` ends 26 May | II-A refuses |
| 4 | Migrations 0125–0131 unapplied | II-A's `evacuation_mode` query fails → the red query-failed panel (correctly) |
| 5 | `core.tas_appointment` is a 16-row stub declaring 10/h | III-A derives a nonsense ceiling. **Mitigated in the UI:** the "Sustained gate rate" field is exposed on the form with the hint *"Blank = derived. Override when the slot book is unprovisioned"* — entering 300 produces the sensible answer shown in screenshot 4. |

Two smaller notes, neither blocking:

- **`GET /api/gate/hourly-profile` is wired and typed but has no screen of its own.** III-A already returns the same hourly profile inline, so adding a second fetch would duplicate a query for no gain. If a standalone gate-profile view is wanted, the client method is ready.
- **I-A (Vessel Bunching) has no card**, because the backend deliberately does not register it — the Notice leaves the optimisation objective to the bidder. `berth-cascade` supplies the costing function an I-A answer needs; the objective belongs in the written submission.

---

### One judgement worth flagging

The brief asked for a `BeforeAfterChart.tsx` component. I split the series-derivation into `whatifSeries.ts` beside it — eslint's `react-refresh/only-export-components` rule flags a file that exports both a component and a function, and the mapping is the only real logic in the UI, so it deserved unit tests. That test immediately earned its place: it caught the chart drawing a red **"Sustained 0/h"** ceiling whenever the backend legitimately returned `sustained_rate_per_hour: null`, because `Number(null)` is `0` and `isFinite(0)` is true. On III-A with an unprovisioned slot book that would have implied the gate had zero capacity — a misleading figure in front of an evaluator, which is exactly what the whole evidence contract exists to prevent.
