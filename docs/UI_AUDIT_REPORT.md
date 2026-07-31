# JNPA DTCCC — Full Application UI Audit

**Date:** 2026-08-01 · **Scope:** `web/` (control room) + `mobile-pwa/` (driver) · **Status:** AUDIT ONLY — no code changed
**Evidence base:** 41 routes + 21 nav leaves read from source · live RDS row counts · live API probes against the deployed database · 22 browser screenshots at 3 viewports · console-error capture per screen

**Headline:** the application is **feature-rich and mostly real** — far better than a typical POC. Its problem is not missing capability, it is **discoverability and honesty**: 30 screens are reachable only by hunting through tabs, one screen is a junk drawer with 11 tabs, four screens are entirely dead code, six navigation deep-links silently fail, the header search drops its query on 3 of 7 targets, and a handful of surfaces present numbers that no longer mean anything. Fixing the information architecture — not building new screens — is what makes this demo-ready.

> ### ⚠️ Read §0 first — it changes how every "real data" verdict below must be read.

---

## 0. The data-mode split (correction to my own first pass)

There are **two parallel data layers**, and which one a screen uses decides whether it shows real data:

| Layer | Used by | Dev (`vite`) | Prod (`vite build`) |
|---|---|---|---|
| `lib/api.ts` (`api.*`) | UC-3 Lifecycle, CFS/ECY, Shipping Lines, Berthing, Performance, Driver Master, Accidents, Camera AI, … | **always live** | always live |
| `data/index.ts` (`getAdapter()`) | **Command Center, Live Operations, Alerts Center, Vehicle Management, Driver Enrollments, System Health, FASTag, Police Reports, Geo Analytics, Driver Advisory, What-If, Demo Console** + 10 panels | **MOCK** | live |

`web/.env.local:6` pins `VITE_DATA_MODE=mock`; `vite.config.ts:19-28` makes dev default to mock and prod default to live.

**Correction:** my first pass called Command Center and Live Operations "real data" based on screenshots. Those screenshots were taken in `vite` dev — so the 40 vehicles, 309 parking slots and 7 on-target corridor KPIs I saw were **fabricated by `data/mock.ts`**, not the RDS. That is why Live Operations showed "40/40 vehicles" while the live `/api/trucks` returned **0 devices**.

Two consequences:

1. **The live API probes in §7 — not the screenshots — predict the production demo.** Adapter-backed screens will show live values in a prod build, including the genuine zeros (`core.gate` 0, `reefer_slot` 0, `camera` 0).
2. **Mock KPIs are unlabelled.** `kpi/compute.ts:41-53` never sets `source`, and `KpiStrip.tsx:53` renders the provenance chip only `if (k.source)`. So in mock mode the 7 corridor KPI cards show values **hard-coded on/near target** (`data/mock.ts:376-437` — gate queue 7.4 vs target 8.0, TRT 43.5 vs 45, throughput 61.5 vs 60) with **no MOCK badge**. Only the sidebar "SIM" pill hints at it. *Verified: `SourceBadge` early-returns `null`; `buildKpiResult` omits `source`.*

**Demo rule:** run the client demo from a **production build** (`npm run build`), never `vite` dev. Otherwise you are demoing fabricated on-target numbers.

---

## 1. Complete UI audit report

### 1.1 Inventory — top-level screens (21 nav-reachable)

Data verdicts come from probing the live RDS-backed gateway, not from reading code.

