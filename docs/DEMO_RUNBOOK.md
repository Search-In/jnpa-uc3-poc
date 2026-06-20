# UC3 Demo Runbook — 5-minute & 15-minute scripts

> For the JNPA UC-III final demo before the evaluation committee. Every step names
> the **exact screen + button** and the **expected on-screen result**, so a
> presenter can run the demo from this doc with **zero improvisation**. Scenarios
> are driven from the **What-If Console** (the wired surface) — *not* the Demo
> Console's roadmap controls.
>
> Default mode is **mock** (zero credentials, deterministic, offline-capable), so
> the demo is byte-stable and never depends on a live upstream. If asked, you can
> show the same flow against the live stack.

---

## 0. Pre-flight (do this 10 minutes before)

| # | Action | Expected |
|---|---|---|
| 0.1 | `make up` (or `docker compose up -d`), wait ~60 s | All services healthy (`docker compose ps`) |
| 0.2 | Open the dashboard at **http://localhost:3000** | Live Operations loads; corridor map + KPI strip render |
| 0.3 | Confirm mode badge | Header / System Health shows **MOCK** mode |
| 0.4 | `make demo-reset` | Clean baseline (ephemeral data wiped, trained models kept) |
| 0.5 | (optional) `python scripts/poc_selftest.py` | 22/23 checks, 0 required failing; B.1 honest WARN (F1 0.8411) |

> **Honesty note for Q&A:** on a CPU-only host the dashboard shows a "DEGRADED
> MODEL" notice on the ANPR/OCR card (fallback ~11%, not the committed ≥95%) and
> the congestion F1 reads 0.8411 (below the 0.85 target). These are **enforced by
> tests** and labelled everywhere — say plainly they are post-award weights/tuning
> items; the architectures are real. Do not claim the headline numbers are met.

---

## 1. The 5-minute script — "one closed gate, the twin reacts"

Goal: show the digital twin detect a disruption and **automatically re-route**,
with a measurable KPI improvement.

| # | Screen → action | Say | Expected on-screen |
|---|---|---|---|
| 1 | **Live Operations** | "This is the DTCCC control room — the 40-km NH-348 corridor from JNPA gates to Karal Phata, live truck positions, the congestion heatmap, and the KPI strip." | Map with corridor + gates + heatmap; KPI strip with value/target/Δ-vs-baseline + sparkline |
| 2 | **Live Operations → map LayerList (top-left)** | "Operators can toggle any operational layer — gates, road network, geofences, heatmap, parking, corridor." | Toggling a layer shows/hides it |
| 3 | **What-If Console → TFC-1** ("Close G-NSICT; forecaster predicts spillover; trucks auto-re-route; TAS slots rescheduled") → click **Run** | "I'll close Gate NSICT. Watch the twin react." | The storyline paints live, step by step: gate CLOSED → spillover predicted → trucks re-routed → TAS slots rescheduled |
| 4 | back to **Live Operations** | "Trucks bound for the closed gate are now re-routed; the queue at the closed gate drains to neighbours." | Truck markers shift; gate queue redistributes on the map |
| 5 | **Live Operations → KPI strip** | "Gate queue wait drops against the baseline — the % improvement is the acceptance KPI." | `gate_queue_wait` shows an improved value + negative Δ% (improvement) |
| 6 | **What-If Console → Reset to baseline** | "And it's fully reversible for the next run." | Storyline shows "Reset to baseline complete"; map returns to baseline |

**Fallback if a step stalls:** the flow is deterministic in mock mode; if the map
doesn't update, re-run TFC-1 — state is idempotent and `make demo-reset` always
returns a clean baseline.

---

## 2. The 15-minute script — full reactive twin + cross-twin

Runs the 5-minute script, then adds the anomaly path, the cross-twin event, and
the evaluator-evidence surfaces.

### 2A. (0–5 min) Run the 5-minute script above (TFC-1).

### 2B. (5–9 min) TFC-2 — wrong-way anomaly + enforcement

| # | Screen → action | Expected |
|---|---|---|
| 1 | **What-If Console → TFC-2** ("Inject a wrong-way track at Karal Phata; anomaly fires; e-Challan issued with evidence") → **Run** | Storyline: wrong-way track injected at C-KARAL-EXIT → anomaly detector fires → e-Challan issued |
| 2 | **Live Operations / alerts feed** | A `WRONG_WAY` alert appears with a snapshot/evidence reference |
| 3 | **Traffic-Police Reports** | The incident is listed; **export PDF** produces a pre-filled e-Challan |
| 4 | (if auth demo) sign in as **police** vs **driver** | Police sees the reports screen; driver's nav does **not** offer it (RBAC) |

### 2C. (9–13 min) TFC-3 — cross-twin DPD release (UC-II → UC-III)

| # | Screen → action | Expected |
|---|---|---|
| 1 | **What-If Console → TFC-3** ("UC-II DPD release spike (2.5×) → corridor demand surge; forecaster build-up; gate-slot reissue") → **Run** | Storyline shows the **cross-twin** link "UC-II → UC-III": a DPD-release spike is consumed, demand surges, the forecaster flags build-up, gate slots are reissued |
| 2 | **Live Operations** | Heatmap intensifies along the corridor; KPI strip reflects the surge then the reactive mitigation |

> This is the genuinely novel capability — UC-III **consuming** a UC-II event via
> a shared cross-twin contract. Emphasise it.

### 2D. (13–15 min) Evaluator evidence

| # | Screen → action | Expected |
|---|---|---|
| 1 | **System Health** | Per-source Health Cards (last good poll, GREEN/AMBER/RED, mode badge); the three fallback chains |
| 2 | **Demo Console → fault injection** (camera / Vahan / trucks) | Force a rung → Health Card flips + Operator Banner raises (LIVE→CACHED→SYNTHETIC etc.) |
| 3 | **Demo Console → realism panel** | OCR/F1 cards show the **honest** numbers with the DEGRADED notice — point to this as integrity, not a gap |
| 4 | Mention the docs | `docs/COVERAGE.md` (requirement→code→test→status), `docs/ASSUMPTIONS.md`, `docs/KPI_DEFINITIONS.md`, `docs/UC3_PRODUCTION_AUDIT.md` |

---

## 3. Q&A quick-answers (pre-loaded)

- **"Is the map ArcGIS?"** Yes — Live Operations uses ArcGIS Maps SDK 4.31 web
  components (`<arcgis-map>`); the geofence editor uses MapLibre + terra-draw for
  polygon drawing. Both are deliberate.
- **"Is OCR really 95%?"** Not on this CPU host — it's running the deterministic
  fallback (~11%), shown honestly with a DEGRADED banner. The CRNN architecture is
  real; ≥95% is a post-award weights/real-data item, and a test enforces it once
  weights load.
- **"Congestion F1?"** 0.8411 vs the 0.85 target — a tuning item, enforced by an
  xfail-strict test so it can't silently drift or be silently "fixed."
- **"Is the ETA AI?"** No — it's a heuristic (OSRM + dead-reckoning); we label it
  as such. A learned ETA head is post-award.
- **"Security?"** Flag-gated JWT + 6-role RBAC + rate limiting on the gateway
  (`AUTH_ENABLED`), DPDP purpose-limitation + synthetic-only enforced in code,
  no hard-coded infra secrets. OFF for this demo profile for speed; ON in prod.
- **"SMS?"** Provider seam wired to the advisory fan-out (env-gated); a real
  provider is one env var away.

---

## 4. Teardown

| Action | Effect |
|---|---|
| `make demo-reset` | Clean baseline for the next run |
| `make down` | Stop + remove the stack |
