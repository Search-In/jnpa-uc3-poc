# UC3 Production-Readiness Audit — Executable Checklist for Claude Code

Open the **existing UC3 repo** in VS Code, run `claude`, paste this whole file. It is a **read-only audit**: walk the codebase, score every item, cite evidence, and report — **do not change any code** until I explicitly say "remediate". Tender GEM/2026/B/7297343 · Appendix C UC III · D.2 PoC (10 marks) · Bid §8.5 · reference bar = our UC1 build.

---

## EXECUTION PROTOCOL (do this)
1. Inventory the repo (tree, stack, services, models, ArcGIS usage, simulators, tests, docs).
2. For **every checklist item below**, determine status and **cite evidence as `path:line`** (or "none found"). Do not assume an item passes because it's plausible — open the file and verify.
3. Emit results as a table per section:
   `ID | Item | Status | Evidence | Gap | Fix | Effort(S/M/L)`
   Status = ✅ PASS · 🟡 PARTIAL · ❌ FAIL · ⬜ MISSING.
4. After all sections, output:
   - **Scorecard** — pass-rate % per section.
   - **🔴 Production blockers** — items that must be green before this is production-grade (esp. anything in SCOPE/SCORE/AIML/SEC).
   - **Prioritised remediation backlog** — ordered, smallest-blast-radius first, with the requirement each unblocks.
   - **Verdict** — "Production-grade ✅" or "Not yet ❌" + the 3–5 highest-leverage fixes.
5. Write the full result to `docs/UC3_PRODUCTION_AUDIT.md`. Then stop and wait.

---

## §1 SCOPE — Appendix C Use Case III coverage
**Requirements (pp. 42–44):**
- `SCOPE-R1` Scalable **mobile app + DT module** with real-time **routing guidance**, **parking availability within the geo-fenced port area**, and **congestion heatmap** of approach routes.
- `SCOPE-R2` **PDP augmented with face recognition** + **Vahan & Sarathi** API integration.
- `SCOPE-R3` **Empty-container supply–demand optimisation** with probable allocation across fleet owners / shipping line / CFS / ECD; equivalent for trucks/trailers of **oil tankers, break-bulk, cement bowsers**.
- `SCOPE-R4` **Alerts & notifications for Customs** (compliance / flags).
- `SCOPE-R5` **Vehicle & container ID, e-seal, Form 13, weighbridge, ICEGATE** capture for **Auto-LEO**.
- `SCOPE-R6` **Carbon-emissions calculation** from fleet-transporter APIs + CPP/parking dwell.
- `SCOPE-R7` **AI video analytics**: detection, classification, **OCR**, trajectory tracking, **density heatmaps**, **ETA predictions**.
- `SCOPE-R8` **Geofencing + no-parking-zone violation detection** with automated alerts.

**Intended use:**
- `SCOPE-IU1` Monitor/optimise truck flow across **all entry/exit gates + connecting roads** from the **DTCCC**.
- `SCOPE-IU2` Driver **mobile app + SMS**: routing, parking availability, in-transit monitoring, real-time alerts, fleet monitoring, utilities visibility.
- `SCOPE-IU3` **End-to-end vehicle & container tracking** for stakeholders.
- `SCOPE-IU4` **360° heatmap + pain-points** to traffic control via UI **and** App.

**KPIs (acceptance criteria, with % improvement vs baseline):**
- `SCOPE-K1` Gate Queue Wait Time · `SCOPE-K2` Average Gate Transaction Time · `SCOPE-K3` TRT for empty containers from ECD · `SCOPE-K4` Turn-Around-Time Inside Port.

## §2 SCORING — D.2 sub-criteria each independently demonstrable (2 marks each)
- `SCORE-1` Solution approach / methodology + **assumptions listed in-app/docs**.
- `SCORE-2` **AI/ML tools** usage is real and visible.
- `SCORE-3` **API/data-integration plan + fallback on data unavailability** is demonstrable live.
- `SCORE-4` **Dashboard view & KPI monitoring** present.
- `SCORE-5` **What-if scenarios + automated reactive workflow** present.