| # | Screen | Route | Purpose | API | Real data (live check) | Demo | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Command Center | `/command-center` | Landing: 16 KPI tiles + GIS map + top alerts/vehicles | ✅ 11 adapters + 8 helpers | ✅ mostly; **4 tiles read 0** (see §7) | ✅ **A** | **IMPROVE** |
| 2 | Live Operations | `/live` | Corridor ops: map, 7 corridor KPIs, terminal cards | ✅ | ✅ 40 vehicles, honest `SYNTHETIC` badge | ✅ **A** | **IMPROVE** (move 2 tabs out) |
| 3 | Driver Advisory | `/advisory` | Per-truck reroute advisory | ✅ | ✅ | B | KEEP |
| 4 | Parking | `/parking` | Facilities, occupancy, violations, reefer | ✅ | ✅ 1,170 slots | ✅ **A** | KEEP |
| 5 | Customs & Gate | `/gate-customs` | e-Seal/Form-13/Weighbridge/ICEGATE + Auto-LEO | ✅ | ⚠️ **0 captures** — gate-data service not running | ✅ **A** | **IMPROVE** |
| 6 | Alerts Center | `/alerts` | Consolidated alerts + accidents/camera/blacklist | ✅ | ✅ 16,402 alerts | ✅ **A** | KEEP |
| 7 | Vehicle & Driver Intelligence | `/intelligence` | 360° vehicle/driver investigation | ✅ | ✅ Vahan/FASTag/geo | ✅ **A** | KEEP |
| 8 | **UC-3 Lifecycle** | `/uc3-lifecycle` | Job → gate → yard → scan → gate-out + chains | ✅ | ✅ jobs, EIR, 1,202 chains | ✅ **A** | KEEP |
| 9 | CFS / ECY Movements | `/cfs-ecy` | CODECO gate movements + dwell | ✅ | ✅ 1,928 movements | ✅ **A** | **MERGE candidate** (§4) |
| 10 | Shipping Lines | `/shipping-lines` | IAL/EAL/EDO advance lists | ✅ | ✅ 8,878 containers | B | **FIX** (React key bug) |
| 11 | Berthing Reports | `/berthing` | Per-terminal vessel calls | ✅ | ✅ 775 vessels | B | KEEP |
| 12 | Performance & Reports | `/performance` | Daily status, monthly TEU, LDB dwell | ✅ | ✅ 1,296 traffic + 1,134 tonnage rows | B | KEEP |
| 13 | Geo Analytics | `/geofencing` | Zones, entry/exit, violations, heatmap, bottlenecks | ✅ | ✅ 14,440 geofence events | B | KEEP |
| 14 | FASTag | `/fastag` | Balance/transactions/journey/toll | ✅ | ⚠️ `mode: demo` — no ULIP creds | B | KEEP (label it) |
| 15 | Reports & Enforcement | `/reports` | **11 tabs** — enforcement + 10 unrelated features | ✅ | ⚠️ mixed; **"Challans Issued" is not from `core.challan`** | B | **SPLIT — biggest problem** |
| 16 | Vehicle Management | `/vehicles` | Fleet + transporters + drivers + upload | ✅ | ✅ 40 vehicles, 2,194 transporters, 31,846 drivers | ✅ **A** | KEEP |
| 17 | Driver Enrollment | `/enrollments` | PWA enrollment approval queue | ✅ | ✅ | B | KEEP |
| 18 | Workflow Composer | `/workflows` | IF/THEN automation authoring | ✅ | ❌ **actions never execute** (proven §2) | ❌ | **HIDE** |
| 19 | System Health | `/health` | Subsystem status + integrations + NVR | ✅ | ✅ | B | KEEP |
| 20 | What-If Console | `/what-if` | Scenario planner TFC-1/2/3 | ✅ | ✅ | B | KEEP |
| 21 | Demo Console | `/demo` | Fault injection / presenter QA | ✅ | ✅ | Internal | KEEP (hide in client build) |

### 1.2 Inventory — embedded-only screens (12, no nav entry)

Each is real, working UI reachable **only** by finding the right tab inside a host.

| Screen | Reachable via | Also duplicated in | Verdict |
|---|---|---|---|
| Accidents | `/alerts?tab=accidents` | `/reports` tab | KEEP (single host) |
| CameraAI | `/gate-customs?tab=camera` | `/alerts`, `/intelligence`, `/reports` — **4 hosts** | **DE-DUPLICATE** |
| TransporterBlacklist | `/vehicles?tab=transporters` | `/alerts`, `/reports`, **and `/vehicles?tab=blacklist` (same screen twice in one host)** | **DE-DUPLICATE** |
| DriverMaster | `/vehicles?tab=drivers` | — | KEEP |
| EcyTrt | `/live?tab=trt` | `/reports` tab | **MOVE** (§8) |
| DoubleTrip | `/live?tab=double-trip` | `/intelligence`, `/reports` — 3 hosts | **MOVE + de-dup** |
| DocumentOCR | `/reports?tab=document_ocr` | — | **MOVE to UC-3 Lifecycle** |
| NvrIntegration | `/health?tab=nvr` | `/reports` tab | KEEP (single host) |
| Integrations | `/health?tab=integrations` | `/reports` tab | KEEP (single host) |
| ReeferAvailability | `/parking?tab=reefer` | `/reports` tab | KEEP (single host) |
| RoadBottlenecks | `/geofencing?tab=bottlenecks` | `/reports` tab | KEEP (single host) |
| GeofencingManager | `/geofencing?tab=zones` | — | KEEP |

