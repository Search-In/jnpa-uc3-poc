# JNPA UC-III — Production-Grade Audit Checklist

## For Claude Code, executed against an existing PoC codebase

**Project:** GeM/2026/B/7297343 — JNPA AI/ML-Enabled, Cyber-Aware Digital Twin
**Use Case under audit:** III — Traffic Monitoring & Vehicular Decongestion
**Auditor role:** Claude Code, running read-only against the repo. **Do not modify code during the audit.** All remediation happens in a second pass.

---

## How to use this checklist

1. Open VS Code in your existing PoC repo root.
2. Launch Claude Code in that folder.
3. Paste the **Master Audit Prompt** below in full. Claude Code will then:
   - Read this entire checklist
   - Execute every check in order
   - Write per-check evidence into `./audit/evidence/<CHECK_ID>.txt`
   - Produce a final report at `./audit/AUDIT_REPORT.md`
   - Produce a remediation plan at `./audit/REMEDIATION_PLAN.md`
4. Review the report. **Do not let Claude Code start remediation until you have read and approved the plan.**
5. After approval, paste the **Remediation Prompt** at the end of this file to begin fixes.

Each check has the form:

```
CHECK ID: <PHASE>-<SUB>-<NN>
Title: <one line>
Severity: BLOCKER | CRITICAL | HIGH | MEDIUM | LOW
Sub-criterion / Standard: <bid clause or compliance frame>
Verify by: <commands / patterns / file paths>
Pass criterion: <unambiguous>
Evidence: <what to save>
```

**Severity ladder** (Claude Code must respect these labels in the remediation plan):

| Severity | Meaning | Must fix before |
|---|---|---|
| BLOCKER | Bid evaluator will mark the sub-criterion as not demonstrated, or the demo will visibly fail | Pre-evaluation demo |
| CRITICAL | Cannot go to production; fails Pre-Production Sign-off (PSO) gate | Go-live |
| HIGH | Material production risk; will surface during 5-yr AMC | First 90 days of operations |
| MEDIUM | Quality bar; not blocking but visible to assessors | Year-1 of AMC |
| LOW | Hygiene; would be flagged in a third-party audit | Best-effort |

---

# MASTER AUDIT PROMPT

> Paste everything between the two horizontal rules into Claude Code in one go. Do not edit anything inside the block.

---

```text
You are operating as a READ-ONLY AUDITOR on the JNPA UC-III PoC codebase
located at the current working directory. You will not modify any source
code, configuration, or data files during this audit.

OBJECTIVE
Produce three artefacts:
  1) ./audit/AUDIT_REPORT.md
  2) ./audit/REMEDIATION_PLAN.md
  3) ./audit/evidence/<CHECK_ID>.txt for every check executed

INPUTS YOU MUST READ FIRST (in this exact order)
  a) ./docs/CLAUDE_CODE_PROMPTS.md  (the original build prompt pack)
  b) ./README.md                    (current repo state)
  c) ./docker-compose.yml           (service topology)
  d) The audit checklist document the user has open in VS Code titled
     "JNPA UC-III — Production-Grade Audit Checklist" — they will paste
     its full contents below this prompt as message 2.

RULES OF EXECUTION
  - Run every check in the checklist, in the order given, including
    those marked N/A — for N/A you record the reason.
  - For each check produce one row in AUDIT_REPORT.md with the columns:
      CHECK_ID | Title | Severity | Result | Evidence | Notes
    where Result is one of PASS / FAIL / PARTIAL / N/A / UNABLE_TO_VERIFY.
  - For FAIL and PARTIAL results write a paragraph under the row
    describing exactly what is missing or wrong, and what the production-
    grade target looks like.
  - For UNABLE_TO_VERIFY (e.g. service not running, missing dataset),
    list what is needed to verify and stop there for that check — do NOT
    fabricate evidence.
  - Save raw command output, file extracts, and grep hits to
    ./audit/evidence/<CHECK_ID>.txt. Keep these short and quotable.
  - Where a check requires running services (e.g. hitting localhost
    endpoints), first run `docker compose ps`. If the stack is not up,
    record UNABLE_TO_VERIFY with the reason and move on — do NOT start
    the stack yourself unless the user has told you to.
  - You may install only audit-time tooling: ripgrep, jq, trivy (image
    scan), cyclonedx-bom (SBOM), pip-audit, npm audit, gitleaks. Nothing
    else — and only via system package manager, not into the project.

OUTPUTS

(1) ./audit/AUDIT_REPORT.md MUST have this top-level structure:
    # JNPA UC-III Production Audit Report
    ## Repository fingerprint  (git commit, branch, date, services listed)
    ## Executive summary       (one paragraph + severity histogram table)
    ## Phase 0 — Inventory
    ## Phase 1 — Functional alignment to bid sub-criteria 8.5.1–8.5.5
    ## Phase 2 — Production-grade engineering
    ## Phase 3 — Compliance
    ## Phase 4 — Integration completeness
    ## Cross-cutting findings
    ## Appendix A — Raw check results table
    ## Appendix B — Evidence index

(2) ./audit/REMEDIATION_PLAN.md MUST have this structure:
    # Remediation Plan
    ## Severity histogram
    ## BLOCKER findings — fix before evaluator demo
        (each item shows: CHECK_ID, what to do, where, owner, ETA-buckets)
    ## CRITICAL findings — fix before go-live
    ## HIGH findings — fix in first 90 days of AMC
    ## MEDIUM findings — fix in Year-1 AMC
    ## LOW findings — hygiene backlog
    ## Suggested order of execution (DAG of dependencies)
    ## Re-audit trigger conditions

OUT-OF-SCOPE FOR THIS PASS
  - You will not write any application code.
  - You will not modify the Dockerfile, compose, or .env files.
  - You will not push, commit, or open PRs.
  - You will not run destructive commands (no docker compose down -v,
    no DROP, no rm -rf outside ./audit/).

WHEN DONE
Print to chat:
  AUDIT COMPLETE. <N_pass> PASS, <N_fail> FAIL, <N_partial> PARTIAL,
  <N_na> N/A, <N_unable> UNABLE_TO_VERIFY.
  See ./audit/AUDIT_REPORT.md and ./audit/REMEDIATION_PLAN.md.
Then stop. Do not begin remediation.
```

---

# BID CONTEXT — DO NOT SKIP

Claude Code uses these constants in its checks:

| Item | Value |
|---|---|
| GeM ref | GEM/2026/B/7297343 |
| Use Case III scope clauses | Corrigendum 3 Appendix C §2.3, pages 42–44 |
| ANPR / OCR accuracy target | ≥ 95% under dust, fog, night |
| Congestion onset F1 target | ≥ 0.85 |
| Corridor | NH-348, JNPA Gates → Karal Phata, 35–40 km |
| Trucking-App scale | 20,000 concurrent, scalable to 30,000+ |
| Hardware footprint | ~80 multi-lane structures + ~35 single-lane poles + ~5 weighbridge nodes |
| AMC term | 5 years post-implementation |
| Operating envelope | 24×7, salt-spray/fog/corrosion-resilient, EoL ≥ 10 yrs |
| Integrations specified | TOS, ULIP/NLP, PCS, LDB, FOIS, IWAI, Vahan, Sarathi, FastTag, Carbon Emissions, DTCS, weighbridge, boom barriers, TAS |
| Compliance frames | IEC 62443, ISO 27001, CERT-In, DPDP Act 2023, MeitY GCC (if cloud), NCIIPC, MII Order, STQC/BIS, GeM GTC, Arbitration and Conciliation Act 1996 |
| Reactive scenarios | TFC-1 (gate closure), TFC-2 (wrong-way), TFC-3 (cargo-surge cross-twin to UC-II) |

---

# PHASE 0 — INVENTORY (run first, before any FAIL/PASS judgements)

| CHECK ID | Title | Verify by |
|---|---|---|
| INV-01 | Repo fingerprint captured | `git rev-parse HEAD`, `git branch --show-current`, `git status --short`, date in UTC |
| INV-02 | Compose service list | `docker compose config --services` — record every service name |
| INV-03 | Running services list | `docker compose ps --format json` — record state, health, ports |
| INV-04 | Language footprint | `tokei .` or `cloc --vcs=git .` — record LOC by language |
| INV-05 | Dependency lockfiles present | find `**/pyproject.toml`, `**/poetry.lock`, `**/requirements*.txt`, `**/package-lock.json`, `**/pnpm-lock.yaml` |
| INV-06 | Top-level folder map | `ls -la` and `tree -L 3 -I 'node_modules\|.git\|venv\|.venv'` |
| INV-07 | Environment template present | `.env.local.example` exists with no real secrets (run `gitleaks detect` for sanity) |
| INV-08 | Build prompt pack present | `docs/CLAUDE_CODE_PROMPTS.md` exists; record commit hash where it last changed |
| INV-09 | Audit folder bootstrap | `mkdir -p audit/evidence` (only writable area in this pass) |
| INV-10 | Test directories present | `find . -type d -name tests -not -path '*/node_modules/*'` |