## §3 BID — §8.5 commitment fidelity (we are held to these)
- `BID-DS` Data sources: ANPR/OCR from Indian-plate corpora at port conditions (dust/fog/night); Vahan/Sarathi/FastTag public-schema sim; RFID via NLDS readers; Trucking-App telemetry **20,000→30,000+**.
- `BID-AI` Models: ANPR **CNN+CRNN ≥95%**; congestion **GNN+LSTM F1 ≥0.85** over gates→Karal Phata ~40 km; **ByteTrack + autoencoder** anomaly (wrong-way/abandoned/illegal-parking/route-deviation); driver ETA/advisory engine.
- `BID-FB` Fallbacks: camera **live→cached→synthetic**; Vahan/Sarathi/FastTag **live→cached KYC→PROVISIONAL (24-hr cure)**; Trucking-App **GPS→ULIP→web check-in**.
- `BID-DASH` 40-km corridor heatmap; geofence alerts with **duration-based escalation**; **reports to traffic-police authorities**.
- `BID-SCN` **TFC-1** Gate Closure; **TFC-2** Wrong-Way @ Karal Phata; **TFC-3** Cargo-Surge cross-twin (consumes UC2 DPD release).

## §4 AIML — AI/ML production-readiness
- `AI-1` Each model exists as trainable code (not a stub) with a documented **eval set**.
- `AI-2` `metrics.json` (or equivalent) **asserts thresholds**: OCR ≥95%, congestion F1 ≥0.85; tests fail if unmet.
- `AI-3` Models served as **microservices behind the gateway** — UI never calls a model directly.
- `AI-4` Inference latency measured and acceptable for real-time gate/road use.
- `AI-5` Model artifacts **versioned**; training **reproducible** (seed + data manifest).
- `AI-6` Graceful behaviour when a model is unavailable (degraded mode, not crash).

## §5 GIS — ArcGIS spatial spine
- `GIS-1` ArcGIS Maps SDK for JS **5.x web components** (`<arcgis-map>`/`<arcgis-scene>`), no deprecated widget classes.
- `GIS-2` Operational layers present: **gates, port-road network (congestion-weighted), geofences/no-parking zones, congestion heatmap, parking facilities, corridor**.
- `GIS-3` Congestion **overlaid on the GIS layer as a heatmap** (explicit Appendix C requirement).
- `GIS-4` **Live positions via Stream Layer** (GeoEvent/Velocity) — UC1 pattern.
- `GIS-5` Map tools: layer toggle + Calcite legend, **time slider**, popups; reroute/violation rendered spatially.
- `GIS-6` **Embed path** into the existing ArcGIS Dashboards app (Embedded Content or Experience Builder widget), sharing the WebMap item.

## §6 DATA — connectors, adapter, fallback
- `DATA-1` Single **typed data adapter** with `MockAdapter`+`LiveAdapter` behind one interface (`DATA_MODE=mock|live`).
- `DATA-2` `npm run dev` runs the full app in **mock mode with zero credentials**.
- `DATA-3` Connectors built to **real contracts** (Vahan/Sarathi/FastTag/ULIP/ICEGATE/RFID) with sim fallback; UI never hits external APIs directly.
- `DATA-4` **Per-source Health Cards** (last good poll, error count, GREEN/AMBER/RED, mode badge) + Operator Banner on degradation.
- `DATA-5` The **three fallback chains** (`BID-FB`) implemented and switchable.
- `DATA-6` Ingestion follows **Bronze→Silver→Gold**; raw payloads retained for audit (`rawRef`).

## §7 SIM — simulators + demo console
- `SIM-1` Schema-accurate generators for every UC3 feed (ANPR, VehicleTrack, TruckTelemetry, Vahan/Sarathi/FastTag, RFID, GateTxn, Parking, Weighbridge, e-seal/Form13/ICEGATE, EmptyContainerMove, Carbon, FaceVerification, GeofenceViolation).
- `SIM-2` **Faithful**: sim events publish to the **same event backbone** as live; dashboard can't tell sim from live except via Health-Card badge.
- `SIM-3` **Deterministic** (seeded) → identical replay; **offline-first** (network-disabled run works).
- `SIM-4` **Corridor traffic model** (gates→Karal Phata 40 km graph, density-driven slowdown) feeds the congestion model with real signal.
- `SIM-5` **Fleet scale 20k→30k** via statistical model (no per-tick object explosion); sustains 30k without choking.
- `SIM-6` **Demo console** (Calcite) with feed switches, demo clock 1×–60×, event injectors, **TFC-1/2/3 triggers**, **fault-injection toggles**, realism sliders (OCR/anomaly/congestion/fleet), recorder/runbooks, status read-outs.
- `SIM-7` `scripts/poc-selftest` asserts each feed, scenario, fault chain, and the offline + 30k checks.