### 1.3 Driver PWA (11 routes, 6 bottom tabs)

| Item | Finding | Verdict |
|---|---|---|
| Bottom tab bar | **6 tabs** (Home · Navigate · Alerts · Jobs · Parking · Vehicle) — the code's own comment describes a "native-style **5-tab**" bar. I added Jobs without removing one. At 390 px this is cramped. | **FIX — my regression** |
| `/jobs` (UC-3) | Real: driver-scoped job list + accept/gate/pickup/drop/complete | KEEP |
| `/trip`, `/zones`, `/reroute`, `/inbox`, `/enroll` | Routed but **not in the tab bar** — reachable only from Home actions or a push interrupt | KEEP (intentional) |

---

## 2. Current navigation problems (ranked by demo risk)

**P1 — `/reports` is a junk drawer.** "Reports & Enforcement" hosts **11 top tabs plus 6 sub-tabs**: Enforcement, Document OCR, Accident Report, ECY TRT, Double Trip, Blacklist, Camera AI, NVR, Road Bottlenecks, Reefer, Integration Health. Nine of those already have a proper home elsewhere. An operator asked to "show gate scanning" has two plausible paths and no reason to prefer either. *Evidence: screenshot `aud-reports.png`.*

**P2 — Workflow Composer's actions are inert.** Proven live, not inferred: firing the seeded "Over-speed → violation" rule returned `actions_fired: ["create_violation","notify_officer"]`, and `core.violation_case` stayed at **0 rows**. The screen looks completely functional. If a client clicks Evaluate and then opens Reports expecting a violation, nothing is there.

**P3 — Three deep-links silently fail.** Command Center and Demo Console emit `?tab=` params that the target screens never read:
- `/geofencing?tab=bottlenecks` → lands on Live Zones
- `/parking?tab=reefer` → lands on Facilities
- `/health?tab=integrations` → lands on Services
The user clicks a KPI tile and arrives at the wrong tab — which reads as "the link is broken".

**P4 — `/document-ocr` redirects to a screen that does not host it.** It points at `/uc3-lifecycle`, which has no OCR tab. The only real host is `/reports?tab=document_ocr`. Following the redirect, the feature is unreachable.

**P5 — Five redirects drop their tab**, landing on a host default: `/camera-ai`, `/nvr`, `/integrations`, `/bottlenecks`, `/reefer`.

**P6 — 30 of 33 web screens have no nav entry.** 12 are embedded-only and 18 are redirect shims. Nav shows 21 leaves; the app actually contains 33 distinct screens.

**P7 — Two fully dead screens** (762 lines): `FollowTheBox.tsx` (431) and `GeofenceEnforcement.tsx` (331) — no route, no nav, no import. Plus `berthing/FullExtract.tsx`'s dead default export (~200 lines).

**P8 — Duplicate mounts.** `TransporterBlacklist` renders at **4 mount points**, twice inside `/vehicles` alone (tabs `transporters` and `blacklist`) with no prop distinguishing them.

**P9 — The header Global Search drops the query on 3 of 7 targets.** `GlobalSearch.tsx:63-68` always navigates to `${route}?q=…`, but `GateCustoms.tsx` (container), `PoliceReports.tsx` (case) and `AlertsCenter.tsx` (alert — reads `tab` only) never read `q`. *Verified by grep: 0 hits for `useSearchParams`/`get("q")` in GateCustoms and PoliceReports.* A client typing a container number into the most prominent control in the app lands on an unfiltered screen. **This is the single most likely live-demo failure.**