All INV checks must PASS or the auditor must STOP and report missing repository structure before going further.

---

# PHASE 1 — FUNCTIONAL ALIGNMENT

> Each check is mapped to a specific clause of Corrigendum 3 Appendix C §2.3, the BID DOC §III row, or the build prompt that originally produced the code.

## 1A — Sub-Criterion 8.5.1: Multi-Source Data Integration

### FUNC-1A-01 — ANPR ingest emits to Kafka topic `anpr.reads`
- **Severity:** BLOCKER (the entire pipeline depends on this)
- **Verify by:**
  - File: `ingest/anpr/src/anpr_ingest/emit.py`
  - Grep: `rg -n "anpr\.reads" ingest/anpr/`
  - Runtime: `docker exec kafka kafka-topics --bootstrap-server kafka:9092 --list | grep '^anpr.reads$'`
  - Schema: payload validates against `jnpa_shared.schemas.AnprRead` (check the import is used in `emit.py`)
- **Pass criterion:** topic exists, producer code path imports and constructs `AnprRead` from schemas, at least 1 message produced in the last hour (`kafka-console-consumer ... --max-messages 1 --timeout-ms 5000`).
- **Evidence:** save the grep + the consumed message JSON.

### FUNC-1A-02 — Indian-plate corpus used for fine-tune AND held-out eval
- **Severity:** CRITICAL
- **Verify by:**
  - Path: `ai/anpr/eval/` exists with a non-empty held-out set
  - File: `ai/anpr/src/anpr/finetune.py` references one of: Kaggle Indian Vehicle Dataset, Indian_LPR (sanchit2843), Rishit-dagli/Vehicle-License-Plate-Detection
  - Code: separate `train/`, `val/`, `test/` splits with documented split ratios
- **Pass criterion:** Eval set is held out (never seen by fine-tune script — check by hashing filenames).
- **Evidence:** split sizes, hashes of test-set first 5 files.

### FUNC-1A-03 — ANPR degradation slices (dust, fog, night) implemented
- **Severity:** CRITICAL — the 95% claim is conditioned on these
- **Verify by:**
  - File: `ai/anpr/src/anpr/degradation.py`
  - Function: each of `apply_dust()`, `apply_fog()`, `apply_lowlight()` exists
  - Eval: `bench.py` runs all three slices and writes accuracy per slice into `metrics.json`
- **Pass criterion:** `metrics.json` has fields `ocr_clean`, `ocr_dust`, `ocr_fog`, `ocr_night` with last-run values; combined weighted ≥ 95.0
- **Evidence:** copy of `metrics.json`.

### FUNC-1A-04 — Vahan / Sarathi / FastTag simulator schema-faithful
- **Severity:** CRITICAL
- **Verify by:**
  - Service: `vahan-sim` exposes `/vahan/rc/{plate}`, `/sarathi/dl/{dl}`, `/fastag/balance/{plate}`
  - Schema: response fields match the Parivahan-published field set (must include `rc_number`, `vehicle_class`, `fuel_type`, `fitness_valid_to`, `puc_valid_to`, `insurance_valid_to`, `registration_date`, `state`, `rto_code`, `blacklist_status`)
  - Test fixture: `data/fixtures/known_plates.json` exists with ≥ 50 plates
  - Latency: p95 < 400 ms over 1000 requests
- **Pass criterion:** all four sub-checks PASS.
- **Evidence:** sample response JSON, fixture count, latency histogram.

### FUNC-1A-05 — Vahan **live** adapter wired to Surepass (or equivalent)
- **Severity:** HIGH (PoC default is simulator, but the live path must exist)
- **Verify by:**
  - Service: `vahan-live` present in compose
  - Code path: explicit reference to `https://kyc-api.surepass.io/api/v1/rc/rc-full`
  - Without `SUREPASS_API_TOKEN`, returns HTTP 503 with `{"error":"live_disabled"}`
