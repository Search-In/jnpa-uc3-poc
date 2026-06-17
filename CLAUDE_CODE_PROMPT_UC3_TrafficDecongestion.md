# Claude Code Project Prompt — JNPA Use Case 3 (Traffic Monitoring & Vehicular Decongestion)

Run this **inside your existing UC3 repo** (open the folder in VS Code, then `claude`). It is written as an **audit-then-remediate** task: Claude Code first inventories what you already have, scores it against every Appendix C requirement / D.2 marking line / committed bid clause, prints a coverage matrix, and only then builds the gaps to Use Case 1 production parity.

Tender: **GEM/2026/B/7297343** · Appendix C §2.3 (Corrigendum 3, pages 42–44) · Bid §8.5 · D.2 PoC = 10 marks.

---

## PROMPT (copy from here ↓)

You are taking an existing, partially-built **Traffic Monitoring & Vehicular Decongestion** PoC (JNPA Digital Twin, Use Case 3) to production grade. The reference quality bar is our Use Case 1 build (ArcGIS-native, typed data adapter with `mock|live` switch, tested KPI engine, Calcite dark shell, documented fallback chains). Do **not** scaffold from scratch — this repo already exists. Work in two phases.

### PHASE 0 — AUDIT (do this first, output before writing any code)
1. Inventory the repo: print the folder tree, the framework/build tool in use, the data-access layer, every component, every model/notebook, and any ArcGIS integration already present.
2. Produce a **Coverage Matrix** table with one row per requirement item below and columns: `Requirement | Where in Appendix C / Bid | Present? (Y/Partial/N) | File(s) | Gap`. Score honestly.
3. Flag any architecture mismatch vs the Use Case 1 standard (no typed data adapter, no mock/live switch, AIS/camera APIs called directly from UI, no tested KPI engine, no Calcite/ArcGIS theming, no fallback banners).
4. **Stop and show me the matrix + a remediation plan** (ordered, smallest-blast-radius first) before Phase 1.

#### Requirement items the matrix MUST cover

**A. Appendix C — Use Case III Requirements (all 8, pages 42–44):**
1. Scalable **mobile application + Digital Twin module** with real-time routing guidance, **parking availability within the geo-fenced port area**, and congestion heatmap of approach routes.
2. Augmentation of **PDP with a face-recognition system** + **Vahan & Sarathi** API integration.
3. **Empty-container supply–demand optimisation** with probable allocation via integration with fleet owners, shipping line, CFS and Empty Container Depots; same arrangement for trucks/trailers for **oil tankers, break-bulk & cement bowsers**.
4. **Alerts & notifications for Customs** (compliance / flags).
5. **Vehicle & container identification, e-seal data, Form 13, weighbridge data, ICEGATE data** capture for the **Auto-LEO** process.
6. **Carbon-emissions calculation** using fleet-transporter APIs + CPP / parking-area data for trailers in AoI.
7. **AI video-analytics pipeline / GPS tracking**: vehicle detection, classification, OCR, trajectory tracking, traffic-density heatmaps, ETA predictions.
8. **Geofencing + no-parking-zone violation detection** with automated alerts & notifications.

**B. Appendix C — Use Case III KPIs (acceptance criteria):**
- Gate Queue Wait Time (min/hr)
- Average Gate Transaction Time (min/hr)
- TRT for empty containers from ECD (min/hr)
- Turn Around Time Inside Port (min/hr)
- Plus operational rollups from bid §8.5.4: queue length, average vehicle dwell, gate-wise throughput.

**C. D.2 PoC marking scheme — must each be independently demonstrable (2 marks each):**
1. Solution approach / methodology + assumptions (listed explicitly in-app).
2. Usage of AI/ML tools.
3. API / data-integration plan + **fallback mechanism on data unavailability**.
4. Dashboard view & KPI monitoring.
5. **What-if scenarios** showing interdependency impact + **automated reactive workflow**.