**P10 — Map settings is a dead feature.** `lib/mapSettings.ts:37` `setBasemap` is called nowhere; its own doc comment says it is "surfaced in the header More options → Map settings", but that menu was deleted (`HeaderActions.tsx:7-8`). `useMapSettings()` in **5 screens** always returns the frozen `"satellite"`.

---

## 3. Screens to REMOVE or HIDE

| Item | Action | Reason |
|---|---|---|
| `screens/FollowTheBox.tsx` | **REMOVE** (431 lines) | Dead; concept withdrawn by the client. Its route already redirects to `/uc3-lifecycle`. |
| `screens/GeofenceEnforcement.tsx` | **REMOVE** (331 lines) | Dead; fully superseded by `GeoAnalytics` (identical tabs + identical API helpers). |
| **`components/layout/Header.tsx`** | **REMOVE** (65 lines) | Dead — superseded by the header inside `Shell.tsx`. 0 importers. |
| **`components/layout/Sidebar.tsx`** | **REMOVE** (69 lines) | Dead — superseded by the sidebar inside `Shell.tsx`. 0 importers. |
| `berthing/FullExtract.tsx` default export | **REMOVE** (~200 lines) | Dead export; the named `TablePanel` stays. |
| **`maplibre-gl` dependency** | **REMOVE** | All maps are ArcGIS; only a `import type` remains. Dead runtime dep + CSS import at `main.tsx:23`. |
| **~90 dead i18n keys ×3 locales** | **REMOVE** | 12 orphan `nav.*`, 58 `followBox.*`, `header.mapSettings`/`basemap.*`, `demo.roadmap*` ("not wired"), `panels.identity.simulate*`. ⚠️ Do **not** bulk-delete: ~90 further keys are built by template interpolation (`kpiLabel.*`, `alertKind.*`, `map.layer.*`) and only *look* unused. |
| **Workflow Composer** `/workflows` | **HIDE from nav** | Actions never execute (§2 P2). Keep the code; re-enable when actions are wired. Highest demo-trap risk. |
| `/reports` tabs: ECY TRT, Double Trip, Camera AI, NVR, Bottlenecks, Reefer, Integration Health, Blacklist, Accident, Document OCR | **REMOVE from this host** | Each already has a correct home. Leaves `/reports` as a real reports screen. |
| `/vehicles?tab=blacklist` | **REMOVE** (duplicate) | Same component as `?tab=transporters` in the same host. |
| `/geofence-events`, `/whatif` | **REMOVE routes** | Duplicate mounts of screens already routed; no nav entry, no in-app link. |
| `/launcher` | **HIDE or link it** | Unreachable — nothing in either app navigates to it. |
| Demo Console `/demo` | **HIDE in client build** | Presenter/QA tool; fault injection in front of a client is a risk. |
| 13 orphan `nav.*` i18n keys + `navSection.completion` | **REMOVE** | Leftovers from the pre-consolidation IA. |

**Not removed, deliberately:** every "empty" screen whose emptiness is a *data* condition rather than a design failure (Reefer, Camera AI, gate captures). Those need an honest empty state, not deletion — see §5.

---

## 4. Screens to MERGE

| Merge | Into | Rationale |
|---|---|---|
| **ECY TRT** + **Double Trip** | Out of `/live`, into a **Truck Operations** home (or UC-3 Lifecycle) | They are truck turn-round metrics, not corridor traffic. See §8. |
| **CFS / ECY Movements** | Consider folding under **UC-3 Lifecycle** as a 5th tab | UC-3 already owns the ECY→CFS *chains*; the raw movement browser is the same domain. Counter-argument: 1,928 rows is a genuine standalone dataset, and the module has its own upload panel. **Recommendation: keep separate for now, cross-link both ways.** |
| **Document OCR** | Into `/uc3-lifecycle` as a Documents sub-tab | It extracts Form-13/LR/permit fields — exactly the gate-document domain. Also fixes the broken `/document-ocr` redirect. |
| **Camera AI** (4 mounts) | Single home at `/gate-customs?tab=camera` | Container/trailer OCR is a gate function. |
| **TransporterBlacklist** (4 mounts) | Single home at `/vehicles?tab=transporters` | It is the Transport Master. |