- **Pass criterion:** with token absent, 503; with stubbed token, the request is **constructed correctly** (we don't have to send it)
- **Evidence:** request snippet, the 503 response.

### FUNC-1A-06 — RFID emulator publishes to MQTT with stable tag pool
- **Severity:** CRITICAL
- **Verify by:**
  - File: `ingest/rfid/emulator.py` — fixed pool of 12,000 tag IDs declared
  - MQTT: `mosquitto_sub -h localhost -t 'rfid/readers/+' -C 20` returns 20 messages
  - DB: `select count(*) from jnpa.rfid_reads where ts > now() - interval '5 minutes';` returns > 0
- **Pass criterion:** all three PASS.
- **Evidence:** 20 MQTT messages, DB row count.

### FUNC-1A-07 — RFID + ANPR correlator emits `vehicle.confirmed`
- **Severity:** HIGH — boom-barrier decisioning depends on this
- **Verify by:**
  - File: `ingest/rfid/correlator.py` joins by 5 s window
  - Topic: `vehicle.confirmed` exists in Kafka
  - Latency: p95 join latency < 1 s
- **Pass criterion:** topic non-empty, latency under target.
- **Evidence:** sample confirmed message, latency timing.

### FUNC-1A-08 — Trucking-App telemetry sim hits scale envelope
- **Severity:** CRITICAL — explicitly called out in the bid (20k → 30k)
- **Verify by:**
  - Service: `truck-sim` running
  - Endpoint: `curl http://localhost:8240/devices | jq '.population'` returns ≥ 20000 (or documented dev-mode override)
  - Scale test: `POST /devices/scale {"target":30000}` reaches population in ≤ 60 s
  - MQTT throughput: `mosquitto_sub -h localhost -t 'trucks/+/telemetry' -C 500` completes in ≤ 30 s at full scale
- **Pass criterion:** all three.
- **Evidence:** scale test transcript, message count.

### FUNC-1A-09 — Routing engine cascade present (OSRM → HERE → dead reckoning)
- **Severity:** HIGH
- **Verify by:**
  - Grep: `rg -n "router\.project-osrm\.org|here\.com/routing" ingest/trucking_app/`
  - Fallback: dead-reckoning function exists and is unit-tested
- **Pass criterion:** all three paths represented in code.
- **Evidence:** grep hits, test name.

### FUNC-1A-10 — Weather context joined to ANPR events
- **Severity:** HIGH — required to label "degraded" frames truthfully
- **Verify by:**
  - File: `ingest/anpr/src/anpr_ingest/weather.py`
  - Pulls OpenWeatherMap on a schedule
  - Each `AnprRead` carries `weather` ∈ {clear, rain, fog, dust}
- **Pass criterion:** field populated; not always "clear".
- **Evidence:** distribution from `select weather, count(*) from jnpa.anpr_reads group by 1`.

### FUNC-1A-11 — ULIP/NLP schema compatibility surface present
- **Severity:** HIGH — the bid commits to "Vahan + Sarathi + FastTag" via ULIP per Appendix A2
- **Verify by:**
  - Code: ingest exposes a `/api/ulip/proxy` route (gateway side) that documents the ULIP request/response shape
  - Schema files in `shared/jnpa_shared/schemas.py` carry `UlipMessage` type
- **Pass criterion:** schema present even if not connected to live ULIP — the bid is "schema compatibility" for now.
- **Evidence:** schema dump.

### FUNC-1A-12 — 360°-camera + e-seal ingest path scoped
- **Severity:** HIGH — explicit bid item (DTCS integration + 360° plate + e-seal)
- **Verify by:** at minimum a stub camera entry of role='360' and a topic `eseal.reads` declared, with a placeholder consumer that logs "ESEAL ingest reserved for post-award DTCS integration"
- **Pass criterion:** stub present; the auditor records this as known carve-out so the evaluator sees we knew about it.
- **Evidence:** stub file path, log line.

---

## 1B — Sub-Criterion 8.5.2: AI/ML Tools Usage

### FUNC-1B-01 — ANPR engine architecture matches spec
- **Severity:** BLOCKER
- **Verify by:**
  - `ai/anpr/src/anpr/detect.py` uses YOLOv8 (CNN detector)
  - `ai/anpr/src/anpr/ocr.py` uses CRNN-style sequence model (PaddleOCR PP-OCRv4 satisfies this)
  - Post-processor `postprocess.py` enforces the Indian regex + BH-series
- **Pass criterion:** all three.
- **Evidence:** class names, regex extract.

### FUNC-1B-02 — ANPR exact-match accuracy bench ≥ 95% (combined)
- **Severity:** BLOCKER — directly scored
- **Verify by:** `curl http://localhost:8301/eval` (or run `python -m anpr.eval`). Inspect `metrics.json` → weighted accuracy ≥ 0.95 AND `OCR_TARGET_MET=true`
- **Pass criterion:** value present and ≥ 95.0.
- **Evidence:** full metrics.json.

### FUNC-1B-03 — Congestion forecaster is GNN + LSTM
- **Severity:** BLOCKER
- **Verify by:**
  - `ai/congestion/model.py` uses `torch_geometric` (GraphSAGE / GCN / GAT)
  - `model.py` has an LSTM head over a 30-step window
- **Pass criterion:** both imports present, model classes wired.
- **Evidence:** class signature.

### FUNC-1B-04 — Congestion onset F1 ≥ 0.85
- **Severity:** BLOCKER
- **Verify by:** `curl http://localhost:8311/metrics | jq '.congestion_onset_f1'` ≥ 0.85
- **Pass criterion:** value present and ≥ 0.85; AND precision ≥ 0.80; AND recall ≥ 0.80.
- **Evidence:** full metrics block + class distribution of the test set.

### FUNC-1B-05 — Behavioural anomaly = ByteTrack + rule + autoencoder hybrid
- **Severity:** CRITICAL
- **Verify by:**
  - ByteTrack: `rg -n "bytetrack|ByteTrack" ai/anomaly/`
  - Rule engine: `ai/anomaly/rules/{wrongway,abandoned,parking,route_deviation}.py` each present
  - AE: `ai/anomaly/autoencoder/model.py` with a 1D-conv encoder/decoder
- **Pass criterion:** all three components present and at least one alert kind per rule has fired in test runs.
- **Evidence:** alert counts per kind from `select kind, count(*) from jnpa.alerts group by 1`.

### FUNC-1B-06 — Driver-side ETA / advisory engine present and pushable
- **Severity:** CRITICAL
- **Verify by:**
  - Endpoint: `POST /api/trucks/{id}/route` triggers re-route push
  - PWA: `mobile-pwa/` registers WebPush subscription against the gateway
  - End-to-end: TFC-1 scenario produces ≥ 1 push reaching a test device
- **Pass criterion:** all three.
- **Evidence:** push delivery receipt.

### FUNC-1B-07 — Model artefact storage with versioning
- **Severity:** HIGH (production-grade)
- **Verify by:** MinIO bucket `models/` has versioned objects for `anpr/`, `congestion/`, `anomaly/`. Each artefact has a sidecar manifest (`{name}/{version}/manifest.json`) with: trained_at, dataset_hash, metrics, git_commit
- **Pass criterion:** all three artefacts versioned; manifests present.
- **Evidence:** `mc ls -r minio/models/` output.

### FUNC-1B-08 — Training reproducibility
- **Severity:** HIGH
- **Verify by:**
  - Random seeds set in every train script (`random`, `numpy`, `torch`, `torch.cuda`)
  - Dataset hash recorded in artefact manifest
  - Train script accepts `--seed` CLI arg
- **Pass criterion:** all three.
- **Evidence:** grep hits.

### FUNC-1B-09 — Inference path observability
- **Severity:** HIGH
- **Verify by:** each AI service exposes `/metrics` (Prometheus) with at minimum: `inference_latency_seconds` histogram, `inference_requests_total` counter, `model_version` gauge label
- **Pass criterion:** all three counters present on all three services.
- **Evidence:** metric dump.

### FUNC-1B-10 — Drift detection scaffold present
- **Severity:** HIGH (AMC concern)
- **Verify by:** at minimum a scheduled job that compares last 24 h ANPR confidence distribution to a baseline; emits an alert kind=`MODEL_DRIFT_SUSPECTED` if KS-statistic > 0.2
- **Pass criterion:** job present and runnable, even if currently inactive.
- **Evidence:** cron / scheduler definition.

---

## 1C — Sub-Criterion 8.5.3: API / Data Integration & Fallback

### FUNC-1C-01 — Camera fallback chain LIVE → CACHED → SYNTHETIC implemented
- **Severity:** BLOCKER
- **Verify by:**
  - `gateway/fallback.py` exposes `decide_camera(camera_id) → DecisionPath`
  - Each tier explicitly tested with a kill-switch
- **Pass criterion:** three tiers in code; each tier observed in `/api/debug/decisions` ring buffer when triggered
- **Evidence:** decision-path log extracts for each tier.

### FUNC-1C-02 — Per-camera degradation visible
- **Severity:** CRITICAL
- **Verify by:** `GET /api/kpi/cameras` returns `[{camera_id, decision_path, last_ok, latency_p95}]` and the dashboard System Health panel renders this
- **Pass criterion:** endpoint exists, dashboard screen exists.
- **Evidence:** response payload + screenshot.

### FUNC-1C-03 — Vahan fallback chain LIVE_PRIMARY → LIVE_FALLBACK → CACHED → PROVISIONAL
- **Severity:** BLOCKER
- **Verify by:**
  - `gateway/fallback.py` implements all four tiers
  - PROVISIONAL writes `jnpa.vehicle_master` row with `provisional=true`, `provisional_until = now() + 24h`
  - Emits alert kind=`PROVISIONAL_VEHICLE`
- **Pass criterion:** all three.
- **Evidence:** sample provisional row, alert.

### FUNC-1C-04 — Trucking-App fallback chain APP_GPS → ULIP relay → web check-in
- **Severity:** CRITICAL
- **Verify by:**
  - Tertiary tier: a `/checkin` HTML page exists and is functional
  - Elevated-scrutiny alert kind=`ELEVATED_SCRUTINY` raised when chain falls past primary
  - Gate boom adds +5 s delay (configurable parameter exists)
- **Pass criterion:** all three.
- **Evidence:** page screenshot, alert sample.

### FUNC-1C-05 — Decision-path observability is queryable
- **Severity:** HIGH
- **Verify by:** `/api/debug/decisions` returns the last 1000 decisions with fields `{ts, api, key, decision_path, reason, latency_ms}`
- **Pass criterion:** endpoint present, ring buffer respected.
- **Evidence:** 5 sample decisions of different paths.

### FUNC-1C-06 — Cache TTLs are sane and documented
- **Severity:** MEDIUM
- **Verify by:** `rg -n "ex=|setex|expire" gateway/` and corresponding markdown doc in `docs/CACHE_POLICY.md` covering each cache key family
- **Pass criterion:** every cache key family has both an in-code TTL and a documented TTL — and they match.
- **Evidence:** mapping table.

### FUNC-1C-07 — Idempotent retries on external APIs
- **Severity:** HIGH
- **Verify by:** every external HTTP client uses an idempotency key (UUIDv4) on `POST`s and applies exponential back-off + jitter
- **Pass criterion:** retry decorator/middleware present, idempotency key in headers, no retry on 4xx except 408/425/429.
- **Evidence:** grep hits, sample logs.

### FUNC-1C-08 — Circuit breaker on each external API client
- **Severity:** HIGH
- **Verify by:** `pybreaker` or equivalent in `gateway/clients/*.py` with sensible thresholds (e.g., 5 fails / 30 s)
- **Pass criterion:** breaker present per client; metric `circuit_state{client="..."}` exposed
- **Evidence:** state metric snapshot.

### FUNC-1C-09 — Provisional cure-window job runs and resolves
- **Severity:** CRITICAL
- **Verify by:** scheduled job that flips `provisional=false` when verified within window, OR raises `PROVISIONAL_EXPIRED` alert when window elapses without verification
- **Pass criterion:** both pathways implemented.
- **Evidence:** test outputs.

---

## 1D — Sub-Criterion 8.5.4: Dashboard & KPI Monitoring

### FUNC-1D-01 — Live heatmap renders over the 40 km corridor
- **Severity:** BLOCKER
- **Verify by:** open `http://localhost:3000/live`; MapLibre canvas present with corridor polyline coloured by jam factor; basemap loaded
- **Pass criterion:** all three.
- **Evidence:** screenshot saved under `audit/evidence/FUNC-1D-01.png`.

### FUNC-1D-02 — Gate-wise throughput and queue length panels present
- **Severity:** CRITICAL
- **Verify by:** KPI row shows for each of G-NSICT, G-JNPCT, G-NSIGT, G-BMCT: avg dwell (60 min), throughput (60 min), current queue length
- **Pass criterion:** all three KPIs × 4 gates rendering live values.
- **Evidence:** screenshot.

### FUNC-1D-03 — Driver-advisory screen with re-route push
- **Severity:** CRITICAL
- **Verify by:** `/advisory` screen shows trucks in AT_GATE_QUEUE with ETA and re-route button; button triggers PWA notification
- **Pass criterion:** end-to-end works once.
- **Evidence:** before/after screenshot.

### FUNC-1D-04 — Geo-fencing editor with duration-based escalation rules
- **Severity:** CRITICAL — explicit bid item
- **Verify by:**
  - Editor screen allows polygon draw/edit (terra-draw or similar)
  - Escalation thresholds configurable (WARNING@5min, CRITICAL@15min, REPORT_TO_POLICE@30min)
  - Saved zones persisted to Postgres and read by anomaly service live
- **Pass criterion:** all three.
- **Evidence:** edited zone JSON, alert at each escalation level.

### FUNC-1D-05 — Photographic evidence chain to traffic police
- **Severity:** CRITICAL — explicit bid item (Appendix B item 15)
- **Verify by:**
  - Evidence frame stored in MinIO with content-addressed key (SHA256 in path)
  - Evidence URL is signed (presigned URL or token-gated)
  - Police-report PDF includes plate, RC info, time, location, evidence image
- **Pass criterion:** all three.
- **Evidence:** sample PDF, signed URL.

### FUNC-1D-06 — Reports exportable in evidence-grade format
- **Severity:** HIGH
- **Verify by:** PDF report carries: case ID, generation timestamp, generator identity, SHA256 of evidence frame, gateway signature line
- **Pass criterion:** all five fields present.
- **Evidence:** sample report.

### FUNC-1D-07 — Heatmap data source documents real vs simulated
- **Severity:** MEDIUM — bid integrity / honesty
- **Verify by:** each panel that uses simulated data carries a small "data: simulator (PoC)" chip
- **Pass criterion:** chip present where applicable.
- **Evidence:** screenshot.

### FUNC-1D-08 — Web Vitals on the dashboard
- **Severity:** MEDIUM
- **Verify by:** Lighthouse run on `/live` returns Performance ≥ 80, Accessibility ≥ 90, Best Practices ≥ 90
- **Pass criterion:** numeric.
- **Evidence:** Lighthouse JSON.

### FUNC-1D-09 — WebSocket alerts panel updates in < 2 s
- **Severity:** HIGH
- **Verify by:** trigger a synthetic alert via gateway; measure time-to-UI-render
- **Pass criterion:** p95 ≤ 2 s.
- **Evidence:** timing log.

### FUNC-1D-10 — Mobile (responsive) parity on the PWA
- **Severity:** HIGH
- **Verify by:** PWA passes Lighthouse PWA ≥ 90 on emulated mid-range Android
- **Pass criterion:** numeric.
- **Evidence:** Lighthouse JSON.

---

## 1E — Sub-Criterion 8.5.5: What-If Scenarios + Reactive Workflow

### FUNC-1E-01 — TFC-1 end-to-end runs and resets
- **Severity:** BLOCKER
- **Verify by:**
  - Trigger: `POST /scenarios/tfc1/run {"gate_id":"G-NSICT","duration_minutes":120}`
  - Steps observed: gate marked closed → synthetic queue at NSICT → forecaster predicts P≥0.7 at JNPCT/NSIGT within 15 min → trucking-app re-routes EN_ROUTE_TO_PORT trucks → TAS-mock marks slots RESCHEDULED
  - Reset: `POST /scenarios/tfc1/reset` restores baseline; gate state, injected trucks, alerts all cleaned
- **Pass criterion:** every step observed in scenarios timeline.
- **Evidence:** timeline JSON dump.

### FUNC-1E-02 — TFC-2 end-to-end runs and resets
- **Severity:** BLOCKER
- **Verify by:**
  - Trigger: `POST /scenarios/tfc2/run {"camera_id":"C-KARAL-EXIT"}`
  - Steps: synthetic wrong-way track injected → anomaly WRONG_WAY alert raised → e-Challan stub returns ID + PDF → dashboard plays evidence
- **Pass criterion:** all four steps.
- **Evidence:** alert + challan IDs.

### FUNC-1E-03 — TFC-3 end-to-end runs with cross-twin link visible
- **Severity:** BLOCKER
- **Verify by:**
  - Trigger: `POST /scenarios/tfc3/run {"dpd_release_spike":2.5}`
  - Cross-twin: synthetic event published on `cargo.dpd_release` (UC-II topic)
  - Reactive: forecaster predicts P≥0.6 on ≥ 5 segments; driver-advisory engine issues re-routes; PWAs receive pushes
- **Pass criterion:** all three.
- **Evidence:** timeline + push receipts.

### FUNC-1E-04 — Reactive workflow is event-driven, not polled
- **Severity:** HIGH
- **Verify by:** Kafka topics drive every step (no `time.sleep` polling in `scenarios/*`); steps are recorded with the trigger source in `jnpa.scenarios.params.steps`
- **Pass criterion:** code review confirms event-driven design.
- **Evidence:** grep hits for `time.sleep` (should be zero in scenario logic).

### FUNC-1E-05 — Each scenario step idempotent and replayable
- **Severity:** HIGH
- **Verify by:** run TFC-1 twice without reset in between; second run must NOT double-inject trucks and must NOT double-fire alerts
- **Pass criterion:** observed.
- **Evidence:** alert counts comparison.

### FUNC-1E-06 — Each scenario traced end-to-end
- **Severity:** HIGH
- **Verify by:** Jaeger trace ID retrievable for each scenario run; trace covers ingest → AI → alert → action
- **Pass criterion:** trace JSON downloadable.
- **Evidence:** trace ID and span count.

---

# PHASE 2 — PRODUCTION-GRADE ENGINEERING

## 2A — Security

### SEC-01 — No secrets in repository
- **Severity:** BLOCKER
- **Verify by:** `gitleaks detect --no-banner --redact -v` returns 0 leaks
- **Pass criterion:** 0 findings.
- **Evidence:** gitleaks report.

### SEC-02 — `.env*` files in `.gitignore`
- **Severity:** BLOCKER
- **Verify by:** `.gitignore` contains `.env`, `.env.*`, `!.env.local.example`
- **Pass criterion:** present.
- **Evidence:** grep.

### SEC-03 — TLS on the gateway (in-cluster or termination at ingress)
- **Severity:** CRITICAL — bid commits to TLS 1.2/1.3
- **Verify by:** gateway accepts HTTPS in compose (or, for dev, behind a Caddy/Traefik with self-signed cert)
- **Pass criterion:** TLS 1.2/1.3 enforced; older protocols denied.
- **Evidence:** `openssl s_client -connect localhost:443 -tls1_1` rejected; `-tls1_2` accepted.

### SEC-04 — RBAC on dashboard with at least 3 roles
- **Severity:** CRITICAL — RBAC explicitly required
- **Verify by:** `web/src/auth/roles.ts` defines roles {viewer, operator, admin}; FastAPI dependencies enforce them on `/api/*` routes
- **Pass criterion:** roles wired, at least one route per role guarded.
- **Evidence:** code extract + a 403 test.

### SEC-05 — MFA hook on admin endpoints
- **Severity:** HIGH — bid commits to MFA on sensitive ops
- **Verify by:** admin endpoints require an additional token (TOTP or webauthn stub for PoC)
- **Pass criterion:** even if PoC stub, the dependency is in place and removable only by env flag.
- **Evidence:** code path.

### SEC-06 — Audit logging tamper-evident
- **Severity:** CRITICAL — explicit bid item
- **Verify by:** every state change is logged to an append-only table `jnpa.audit_log` with `prev_hash, this_hash` chain (Merkle-style). A daily anchor (hash of last day's tail) printed to logs or written to MinIO with object-lock
- **Pass criterion:** chain table exists; anchor mechanism documented.
- **Evidence:** schema dump + anchor location.

### SEC-07 — Data at rest encryption posture
- **Severity:** CRITICAL — bid commits to encryption at rest
- **Verify by:** Postgres data volume on an encrypted FS in production target (documented in `docs/SECURITY.md`); MinIO `--encryption=auto`; Redis with `--requirepass`
- **Pass criterion:** posture documented even if dev compose doesn't enforce on the laptop.
- **Evidence:** docs reference.

### SEC-08 — Secrets manager integration plan
- **Severity:** HIGH
- **Verify by:** `docs/SECRETS.md` describes the production target (HashiCorp Vault / AWS Secrets Manager / KMS); gateway reads secrets via a swappable provider interface
- **Pass criterion:** interface present, default provider = env, production provider documented.
- **Evidence:** interface file path.

### SEC-09 — Container image scan
- **Severity:** HIGH
- **Verify by:** `trivy image <each-image>` for every service in compose
- **Pass criterion:** 0 CRITICAL CVEs; HIGH CVEs documented with accepted-risk rationale
- **Evidence:** trivy reports under `audit/evidence/SEC-09-<service>.txt`.

### SEC-10 — SBOM produced per service
- **Severity:** MEDIUM
- **Verify by:** `cyclonedx-bom` runs against each service folder; SBOMs in `sbom/`
- **Pass criterion:** SBOM per service, formats SPDX or CycloneDX.
- **Evidence:** sbom file listing.

### SEC-11 — Dependency vulnerabilities
- **Severity:** HIGH
- **Verify by:** `pip-audit` and `npm audit --omit=dev` per service
- **Pass criterion:** 0 CRITICAL; HIGH triaged.
- **Evidence:** audit JSON.

### SEC-12 — SAST (static analysis)
- **Severity:** MEDIUM
- **Verify by:** `bandit -r .` for Python; `eslint --no-eslintrc -c security` or `semgrep --config p/owasp-top-ten` for JS/TS
- **Pass criterion:** 0 HIGH severity issues unresolved.
- **Evidence:** report.

### SEC-13 — Authentication on ingest endpoints
- **Severity:** CRITICAL — ingestors must not be open
- **Verify by:** internal services accept only requests from the gateway (mTLS, shared secret header, or compose-network-only binding)
- **Pass criterion:** at least compose-network-only binding (no host port exposed); production target documented as mTLS.
- **Evidence:** compose port mappings.

### SEC-14 — PII minimisation in logs
- **Severity:** CRITICAL — DPDP Act 2023
- **Verify by:** structured logger redacts plate numbers below admin scope; owner names stored as hashes only
- **Pass criterion:** redaction implemented; tests for it.
- **Evidence:** test names.

### SEC-15 — Rate limiting on the gateway
- **Severity:** HIGH
- **Verify by:** per-IP rate limit middleware present, returns 429 with `Retry-After`
- **Pass criterion:** middleware present; one route smoke-tested.
- **Evidence:** test transcript.

### SEC-16 — CORS posture
- **Severity:** MEDIUM
- **Verify by:** allow-list of dashboard origins; not `*` in production target
- **Pass criterion:** allow-list configurable; default safe.
- **Evidence:** config snippet.

### SEC-17 — Sensitive data minimisation in evidence pack
- **Severity:** HIGH
- **Verify by:** evidence pack does not include `.env.local`, secrets, or full plate-to-owner mappings
- **Pass criterion:** check passes.
- **Evidence:** evidence pack file listing.

### SEC-18 — Incident-response runbook present
- **Severity:** HIGH — CERT-In requires reporting within 6 hours
- **Verify by:** `docs/runbooks/INCIDENT_RESPONSE.md` covers: detection, triage, containment, CERT-In notification template (6-hour window), evidence preservation, post-mortem
- **Pass criterion:** sections present.
- **Evidence:** runbook excerpt.

---

## 2B — Resilience

### RES-01 — Healthcheck on every service
- **Severity:** CRITICAL
- **Verify by:** `docker compose ps` shows `(healthy)` for every service; each Dockerfile has a `HEALTHCHECK` and FastAPI services expose `/healthz` and `/readyz`
- **Pass criterion:** all services healthy.
- **Evidence:** compose ps output.

### RES-02 — Liveness vs readiness distinction
- **Severity:** HIGH
- **Verify by:** `/healthz` returns 200 fast (process alive); `/readyz` checks downstream dependencies (DB, Kafka, Redis)
- **Pass criterion:** distinction implemented.
- **Evidence:** sample responses.

### RES-03 — Graceful shutdown
- **Severity:** HIGH
- **Verify by:** SIGTERM handlers in every service flush in-flight Kafka producers and close DB connections; tested by `docker compose stop` and checking log lines
- **Pass criterion:** "graceful shutdown complete" log present per service.
- **Evidence:** shutdown logs.

### RES-04 — Retry / back-off on Kafka clients
- **Severity:** HIGH
- **Verify by:** producer config includes `retries`, `retry.backoff.ms`, `enable.idempotence=true`; consumer config includes `enable.auto.commit=false` and manual commits after processing
- **Pass criterion:** all three settings present.
- **Evidence:** config dump.

### RES-05 — Database connection pooling
- **Severity:** HIGH
- **Verify by:** SQLAlchemy / asyncpg pool size set deliberately; not relying on defaults
- **Pass criterion:** pool config present per service with documented rationale.
- **Evidence:** config snippet.

### RES-06 — Kafka topic replication & retention
- **Severity:** HIGH
- **Verify by:** topics created with explicit retention; production target uses replication factor 3 (PoC may be 1, but documented)
- **Pass criterion:** topic create script present with explicit args; production override documented.
- **Evidence:** script path.

### RES-07 — Backpressure on the truck-sim → MQTT path
- **Severity:** CRITICAL at 20k devices
- **Verify by:** publisher respects queue depth; sheds load (or drops oldest, configurable) when broker can't keep up
- **Pass criterion:** code path present; tested at 20k scale.
- **Evidence:** test transcript.

### RES-08 — Idempotent Kafka consumers
- **Severity:** HIGH
- **Verify by:** consumers de-duplicate by `(topic, partition, offset)` or by a domain idempotency key; visible in the consumer code
- **Pass criterion:** present.
- **Evidence:** code extract.

### RES-09 — DB migrations versioned
- **Severity:** CRITICAL
- **Verify by:** Alembic (or equivalent) directory present; `init.sql` superseded or wrapped by a migration; CI runs migrations
- **Pass criterion:** Alembic present; up/down paths exist.
- **Evidence:** migration list.

### RES-10 — Backup and PITR documented
- **Severity:** HIGH (production)
- **Verify by:** `docs/BACKUP_RESTORE.md` documents: Postgres WAL archiving, full + incremental cadence, retention, recovery time objective (RTO), recovery point objective (RPO)
- **Pass criterion:** documented with concrete values (e.g., RTO ≤ 4h, RPO ≤ 15 min).
- **Evidence:** doc excerpt.

### RES-11 — Disaster recovery posture
- **Severity:** CRITICAL — bid commits to DR
- **Verify by:** `docs/DR.md` describes: hot-warm topology, DNS failover, replication for Postgres (logical or physical), MinIO bucket replication, Kafka MM2
- **Pass criterion:** topology present.
- **Evidence:** doc excerpt.

### RES-12 — Chaos test runbook
- **Severity:** MEDIUM
- **Verify by:** `docs/runbooks/CHAOS_DRILLS.md` lists drills: kill ANPR, kill congestion, network partition, broker restart, with expected fallback behaviour
- **Pass criterion:** at least 5 drills documented.
- **Evidence:** doc excerpt.

### RES-13 — Cross-service timeouts set explicitly
- **Severity:** HIGH
- **Verify by:** every HTTP client has connect + read timeouts; no library defaults relied on
- **Pass criterion:** grep finds explicit timeouts on every external call.
- **Evidence:** grep hits.

---

## 2C — Observability

### OBS-01 — Structured logs everywhere
- **Severity:** CRITICAL
- **Verify by:** all services log JSON; `trace_id`, `service`, `version`, `severity` present on every line
- **Pass criterion:** sample log line per service has all four fields.
- **Evidence:** log samples.

### OBS-02 — Metrics on every service
- **Severity:** CRITICAL
- **Verify by:** Prometheus endpoint on every service; scrape configs in `infra/prometheus/`
- **Pass criterion:** all services reachable from Prometheus.
- **Evidence:** `up == 1` for all services in Prometheus.

### OBS-03 — Distributed tracing wired
- **Severity:** HIGH
- **Verify by:** OpenTelemetry SDK initialised in every service; trace propagation through Kafka headers; Jaeger in compose
- **Pass criterion:** any TFC-x scenario produces an end-to-end trace with > 4 spans.
- **Evidence:** trace JSON.

### OBS-04 — Dashboards exist for the SLOs
- **Severity:** HIGH
- **Verify by:** Grafana provisioned dashboards: "UC-III overview", "Ingest health", "AI inference latency", "Fallback decisions", "Scenario timeline"
- **Pass criterion:** all five present.
- **Evidence:** dashboard JSON in `infra/grafana/provisioning/dashboards/`.

### OBS-05 — Alerting rules defined
- **Severity:** HIGH
- **Verify by:** Prometheus alerting rules for: kafka_lag, model latency p95, decision-path=CACHED for > 5 min, anomaly service down, scenario stuck
- **Pass criterion:** all five rules present.
- **Evidence:** rules file.

### OBS-06 — SLOs documented
- **Severity:** HIGH
- **Verify by:** `docs/SLO.md` defines: availability target per service, latency p95 per endpoint, error budget policy
- **Pass criterion:** numeric values present.
- **Evidence:** doc.

### OBS-07 — Correlation between alerts and runbooks
- **Severity:** MEDIUM
- **Verify by:** each Prometheus alert carries an annotation `runbook_url` pointing to a markdown in `docs/runbooks/`
- **Pass criterion:** all alerts annotated.
- **Evidence:** alert rules excerpt.

### OBS-08 — Log retention policy aligned to DPDP
- **Severity:** HIGH — DPDP Act 2023
- **Verify by:** `docs/LOG_RETENTION.md` defines retention windows per log family, especially for logs that contain PII (e.g., plate numbers)
- **Pass criterion:** documented; PII-bearing logs ≤ 180 days unless legal hold
- **Evidence:** doc.

---

## 2D — Performance

### PERF-01 — Load tests written for hot paths
- **Severity:** HIGH
- **Verify by:** `tests/load/` contains k6 or locust scripts for: gateway `/api/vahan/rc/{plate}`, `/api/kpi`, congestion `/predict`
- **Pass criterion:** scripts present; thresholds in script.
- **Evidence:** script paths.

### PERF-02 — Targets met at design scale
- **Severity:** HIGH
- **Verify by:** running PERF-01 against the stack hits: gateway p95 ≤ 250 ms, congestion `/predict` p95 ≤ 800 ms, ANPR p95 ≤ 400 ms per crop
- **Pass criterion:** all three.
- **Evidence:** k6 / locust summary.

### PERF-03 — Kafka throughput at 20k devices
- **Severity:** CRITICAL
- **Verify by:** sustained 4000 msg/s on `truck.telemetry`; consumer lag steady-state < 1000 msgs
- **Pass criterion:** both.
- **Evidence:** consumer lag chart.

### PERF-04 — Memory & CPU caps set per service
- **Severity:** HIGH
- **Verify by:** every compose service has `deploy.resources.limits` set (CPU + memory)
- **Pass criterion:** all services.
- **Evidence:** compose excerpt.

### PERF-05 — GPU model fallback to CPU is tested
- **Severity:** MEDIUM
- **Verify by:** ANPR and anomaly services have explicit device selection (`cuda` if available else `cpu`); CPU path passes the eval (possibly with lower throughput, documented)
- **Pass criterion:** both paths execute.
- **Evidence:** logs from both runs.

### PERF-06 — Database indices for hot queries
- **Severity:** HIGH
- **Verify by:** hypertables have time-based indices; `alerts(kind, ts)`, `vehicle_master(provisional, provisional_until)`, `truck_telemetry(device_id, ts)` all indexed
- **Pass criterion:** indexes present (`\d+` on each table).
- **Evidence:** psql output.

### PERF-07 — Continuous aggregates / materialised views for KPIs
- **Severity:** HIGH
- **Verify by:** Timescale continuous aggregates for: gate throughput per 1 min, segment jam factor per 1 min, alerts by kind per 5 min
- **Pass criterion:** all three present.
- **Evidence:** aggregate definitions.

---

## 2E — Data Management

### DATA-01 — Schema migrations idempotent and reversible
- **Severity:** CRITICAL
- **Verify by:** every Alembic revision has both `upgrade()` and `downgrade()` non-trivially defined
- **Pass criterion:** all revisions reviewed.
- **Evidence:** Alembic listing.

### DATA-02 — Data classification document present
- **Severity:** CRITICAL — DPDP
- **Verify by:** `docs/DATA_CLASSIFICATION.md` lists every table/column with classification {public, internal, restricted, PII, SPDI} and retention
- **Pass criterion:** every table in jnpa schema covered.
- **Evidence:** doc.

### DATA-03 — PII fields encrypted (column-level) where required
- **Severity:** HIGH — DPDP
- **Verify by:** owner names, DL numbers stored as hashes (one-way) or encrypted with key envelope; access requires admin role
- **Pass criterion:** stored hashes; one route to access plaintext via gated decrypt
- **Evidence:** schema + decrypt function.

### DATA-04 — Test data and production data isolated
- **Severity:** CRITICAL
- **Verify by:** `data/fixtures/` only ever loaded into named test/dev DBs (not "postgres" default); environment marker prevents accidental load into production
- **Pass criterion:** guard present.
- **Evidence:** guard code.

### DATA-05 — Vehicle-master deletion path (DPDP right to erasure)
- **Severity:** HIGH — DPDP rights
- **Verify by:** `DELETE /api/vehicle/{plate}` route exists (admin-only) and propagates to delete from alerts, telemetry, RFID, ANPR history with audit-log entry
- **Pass criterion:** route exists with cascade.
- **Evidence:** test transcript.

### DATA-06 — Evidence retention policy
- **Severity:** HIGH
- **Verify by:** MinIO bucket policy retains evidence ≥ 1 year; legal-hold lock available for items with open challan
- **Pass criterion:** documented and enforced.
- **Evidence:** policy.

### DATA-07 — Backups encrypted
- **Severity:** HIGH
- **Verify by:** backup scripts encrypt before upload; KMS key rotation documented
- **Pass criterion:** both.
- **Evidence:** script.

---

## 2F — Deployment, DR, and Environment

### DEPLOY-01 — One-command bring-up still works
- **Severity:** CRITICAL
- **Verify by:** `make up && sleep 60 && make bootstrap-check` prints `BOOTSTRAP OK`
- **Pass criterion:** clean machine bring-up < 5 min.
- **Evidence:** transcript.

### DEPLOY-02 — Compose has no `latest` tags
- **Severity:** HIGH
- **Verify by:** grep `:latest` in compose → zero hits
- **Pass criterion:** zero.
- **Evidence:** grep.

### DEPLOY-03 — Multi-stage Dockerfiles
- **Severity:** MEDIUM
- **Verify by:** every Dockerfile uses build + runtime stages
- **Pass criterion:** all do.
- **Evidence:** Dockerfile excerpts.

### DEPLOY-04 — Non-root user in containers
- **Severity:** HIGH
- **Verify by:** `USER` directive in every Dockerfile
- **Pass criterion:** all do.
- **Evidence:** Dockerfile excerpts.

### DEPLOY-05 — Image provenance (signed)
- **Severity:** MEDIUM
- **Verify by:** docs reference cosign signing in CI; not required for laptop demo
- **Pass criterion:** docs present.
- **Evidence:** doc.

### DEPLOY-06 — Environment matrix documented
- **Severity:** HIGH
- **Verify by:** `docs/ENV_MATRIX.md` defines dev / staging / pre-prod / prod with differences in scale, secrets, replicas, monitoring
- **Pass criterion:** four envs documented.
- **Evidence:** doc.

### DEPLOY-07 — Production deployment target documented
- **Severity:** HIGH — bid commits to on-prem / MeitY GCC / hybrid
- **Verify by:** `docs/DEPLOY_PROD.md` describes Kubernetes manifests or alternative; data residency = India; cloud provider option = MeitY-empanelled
- **Pass criterion:** documented.
- **Evidence:** doc.

### DEPLOY-08 — Configuration as code
- **Severity:** HIGH
- **Verify by:** all configuration externalised; no hard-coded URLs or secrets
- **Pass criterion:** grep finds no hard-coded URLs.
- **Evidence:** grep negative.

### DEPLOY-09 — Rollback strategy documented
- **Severity:** HIGH
- **Verify by:** `docs/ROLLBACK.md` describes image rollback, DB migration down, feature-flag kill
- **Pass criterion:** doc.
- **Evidence:** doc.

### DEPLOY-10 — Feature-flag plumbing present
- **Severity:** MEDIUM
- **Verify by:** at least one feature flag system in place (LaunchDarkly client or simple env-driven flags); fallback orchestrator tiers controllable via flags
- **Pass criterion:** present.
- **Evidence:** code path.

---

## 2G — Operability

### OPS-01 — Runbooks for top-10 alerts
- **Severity:** HIGH
- **Verify by:** `docs/runbooks/` has at least 10 runbooks linked from Prometheus alerts
- **Pass criterion:** present.
- **Evidence:** runbook list.

### OPS-02 — On-call playbook
- **Severity:** HIGH
- **Verify by:** `docs/runbooks/ONCALL.md` lists pager rotation, escalation tree, severity definitions, comms template
- **Pass criterion:** all four sections.
- **Evidence:** doc.

### OPS-03 — Make targets cover the lifecycle
- **Severity:** MEDIUM
- **Verify by:** Makefile has `up, down, logs, test, e2e, lint, format, audit, scale, drill, demo-reset`
- **Pass criterion:** all 11 targets.
- **Evidence:** make targets list.

### OPS-04 — Demo-reset returns to known state
- **Severity:** HIGH — needed for evaluator walk-throughs
- **Verify by:** `make demo-reset` clears synthetic state, keeps models, restarts the stack to baseline in < 90 s
- **Pass criterion:** ≤ 90 s.
- **Evidence:** transcript.

### OPS-05 — Operator training material
- **Severity:** MEDIUM
- **Verify by:** `docs/operator/` has short user guide for each dashboard screen
- **Pass criterion:** one doc per screen.
- **Evidence:** doc list.

### OPS-06 — Service catalogue
- **Severity:** MEDIUM
- **Verify by:** `docs/SERVICE_CATALOGUE.md` lists every service with: purpose, owner, dependencies, SLO, runbook
- **Pass criterion:** every compose service listed.
- **Evidence:** doc.

---

## 2H — Documentation & Architecture

### DOC-01 — README is a one-shot bring-up
- **Severity:** HIGH
- **Verify by:** README contains prerequisites, clone steps, `cp .env.local.example .env.local`, `make up`, verification, troubleshooting
- **Pass criterion:** all five sections.
- **Evidence:** README extract.

### DOC-02 — Architecture decision records (ADRs)
- **Severity:** HIGH
- **Verify by:** `docs/adr/` contains at least: ADR-001 Stack choice, ADR-002 GNN+LSTM model, ADR-003 Fallback orchestrator, ADR-004 Evidence chain, ADR-005 PII handling
- **Pass criterion:** all five present.
- **Evidence:** ADR list.

### DOC-03 — OpenAPI for gateway
- **Severity:** CRITICAL
- **Verify by:** `gateway` publishes `/openapi.json` and `/docs`; every endpoint has summary, response schemas, examples
- **Pass criterion:** all routes covered.
- **Evidence:** OpenAPI file size + endpoint count.

### DOC-04 — Diagrams in repo
- **Severity:** MEDIUM
- **Verify by:** `docs/diagrams/` has: system overview, dataflow per sub-criterion (5), DR topology, evidence chain
- **Pass criterion:** at least 7 diagrams.
- **Evidence:** file count.

### DOC-05 — Threat model
- **Severity:** HIGH — required for IEC 62443 alignment
- **Verify by:** `docs/THREAT_MODEL.md` covers: assets, trust boundaries (IT/OT zones), STRIDE pass, mitigations mapped to controls
- **Pass criterion:** all four sections.
- **Evidence:** doc.

### DOC-06 — Glossary
- **Severity:** LOW
- **Verify by:** `docs/GLOSSARY.md` defines DTCS, DPD, e-Seal, ULIP, PCS, LDB, FOIS, IWAI, TAS, Karal Phata
- **Pass criterion:** all ten terms.
- **Evidence:** glossary.

---

# PHASE 3 — COMPLIANCE

## 3A — IEC 62443 (Cybersecurity for IACS)

### COMP-IEC-01 — Zone/conduit model documented
- **Severity:** CRITICAL — explicit bid commitment to IEC 62443 zoning
- **Verify by:** `docs/threat_model/ZONES.md` defines: L0 field devices, L1 cameras/RFID/IoT, L2 ingest, L3 AI services, L4 gateway/UI, L5 DR; conduits documented between zones with allowed protocols
- **Pass criterion:** all six levels.
- **Evidence:** zone diagram.

### COMP-IEC-02 — Inter-zone authentication
- **Severity:** HIGH
- **Verify by:** higher-trust → lower-trust calls authenticated; documented plan for mTLS at L2/L3 boundary
- **Pass criterion:** documented.
- **Evidence:** doc.

### COMP-IEC-03 — Patch management policy
- **Severity:** HIGH
- **Verify by:** `docs/PATCH_MGMT.md` describes monthly cadence, emergency-patch SLA (≤ 7 days for critical CVE), staged rollout
- **Pass criterion:** all three.
- **Evidence:** doc.

## 3B — DPDP Act 2023

### COMP-DPDP-01 — Lawful purpose mapping
- **Severity:** CRITICAL
- **Verify by:** `docs/DPDP_COMPLIANCE.md` lists every category of personal data, purpose, lawful basis, retention
- **Pass criterion:** matrix complete.
- **Evidence:** doc.

### COMP-DPDP-02 — Data principal rights operationalised
- **Severity:** HIGH
- **Verify by:** routes for access, correction, erasure, grievance present (even if admin-mediated for PoC)
- **Pass criterion:** four routes documented.
- **Evidence:** routes.

### COMP-DPDP-03 — DPIA template
- **Severity:** HIGH
- **Verify by:** Data Protection Impact Assessment template ready in `docs/DPIA.md` with risks categorised
- **Pass criterion:** template present.
- **Evidence:** doc.

### COMP-DPDP-04 — Cross-border data transfer posture
- **Severity:** HIGH — DPDP allows transfer only to notified countries
- **Verify by:** `docs/DPDP_COMPLIANCE.md` confirms no transfer of personal data outside India; all third-party APIs documented with data-flow direction
- **Pass criterion:** confirmed.
- **Evidence:** doc.

## 3C — CERT-In

### COMP-CERT-01 — 6-hour incident reporting template
- **Severity:** CRITICAL
- **Verify by:** `docs/runbooks/INCIDENT_RESPONSE.md` contains the CERT-In notification template ready to send within 6 hours of detection
- **Pass criterion:** template present.
- **Evidence:** template excerpt.

### COMP-CERT-02 — Log retention 180 days
- **Severity:** CRITICAL — CERT-In direction
- **Verify by:** log retention policy ≥ 180 days for system/security logs
- **Pass criterion:** documented.
- **Evidence:** policy.

### COMP-CERT-03 — NTP synchronisation
- **Severity:** HIGH — CERT-In direction
- **Verify by:** containers use NTP (Indian NIC time source preferred); time skew < 100 ms
- **Pass criterion:** verified.
- **Evidence:** ntp config.

## 3D — Make-in-India (MII) Order

### COMP-MII-01 — Local content disclosure
- **Severity:** HIGH — affects bid scoring on Class-I/II/III local supplier classification
- **Verify by:** `docs/MII_DISCLOSURE.md` lists every commercial dependency with origin and the % of bill of materials attributable to India-origin work
- **Pass criterion:** doc present, computed.
- **Evidence:** doc.

### COMP-MII-02 — Indian-origin AI artefacts noted
- **Severity:** MEDIUM
- **Verify by:** fine-tuned ANPR model documented as work product of the bidder (India-origin)
- **Pass criterion:** noted.
- **Evidence:** model manifest.

## 3E — STQC/BIS

### COMP-STQC-01 — Hardware/firmware compliance plan
- **Severity:** HIGH (production)
- **Verify by:** `docs/STQC_PLAN.md` describes the certification plan for edge devices (cameras, IoT, NVRs); not a code item but must be present so the bid is internally consistent
- **Pass criterion:** doc present.
- **Evidence:** doc.

## 3F — GeM GTC and Arbitration

### COMP-GEM-01 — Defaults match GeM GTC
- **Severity:** MEDIUM
- **Verify by:** any commercial terms baked into the PoC (SLA defaults, breach handling) reflect GeM GTC defaults; deviations called out
- **Pass criterion:** doc cross-references GeM GTC.
- **Evidence:** doc.

### COMP-GEM-02 — Seat of arbitration noted
- **Severity:** LOW (code irrelevant but doc consistency)
- **Verify by:** README footer or compliance doc states arbitration seat = Pune, per ICA
- **Pass criterion:** noted.
- **Evidence:** doc.

---

# PHASE 4 — INTEGRATION COMPLETENESS

> Every integration committed in BID DOC §III row and `1774533360_scope.pdf` items 1–6.

| CHECK ID | Integration | Pass criterion |
|---|---|---|
| INT-01 | **ULIP/NLP** schema parity | `schemas.UlipMessage` matches the latest ULIP developer-portal sample; proxy route `/api/ulip/*` returns 200 against the sandbox if `ULIP_API_KEY` present, 503 otherwise |
| INT-02 | **PCS (Port Community System)** | adapter `gateway/clients/pcs.py` exists; even if stubbed, declares the message envelope and a smoke test |
| INT-03 | **TOS** | adapter `gateway/clients/tos.py` exists; the cross-twin `cargo.dpd_release` topic schema documented |
| INT-04 | **LDB** | adapter `gateway/clients/ldb.py` exists with documented schema |
| INT-05 | **FOIS** | adapter `gateway/clients/fois.py` exists with documented schema |
| INT-06 | **IWAI** | adapter `gateway/clients/iwai.py` exists with documented schema |
| INT-07 | **Vahan** | live + sim covered (FUNC-1A-04, 1A-05) |
| INT-08 | **Sarathi** | sim covered; live adapter declared |
| INT-09 | **FastTag (NETC)** | sim covered; live adapter declared |
| INT-10 | **Carbon Emissions API** | adapter declared; pull cadence documented |
| INT-11 | **DTCS (Drive-Through Container Scanner)** | hook point declared (`ingest/dtcs/`) even if stub-only |
| INT-12 | **Boom barriers** | mock service `ingest/boom/` with command channel `boom/cmd/{gate}` over MQTT |
| INT-13 | **Weighbridge** | mock service `ingest/weighbridge/`, 5 logical nodes, producing weight events |
| INT-14 | **TAS (TAS sub-system of TOS)** | mock service `gateway/tas_mock.py` with slot reschedule API |
| INT-15 | **Google Maps Distance Matrix/Roads** | live; falls back per FUNC-1C / RES |
| INT-16 | **HERE Traffic Flow v7** | live fallback |
| INT-17 | **TomTom Traffic Flow** | second fallback |
| INT-18 | **OpenWeatherMap** | live; degrades to cached |
| INT-19 | **Bhuvan WMS** | fallback basemap on dashboard |
| INT-20 | **OSRM** | route engine, with HERE Routing fallback |
| INT-21 | **MinIO** | evidence storage; signed URLs |
| INT-22 | **MQTT (Mosquitto)** | RFID + trucking + boom |
| INT-23 | **Kafka** | event backbone |
| INT-24 | **Open protocols at adapter boundary** | every external adapter exposes/consumes one of REST, gRPC, MQTT, OPC-UA, Modbus, IEC 61850, ONVIF — and which one is documented |

Each row produces a CHECK with severity HIGH unless the integration is in BLOCKER/CRITICAL functional checks above, in which case the higher severity wins.

**Verify by (template applied to each row):**
- adapter file exists in the path noted
- schema declared in `shared/jnpa_shared/schemas.py` or in the adapter's `schema.py`
- adapter has a stub or live test
- documentation in `docs/INTEGRATIONS/<NAME>.md` covers: purpose, schema, auth, rate limits, fallback behaviour, owner

---

# CROSS-CUTTING — Anti-patterns the auditor must flag

Search the entire codebase for these and report any hits with file:line and severity:

| Anti-pattern | Severity | Rationale |
|---|---|---|
| `print(` in service code (not scripts) | LOW | structured logger only |
| `except Exception: pass` | HIGH | swallows failures, blinds ops |
| `time.sleep` in async paths | MEDIUM | blocks event loop |
| `eval(` / `exec(` | CRITICAL | RCE risk |
| `subprocess.*shell=True` with interpolation | CRITICAL | command injection |
| `verify=False` on `requests` / `httpx` | CRITICAL | TLS bypass |
| `localhost` hard-coded in service code | HIGH | breaks containerisation/DR |
| `TODO`, `FIXME`, `XXX` in code | LOW | hygiene |
| Hard-coded `password`, `secret`, `key`, `token` literals | BLOCKER | secret leak |
| Wildcard CORS `*` | HIGH | misconfiguration risk |
| Missing `@router.dependencies` for protected routes | CRITICAL | auth bypass |

---

# REPORTING TEMPLATE

The auditor produces `./audit/AUDIT_REPORT.md` like so:

```
# JNPA UC-III Production Audit Report

## Repository fingerprint
- commit: <hash>
- branch: <branch>
- run at (UTC): <ts>
- services discovered: <list>

## Executive summary
<two paragraphs>

### Severity histogram
| Severity | Count |
|---|---|
| BLOCKER | n |
| CRITICAL | n |
| HIGH | n |
| MEDIUM | n |
| LOW | n |

### Top 5 risks
1. <CHECK_ID> — <one-line risk>
2. ...

## Phase 0 — Inventory
| INV-ID | Title | Result | Notes |
| ... |

## Phase 1 — Functional alignment
### 8.5.1 Multi-Source Data Integration
<table of FUNC-1A-*>
<FAIL/PARTIAL paragraphs>

### 8.5.2 AI/ML Tools Usage
...

### 8.5.3 API / Data Integration & Fallback
...

### 8.5.4 Dashboard & KPI Monitoring
...

### 8.5.5 What-If + Reactive
...

## Phase 2 — Production-grade engineering
### Security
### Resilience
### Observability
### Performance
### Data
### Deployment
### Operability
### Documentation

## Phase 3 — Compliance
### IEC 62443
### DPDP Act 2023
### CERT-In
### MII
### STQC/BIS
### GeM / Arbitration

## Phase 4 — Integration completeness
| INT-ID | Integration | Result |
| ... |

## Cross-cutting findings
| Anti-pattern | Hits | Files |

## Appendix A — Full check results table
| CHECK_ID | Title | Severity | Result | Evidence file |

## Appendix B — Evidence index
| Evidence file | Bytes | Captured at |
```

---

# REMEDIATION PRIORITISATION RUBRIC

The auditor produces `./audit/REMEDIATION_PLAN.md` strictly ordered by:

1. **BLOCKER** items — these prevent the evaluator from awarding the sub-criterion. Fix within 48 hours, before any demo.
2. **CRITICAL** items — these prevent go-live. Schedule into a single sprint.
3. **HIGH** items — production risk. Schedule into the first 90 days of AMC mobilisation.
4. **MEDIUM** items — quality. Schedule into Year-1 AMC.
5. **LOW** items — hygiene backlog. No deadline.

Within each severity, order by:
- **Dependency direction** — fixes that unblock other fixes go first.
- **Effort estimate** — Claude Code provides T-shirt size {XS=<2h, S=<1d, M=<3d, L=<1wk, XL=>1wk}.
- **Risk decay** — if a finding has a near-term external trigger (e.g., demo date, CERT-In timeline), it bubbles up.

Each remediation item must include:

```
### <CHECK_ID> — <title>
- Severity: <S>
- Effort: <T-shirt>
- Owner candidate: <integration / AI / platform / dashboard / docs>
- Files to touch: <paths>
- What to do (step-by-step):
  1.
  2.
  3.
- Acceptance test: <command or test that re-runs the same check>
- Notes / risks while fixing:
```

Re-audit trigger: any of the following warrant re-running the full audit
- a BLOCKER or CRITICAL finding has been fixed
- evaluator demo date confirmed (≥ 5 working days before)
- pre-production sign-off requested
- a new prompt from the build-prompt pack has been executed

---

# RE-AUDIT GATE — Pre-evaluation demo

Before the evaluator visit, run only the **BLOCKER + CRITICAL** subset:

```
make audit-blocker-critical
```

This must be a Make target the auditor adds in the report (as a remediation item itself, severity HIGH). It runs the subset and prints a green/red verdict so Aniket can decide whether to defer the demo.

---

# REMEDIATION PROMPT (paste later, only after reviewing the plan)

> Paste the block below into Claude Code only when the report and plan have been reviewed and you are ready to fix.

---

```text
You have already produced ./audit/AUDIT_REPORT.md and
./audit/REMEDIATION_PLAN.md. Switch from AUDITOR mode to REMEDIATOR mode.

RULES OF EXECUTION
  - Work only on items present in REMEDIATION_PLAN.md, in the order
    given there.
  - Start with BLOCKER items. Do not touch CRITICAL until every
    BLOCKER is closed and re-verified.
  - For each remediation item:
      (a) re-read the original check from the audit checklist
      (b) make the smallest correct change
      (c) re-run the acceptance test exactly as stated
      (d) update AUDIT_REPORT.md with the new result and timestamp
      (e) move to the next item
  - Never make changes to scope. If a fix requires scope expansion,
    stop and ask.
  - Never weaken a test to make it pass.
  - Never modify the audit checklist document.
  - Every change must be a focused, reviewable diff. Group related
    changes per commit.

STOP CONDITIONS
  - All BLOCKER items closed -> stop and ask for confirmation before
    starting CRITICAL.
  - An acceptance test fails after the fix -> stop and report.
  - A fix requires destructive operations -> stop and ask.

WHEN DONE WITH A SEVERITY BAND
Print to chat:
  REMEDIATION PASS COMPLETE for severity=<S>.
  Closed: <n>. Re-opened: <n>. Pending: <n>.
  Next severity band ready: <S+1>.
Then wait.
```

---

*End of audit checklist. Save this file at `docs/AUDIT_CHECKLIST.md` so it stays version-controlled and is the single source of truth for what "production-grade UC-III" means.*