**D. Bid §8.5 commitments (we are contractually held to these — match them exactly):**
- §8.5.1 data sources: ANPR/OCR from public Indian-plate corpora at port conditions (dust/fog/night), **OCR ≥ 95%**; Vahan/Sarathi/FastTag public-schema simulator; RFID from NLDS readers; Trucking-App telemetry simulated at **20,000 installs scalable to 30,000+**.
- §8.5.2 models: ANPR **CNN detector + CRNN OCR** (≥95%); **Traffic Congestion Forecaster = GNN over road network (gates → Karal Phata ~40 km) + LSTM**, **F1 ≥ 0.85**; **Behavioural Anomaly Detector = ByteTrack + rule + autoencoder** (wrong-way, abandoned, illegal parking, route deviation); driver-side ETA/advisory engine.
- §8.5.3 fallbacks: camera/ANPR **live → cached → synthetic replay**; Vahan/Sarathi/FastTag **live → cached KYC → PROVISIONAL (24-hr cure window)**; Trucking-App **GPS → ULIP relay → web check-in**.
- §8.5.4 dashboard: 40-km port-to-NH corridor heatmap; geofencing alerts with duration-based escalation; **reports to traffic-police authorities**.
- §8.5.5 scenarios: **TFC-1 Gate Closure**, **TFC-2 Wrong-Way at Karal Phata**, **TFC-3 Cargo Surge Cross-Twin (DPD release from UC2 → port-road congestion)**.

### PHASE 1 — REMEDIATE TO PRODUCTION GRADE

**Tech stack (align to UC1 for portability across PoCs):**
- ArcGIS Maps SDK for JavaScript **5.x** via `@arcgis/core` + `@arcgis/map-components` (`<arcgis-map>` / `<arcgis-scene>` web components — no deprecated widget classes). Production target is **ArcGIS Enterprise 11.3** at JNPA; PoC may run on ArcGIS Online.
- React 18 + TypeScript (strict) + Vite; `@esri/calcite-components-react` dark shell.
- Real-time vehicle/camera telemetry: **ArcGIS GeoEvent Server / ArcGIS Velocity** → **Stream Layer**; PoC fallback = client-side feature collection fed by the simulators.
- AI services as **separate FastAPI (Python) microservices** (ANPR, congestion, anomaly, ETA) behind the API gateway; the web app never calls models directly.
- **Single typed data adapter** (`src/data/`) with `MockAdapter` + `LiveAdapter` behind one interface, selected by `VITE_DATA_MODE=mock|live`. UI never touches camera/Vahan/ULIP APIs directly.
- Trucking App: **React Native (Expo)** mobile + responsive **web check-in** sharing one API layer.

**Build / fix these modules (each maps to a requirement above):**
1. `gis/` — ArcGIS map with layers: gates, port roads (gate→Karal Phata corridor), geofences/no-parking zones, **live congestion heatmap** (density renderer), parking facilities with live availability counts. Toggleable layers, popups, Calcite legend.
2. `anpr-service/` — CNN vehicle detector + CRNN OCR (Indian plates), augmented for dust/fog/night; expose `/detect`; emit **OCR confidence**; assert ≥95% on the eval set; **fallback live→cached→synthetic replay** with per-device degradation state.
3. `congestion-service/` — GNN(road graph)+LSTM forecaster; `/forecast?horizon=30..120`; **F1 ≥ 0.85** reported in `metrics.json`; feeds the heatmap + ETA-to-gate.
4. `anomaly-service/` — ByteTrack + rules + autoencoder for wrong-way / abandoned / illegal-parking / route-deviation; emits geo-tagged events with snapshot reference.
5. `eta-advisory/` — driver ETA-to-gate + routing advisory; pushed to mobile app + web; reroutes on TFC-1/TFC-3 triggers.
6. `identity/` — **face-recognition driver verification (PDP augmentation)** + **Vahan/Sarathi/FastTag** connector (public-schema simulator) with **cached-KYC → PROVISIONAL 24-hr cure** fallback.
7. `gate-data/` — **e-seal, Form 13, weighbridge, ICEGATE** capture feeding the **Auto-LEO** workflow + container/vehicle ID match; **Customs alerts & flags** notifications.
8. `empty-container/` — supply–demand optimiser + **probable allocation** across fleet owners / shipping line / CFS / ECD (and tanker/break-bulk/cement-bowser variants); drives the **TRT-for-empty-from-ECD** KPI.
9. `carbon/` — emissions calculator from fleet-transporter API + CPP/parking dwell; AoI rollup tile.
10. `kpi/` — pure, **unit-tested** functions for Gate Queue Wait Time, Avg Gate Transaction Time, TRT empty-from-ECD, TAT-inside-port, plus queue length / dwell / gate throughput; each returns `{value, target, deltaPct, trend[]}` vs configurable baseline (% improvement vs current baseline ops, as the KPI table requires).
11. `dashboard/` (DTCCC view) — KPI strip, corridor heatmap, gate-wise queue panels, anomaly feed, parking-availability board, carbon tile, **traffic-police report export**, and a per-integration **Health Card** (last good poll, error count, Green/Amber/Red degradation) so fallback behaviour is visible on screen.
12. `scenarios/` — orchestrated **what-if engine** running TFC-1/2/3, each visibly recomputing KPIs and firing the automated reactive workflow (advisory push, alert, reroute). TFC-3 must consume a DPD-release event from the Use Case II twin to prove **cross-twin interdependency**.