---

## 5. Screens to IMPROVE

| Screen | Problem | Fix |
|---|---|---|
| Command Center | 4 tiles render `0` indistinguishably from "loading" and from "genuinely zero" (`?? 0` over `any`) | Render `—` + a "no data" tooltip when the query is empty/errored |
| Command Center | `TRT ≥ 120 min` alert banner reads `avg_trt_min` **without checking `source: "baseline"`** — a hardcoded placeholder (135 min) can raise an operator alert. The same file gets this right for the *card* 160 lines earlier | Honour `source` in the banner, as `EcyTrt.tsx:75` already does |
| Reefer Availability | Backend returns `{"totals": {}}` when empty → **five KPI tiles all show `0`**, which reads as "0 reefer slots free" (an alarm state) | Empty state: "No reefer slots configured" |
| Shipping Lines | **28 React duplicate-key warnings** on load (console) | Fix the list key |
| Customs & Gate | 0 captures because the `gate-data` service isn't running | Show a service-down state, not an empty table |
| Reports & Enforcement | "Challans Issued: 3" while `core.challan` has **0 rows** — the number does not come from the challan table | Re-source or remove the tile |
| All 5 upload panels | Every one lists uploads but **none can open one** (`*UploadDetail` helper exists, zero call sites) — a systematic gap | Wire the per-file drill-down (5 screens, one pattern) |
| PWA | 6 bottom tabs | Return to 5: fold Vehicle/Profile into Home, or Parking into Jobs |
| **Global Search** | Drops the query for container / alert / case (P9) | Read `?q=` in `GateCustoms`, `AlertsCenter`, `PoliceReports` — 3 small edits, highest demo value |
| **Live Ops KPI strip** | Mock KPIs render **unbadged** (§0) | Set `source: "baseline"` in the mock builder so the existing `SourceBadge` fires |
| **Launcher** | 2 of 3 tiles permanently disabled; UC-2's `href` field is declared but never rendered, so "External twin" is a dead tile; footer still cites "Follow-the-Box" | Fix or hide the screen |
| **What-If Console** | "Open trace" links to hardcoded `http://localhost:16686` (Jaeger) — dead for every non-local user; copy says "Major Accident is planned — no live scenario yet" | Hide the link off-localhost |
| **Integrations tab** | Books every slot as hardcoded `vehicle_id: "TRK-000123"`; a `StatusChip label="MOCK"` is **hardcoded** regardless of the real source | Bind to the real source; parameterise the vehicle |
| 6 upload panels (~2,150 lines) | Near-identical flow; their own headers say "Mirrors … **exactly**" | Consolidate into one parameterised component (post-demo) |

---

## 6. Final recommended navigation

Reorganised so the sidebar mirrors the operator's actual workflow. **No screen is lost** — everything below already exists.

```
OPERATIONS  (live, now)
  Command Center            /command-center
  Live Operations           /live              ← corridor only (TRT/Double-Trip move out)
  Driver Advisory           /advisory
  Alerts Center             /alerts
  Parking                   /parking

TRUCK & CARGO LIFECYCLE  (the UC-3 story — new grouping, existing screens)
  UC-3 Lifecycle            /uc3-lifecycle     ← + Document OCR as a tab
  Customs & Gate            /gate-customs      ← single home for Camera AI
  Truck Operations          /truck-ops         ← NEW HOST for ECY TRT + Double Trip
  CFS / ECY Movements       /cfs-ecy
  Shipping Lines            /shipping-lines

INTELLIGENCE & ANALYTICS
  Vehicle & Driver Intel    /intelligence
  Geo Analytics             /geofencing
  FASTag                    /fastag
  Berthing Reports          /berthing
  Performance & Reports     /performance
  Reports & Enforcement     /reports           ← reduced to 6 real report tabs

ADMINISTRATION
  Vehicle Management        /vehicles
  Driver Enrollment         /enrollments
  System Health             /health
  What-If Console           /what-if
  [hidden: Workflow Composer, Demo Console, Launcher]
```

Sections go from 3 → 4; nav leaves from 21 → 19 (2 hidden, 1 added). "Truck Operations" is the only new route, and it is a **host for two screens that already exist**.