## §8 APP — driver mobile + web
- `APP-1` **Mobile app** (React Native/Expo) exists with routing guidance, parking availability, in-transit monitoring, alerts, fleet/utilities visibility.
- `APP-2` **Web check-in** path (the GPS→ULIP→**web check-in** fallback tier) exists.
- `APP-3` **SMS** advisory path present (or clearly stubbed with provider seam).
- `APP-4` App + web **share the same API layer** and run against the mock adapter.

## §9 KPI — KPI engine
- `KPI-1` Pure, **unit-tested** functions for `SCOPE-K1..K4` + queue length / dwell / gate throughput.
- `KPI-2` **Baseline config** + each KPI returns **% improvement vs baseline** with `{value,target,deltaPct,trend[]}`.
- `KPI-3` KPIs render on the dashboard with trend + target.

## §10 NOTIF — notifications & alerts
- `NOTIF-1` **Customs alerts/flags** notifications.
- `NOTIF-2` **Geofence/no-parking violation** alerts with **duration-based escalation**.
- `NOTIF-3` **Anomaly alerts** (wrong-way etc.) carry a **snapshot/photo reference** + route to Traffic Cell.
- `NOTIF-4` **Traffic-police report** export/feed.
- `NOTIF-5` Notifications are **role-filtered, multilingual, ack-tracked**.

## §11 SEC — RBAC, security, DPDP
- `SEC-1` **Roles** (JNPA Traffic, Terminal Ops, Customs, Traffic Police, Trucker/Driver, DTCCC admin) with scoped views/data.
- `SEC-2` Gateway: **OAuth2/OIDC + JWT**, OWASP API Top-10 ruleset, per-consumer rate limits, TLS.
- `SEC-3` **DPDP**: **face data synthetic/consented only** in PoC, documented in `ASSUMPTIONS.md`; purpose-limitation at Silver→Gold; real biometrics gated post-award.
- `SEC-4` **Audit logging** present; secrets only in `.env` (+ committed `.env.example`), none hard-coded.

## §12 ENG — engineering quality (UC1 parity)
- `ENG-1` TypeScript **strict**; ESLint + Prettier configured and clean.
- `ENG-2` Tests: unit (KPI, adapter contract), **model metric tests**, mapper/golden tests; meaningful coverage on core logic.
- `ENG-3` **CI** runs build + lint + tests.
- `ENG-4` Graceful **empty/error states** everywhere (Calcite notices, never blank panels).
- `ENG-5` **Responsive** (tablet) + **a11y** (keyboard focus, reduced-motion).
- `ENG-6` **i18n** Hindi / English / Marathi scaffolding.
- `ENG-7` No colour literals outside `tokens.ts`; Calcite dark theme tokens.
- `ENG-8` `docker compose up` brings the stack up (services + sim mode).
- `ENG-9` `README` (run, simulator setup, live-switch credentials, ArcGIS embed) is accurate.

## §13 XTWIN — cross-twin interdependency
- `XT-1` A **shared cross-twin event contract** is defined once (shared `schemas` package).
- `XT-2` **TFC-3 consumes the UC2 DPD-release event**; UC3 emits/accepts its side of the contract.
- `XT-3` The cross-twin trigger is fireable from the shared demo console.

## §14 DOCS — evaluator evidence
- `DOC-1` `docs/COVERAGE.md` maps **every Appendix C requirement / KPI / D.2 sub-criterion / §8.5 clause** to code + test + status.
- `DOC-2` `docs/ASSUMPTIONS.md` lists every assumed/synthetic dataset with justification.
- `DOC-3` `docs/KPI_DEFINITIONS.md` with formulas + baselines.
- `DOC-4` A **demo runbook** (5-min & 15-min) exists and matches the console runbooks.

---

**Run the audit now (read-only). Produce the per-section tables, the scorecard, the production-blocker list, the remediation backlog, and the verdict; write it to `docs/UC3_PRODUCTION_AUDIT.md`; then stop and wait for my instruction.**