**Assumptions & data:** maintain `docs/ASSUMPTIONS.md` listing every assumed/synthetic dataset with justification (D.2 sub-criterion 1 is partly scored on this). Baseline from jnport.gov.in (Reports/NLDS); ULIP (goulip.in) as the public relay; all production APIs (Vahan/Sarathi/ICEGATE/FastTag/TOS) marked "JNPA-facilitated, simulator in PoC" per Appendix A2.

**Quality bar (UC1 parity):** TS strict, ESLint+Prettier, Vitest for every KPI fn + adapter contract, pytest for model metric thresholds; graceful empty/error states (Calcite notices, never blank); responsive to tablet; keyboard focus + reduced-motion; multilingual scaffolding **Hindi / English / Marathi** (Corrigendum 3 Appendix A6); secrets only in `.env` (+ committed `.env.example`); no colour literals outside `tokens.ts`.

**Deliverables:**
1. `npm run dev` runs the full dashboard in **mock** mode with zero credentials (demos instantly at JNPA).
2. The mobile + web Trucking App runs against the same mock adapter.
3. `README.md` (run, simulator setup, GeoEvent/Velocity live path, the three fallback chains, ArcGIS embed path) + `docs/COVERAGE.md` (the Phase-0 matrix, now all green) + `docs/KPI_DEFINITIONS.md` + `docs/ASSUMPTIONS.md`.
4. A `scripts/poc-selftest` that asserts each D.2 sub-criterion is demonstrable and prints a pass/fail line per requirement item.

**Start with Phase 0.** Print the coverage matrix and remediation plan, then wait for my go-ahead.

## (end prompt ↑)

---

### Notes for you (not part of the prompt)
- **Why audit-first:** your repo already exists, so rebuilding risks losing working pieces. The matrix also doubles as `docs/COVERAGE.md` — useful evidence for **D.2 sub-criterion 1** and a clean artefact to drop into Annexure PoC-2.
- **The four under-covered points:** bid §8.5 leans heavily on ANPR/congestion/anomaly. Appendix C also demands **parking availability, face-recognition (PDP), empty-container allocation, carbon emissions, and Auto-LEO data capture**. The prompt forces all of them in — otherwise an evaluator reading Appendix C point-by-point finds gaps even though §8.5 reads well.
- **Cross-twin scenario (TFC-3)** is the one most teams skip. The marking scheme explicitly rewards "interdependencies" and "multi-player, cross-domain" orchestration — so wiring a real UC2→UC3 event (DPD release → road congestion) is worth a full mark and is the hardest to fake live.
- **Live vs PoC data:** JNPA runs ArcGIS Enterprise 11.3, where Velocity needs Enterprise 12.1 — so for the on-prem production path use **GeoEvent Server** for vehicle/camera streams; ArcGIS Online + Velocity is fine for the PoC demo. The prompt keeps both paths open behind the `mock|live` switch.
- **One decision before you run it:** confirm whether the PoC face-recognition demo uses a synthetic/consented driver dataset only. Given DPDP Act exposure flagged in your bid (§7.4), keep PoC biometrics on synthetic faces and note it in `ASSUMPTIONS.md` — real PDP biometrics are a post-award, consent-gated workflow.