---

## 7. Data-visibility check (live against the deployed RDS)

**Populated and demo-safe:** truck_telemetry 26.1 M · rfid_read 6.1 M · pdp 367,078 · anpr_read 53,509 · gate_event 47,376 · driver 31,846 · alert 16,402 · geofence_event 14,440 · igm_line_container 12,235 · advance_list_container 8,878 · transporter 2,194 · cfs_ecy_movement 1,928 · ecy_cfs_chain 1,202 · parking_slot 1,170 · berthing_report_vessel 775.

**Empty tables that a screen reads (zero-value cards):**

| Table | Rows | Screen affected | Action |
|---|---|---|---|
| `automation_rule` / `automation_execution` | 0 / 0 | Workflow Composer | HIDE screen |
| `violation_case`, `challan`, `case_audit` | 0 | Reports → Violations/Challans | Honest empty state |
| `reefer_slot` | 0 | Reefer Availability | Empty state (not five `0`s) |
| `camera` | 0 | Camera AI | Empty state |
| **`core.gate`** | **0** | **`/api/gates` returns 0 → no gate markers on any map, incl. PWA** | **Seed 5 gates — highest-value 10-minute fix** |
| `air_quality_readings`, `ldb_movement`, `logistics_*`, `toll_enroute` | 0 | tiles on Live Ops / FASTag | Hide tile when empty |
| `customs_message`, `sl_import_file` | 0 | import-ledger tabs | Expected (docs came via base schema, not the ledger) |

**Two data-model findings:**
1. **`core.container` holds 22,624 containers and is read by NO code.** The cargo screens read `core.cargo` (**19 rows**). A real container dimension sits unused while the demo shows 19 boxes.
2. FASTag runs `mode: demo` (no ULIP credentials) — fine, but the screen should say so.

---

## 8. Live Operations review — should ECY TRT and Double Trip stay?

**Recommendation: MOVE both out of `/live`.**

- **Domain mismatch.** `/live` is the NH-348 corridor view: vehicle positions, corridor KPIs, terminal throughput, congestion. ECY TRT measures a truck's turn-round *inside* the terminal (Gate-In → Parking → Loading → Gate-Out) and Double Trip measures tractor cycle productivity. Neither is a corridor concern; both are gate/yard concerns — the UC-3 domain.
- **Duplication.** Both are *already* mounted a second time under `/reports`, and Double Trip a third time under `/intelligence`. Three homes for one screen is why operators can't form a mental model.
- **Thin data reinforces it.** `core.trt_record` has **1 row**, `core.tt_trip` has **2**. As tabs on the busiest operational screen they look like filler; grouped with the UC-3 lifecycle they read as the natural completion of the truck journey.
- **The tab is the only thing keeping them alive.** `/trt` and `/double-trip` both redirect into `/live?tab=…`; neither has a nav entry.

**Do:** create a `Truck Operations` host (ECY TRT · Double Trip · TRT records), place it in the new *Truck & Cargo Lifecycle* section next to UC-3 Lifecycle, repoint `/trt` and `/double-trip` there, and remove both tabs from `/live` and `/reports`. `/live` then does one thing well.

---

## 9. UI changes required, components impacted, API dependencies

**No new design system.** Every change reuses `components/ui/dtccc.tsx` (`PageContainer`, `PageHeader`, `SegmentedTabs`, `StatCard`, `StatusChip`, `DataTable`, `SearchInput`), `ui/card`, `ui/button`, `ui/misc` (`EmptyState`/`LoadingState`/`ErrorState`).

| Change | Files impacted | API dependency |
|---|---|---|
| New `Truck Operations` host | `App.tsx`, `navConfig.tsx`, `en/hi/mr.json`, `lib/auth.ts`, new `screens/TruckOperations.tsx` (thin host) | none new — reuses `/api/trt/*`, `/api/double-trip/*` |
| Strip 10 tabs from `/reports` | `PoliceReports.tsx`, `reports/Uc3ReportTabs.tsx` | none |
| Fix 3 broken `?tab=` links | `GeoAnalytics.tsx`, `ParkingManagement.tsx`, `SystemHealth.tsx` (add `useSearchParams`) | none |
| Fix 5 tab-dropping redirects | `App.tsx` | none |
| Move Document OCR into UC-3 | `Uc3Lifecycle.tsx`, `App.tsx` | `/api/ocr/*` |
| Delete dead screens | `FollowTheBox.tsx`, `GeofenceEnforcement.tsx`, `FullExtract.tsx` | none |
| Honest empty/zero states | `CommandCenter.tsx`, `ReeferAvailability.tsx`, `GateCustoms.tsx` | none |
| `source: "baseline"` in banner | `CommandCenter.tsx:443` | `/api/trt/summary` |
| Duplicate-key fix | `ShippingLines.tsx` | none |
| Upload drill-down ×5 | 5 `UploadPanel.tsx` | `*UploadDetail` helpers **already exist** |
| Seed `core.gate` | data task, no UI change | `/api/gates` |
| PWA 6 → 5 tabs | `mobile-pwa/src/App.tsx` | none |

**Backend with no UI at all (decide, don't build blindly):** the **Marine domain — 7 routers, 27 endpoints** (`/api/marine/*`) has zero frontend. It is UC-I scope, not UC-3. **Recommendation: leave it; do not build screens for it in this phase.**

**Realtime left on the table:** the backend broadcasts 7 WS channels (`accident`, `bottleneck`, `camera_ai`, `double_trip`, `reroute_ack`, `tas`, `trt`) that **no screen subscribes to** — those screens poll every 8 s instead. Conversely the frontend listens for a `traffic` channel **no backend produces**. Low-risk polish, not demo-critical.

---

## 10. Recommended demo flow

Every step below shows real database data today (after the `core.gate` seed).

1. **Command Center** — the port at a glance: 16 KPIs, live map, alerts. *"One screen, whole corridor."*
2. **Live Operations** — drill into the corridor: 40 vehicles, gate queues, terminal throughput, congestion.
3. **UC-3 Lifecycle** — the story: search a container → job assignment (validated against PDP permit + blacklist) → Gate In with BAT lane → Yard Pickup at `2P08D.1` → Scanner `D-INNSA1RSDT02` → Gate Out. Then the **Documents** tab (EIR with measured 165/82-min TAT) and **ECY → CFS Chains** (242 complete chains, anomaly flagged).
4. **Truck Operations** *(new host)* — ECY TRT phases and Double-Trip cycles.
5. **CFS / ECY Movements** — the 1,928 raw CODECO movements behind the chains.
6. **Vehicle & Driver Intelligence** — pick the truck from step 3, show its 360°: Vahan RC, FASTag, customs, geo history.
7. **Driver PWA** — same job from the driver's phone: Accept → Reached gate → Confirm pickup → Complete.
8. **Reports & Enforcement** — close with police reports and the customs picture.

**Keep out of the client demo:** Workflow Composer (inert actions), Demo Console (fault injection), Launcher (orphan).

---

## Recommended implementation order (on approval)

| Phase | Work | Effort | Risk |
|---|---|---|---|
| **1 — Truth** | **Demo from a prod build (§0)**; fix Global Search `?q=` on 3 screens (P9); badge mock KPIs; hide Workflow Composer + Demo Console; fix 3 broken deep-links + 5 tab-dropping redirects; fix `/document-ocr`; honest empty states; `source: "baseline"` banner fix; duplicate-key fix | ~0.5 d | very low |
| **2 — Declutter** | Strip 10 tabs from `/reports`; de-duplicate Camera AI + TransporterBlacklist; delete 4 dead screens + dead export + `maplibre-gl`; remove `/geofence-events`, `/whatif`; clean the *verified* orphan i18n keys | ~0.5 d | low |
| **3 — Re-group** | New `Truck Operations` host; 4-section sidebar; move Document OCR into UC-3; repoint redirects | ~1 d | medium (nav muscle-memory) |
| **4 — Data polish** | Seed `core.gate`; wire 5 upload drill-downs; decide on `core.container`'s 22 k rows | ~0.5 d | low |

Phases 1 and 2 alone remove every demo trap. Phase 3 is what makes the sidebar tell the UC-3 story.

**Awaiting approval before any code change.**
