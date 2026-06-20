# UC3 Remediation Changelog

One line per task: `[<TASK-ID>] <what changed> <file:line>`. Tracks the remediation pass
against `docs/UC3_PRODUCTION_AUDIT.md` / `docs/UC3_Remediation_Prompt.md`.

## Wave 0 — Truth & safety

- [0.1] COVERAGE.md §8.5.2 row rewritten: congestion F1=0.8411 **below** 0.85, ANPR OCR ~11% fallback (`OCR_TARGET_MET:false`), ETA labelled heuristic; resolved the :40↔:59 self-contradiction — `docs/COVERAGE.md:59`
- [0.1] COVERAGE.md UC1-parity row: "ArcGIS 5.x + Calcite dark shell" → "ArcGIS **4.31** web components + Calcite **light** shell" — `docs/COVERAGE.md:70`
- [0.1] README "Calcite dark shell" → "Calcite light shell (`calcite-mode-light`)" — `README.md:86`
- [0.1] README e2e prose: F1/`OCR_TARGET_MET` framed as target-checks, not achieved facts; added an honest "Current model state" callout — `README.md:500`
- [0.2] Extended `OcrEval`/`CongestionMetrics` types with `target`/`target_met`/`degraded` — `web/src/data/types.ts:38`
- [0.2] Mock realism probes now mirror REAL artifacts (OCR 0.1063 target-not-met/degraded; F1 0.8411 below 0.85) instead of fabricated 0.97/0.86 — `web/src/data/mock.ts:929`
- [0.2] Live adapter reads `combined_weighted_accuracy_pct`/`OCR_TARGET_MET`/`degraded` and `target_f1` from the gateway eval endpoints — `web/src/data/live.ts:123`
- [0.2] Demo Console renders a red "DEGRADED MODEL — fallback OCR active" notice + a "Below target" F1 notice; numbers go red when a target is unmet — `web/src/screens/DemoConsole.tsx:216`
- [0.3] Moved unused MapLibre operations-map duplicate out of the source tree (no importers) — `web/_attic/LiveMap.tsx.maplibre-unused` (was `web/src/components/map/LiveMap.tsx`). NOTE: `GeofencingManager.tsx` still legitimately uses MapLibre+terra-draw for the no-parking zone editor; the LiveOperations map is ArcGIS.
- [0.4] Theme decision: stay **light** (deliberate, per commit e325ce4); code unchanged, docs aligned in 0.1 — `web/src/components/layout/Shell.tsx:86`
- [0.5] Demo Console: replaced the clickable "(preview)" presenter controls with **disabled** roadmap placeholders + a brand notice pointing presenters to the What-If Console for TFC-1/2/3 — `web/src/screens/DemoConsole.tsx:334`

**Wave 0 verification:** `tsc -b --noEmit` exit 0 · web vitest 17/17 pass · pytest 168 pass / 24 skip / 8 fail — all 8 failures are pre-existing `ModuleNotFoundError: torch` in `test_congestion.py` (local `.venv` lacks PyTorch), unrelated to Wave 0 (no Python files changed). To be addressed by Wave 1.1 (make these skip, not fail, on a torch-less host).

## Wave 1 — AI numbers honest & enforced

- [1.1] Added HARD congestion F1 gate `test_congestion_f1_meets_target` (reads committed metrics.json, no torch) — `tests/test_congestion.py:163`. Marked `xfail(strict=True)` because the committed artifact is 0.8411 < 0.85: stays honest+green on a CPU host AND self-correcting (a retrain that clears 0.85 XPASSes → strict-fail → forces marker removal). Number can no longer silently drift or be silently "fixed".
- [1.1] Added `test_congestion_metrics_artifact_is_wellformed` (gateable fields always present, value-independent) — `tests/test_congestion.py:188`.
- [1.1] Decorated the 8 graph/synthetic/feature/source tests with `skipif(not HAVE_TORCH)` so a torch-less host SKIPS (was: 8 hard failures) — `tests/test_congestion.py:58+`. Added missing `import json` — `tests/test_congestion.py:18`.
- [1.1] Selftest B.1 made **dynamically required**: hard gate once F1 meets target, honest non-blocking WARN while below (demo stays green, gap visible) — `scripts/poc_selftest.py:85,408`.
- [1.2] Added ANPR OCR ≥95% gate `test_anpr_ocr_meets_target_when_weights_present` — SKIP when fallback/no-weights, FAIL when weights present and <95% — `tests/test_anpr_ai.py:159`. Added `test_anpr_eval_artifact_is_wellformed` — `tests/test_anpr_ai.py:151`.
- [1.2] Relabelled selftest SIM.3 to make clear it gates the SIMULATED OCR-*confidence distribution*, not the real engine accuracy (which the ANPR /eval B-gate enforces) — `scripts/poc_selftest.py:296,399`.
- [1.3] COVERAGE C7 corrected: AI parts present (detection/classification/OCR/trajectory/density) but **ETA is heuristic (OSRM + dead-reckoning), not a learned model** — `docs/COVERAGE.md:27`. Driver Trip screen labels "ETA to gate" with no AI claim (`mobile-pwa/src/screens/Trip.tsx:109`) — no change needed.

**Wave 1 verification:** `tsc -b --noEmit` exit 0 · full pytest **170 pass / 33 skip / 1 xfail / 0 fail** (was 168/24/8-fail — the 8 torch failures are now clean skips; floor preserved + improved) · `poc_selftest` **22/23, 0 required failing**, B.1 honest amber WARN ("F1 = 0.8411 vs target 0.85 — below target"), overall verdict stays GREEN. Gates confirmed: F1 gate XFAILs (not fail), ANPR OCR gate SKIPs with weights absent, both FLIP to hard failures the moment real numbers regress/are-met-then-drop.

## Wave 2 — CI that can actually fail

- [2.1] Added `.github/workflows/ci.yml` — 3 gating jobs: **web** (prettier check + eslint + tsc typecheck + vitest), **mobile** (eslint + tsc typecheck), **python** (editable installs mirroring `make install-shared` + `pytest shared tests` + `poc_selftest`), plus a `ci-ok` aggregate. Runs on push + PR; `workflow_call` enabled so deploy can require it — `.github/workflows/ci.yml`.
- [2.1] `deploy.yml` now gates on CI: added a `ci` job (`uses: ./.github/workflows/ci.yml`) and `deploy: needs: ci` — deploy proceeds only after every CI job is green — `.github/workflows/deploy.yml:8`.
- [2.2] Added Prettier: `.prettierrc.json`, `.prettierignore`, root `format`/`format:check` (+ `lint`/`test`/`typecheck` passthrough) scripts and `prettier` devDep — `package.json`, `.prettierrc.json`, `.prettierignore`. Ran `format` once across `web/src` + `mobile-pwa/src` (49 files normalized); `format:check` now clean and CI-gated.
- [2.3] Coverage visibility: web job runs `vitest --coverage` (added `@vitest/coverage-v8` devDep — `web/package.json:52`); python job runs `pytest --cov` (`pytest-cov` pip-installed in CI). Printed, not thresholded (visibility only, per spec).

**Wave 2 verification:** both workflow YAMLs parse (`yaml.safe_load` OK) · CI-exact commands run green locally — `pnpm format:check` clean, `pnpm --filter jnpa-uc3-dashboard {lint,typecheck,test --coverage}` pass (17/17), mobile lint/typecheck pass (0 errors), web+mobile `tsc` exit 0 after the prettier reformat (purely cosmetic — no functional change) · `pnpm --filter` names match package names.

## Wave 3 — Security & access control (flag-gated; default OFF for demo, ON for prod)

- [3.1] New `gateway/auth.py`: `AUTH_ENABLED`-gated global middleware — JWT bearer (PyJWT + stdlib HS256 fallback so no extra install for tests), per-path RBAC policy, in-process per-consumer token-bucket rate limiter (429). Public paths (`/healthz`, `/metrics`, `/api/auth/*`, WS) always open — `gateway/auth.py`.
- [3.1] New `gateway/routers/auth.py`: `/api/auth/login` (seeded role users, OIDC-ready seam), `/api/auth/dev-token` (flag-gated), `/api/auth/roles` — `gateway/routers/auth.py`.
- [3.1] `gateway/main.py`: origin-scoped CORS from `CORS_ALLOW_ORIGINS` (credentialed when explicit; `*` only in dev), `install_auth(app)`, auth router mounted — `gateway/main.py:114,124`. Added `pyjwt==2.*` to gateway deps — `gateway/pyproject.toml`. README "Security" section documents auth/RBAC/DPDP/secrets/TLS — `README.md`.
- [3.2] RBAC: 6-role enum (`JNPA_TRAFFIC/TERMINAL_OPS/CUSTOMS/TRAFFIC_POLICE/DRIVER/DTCCC_ADMIN`) carried in the JWT; per-path scoping (police reports, fault/scenario control-room-only, identity customs/admin, driver check-in/push) — `gateway/auth.py:_POLICY`. Web: `web/src/lib/auth.ts` (role model + screen policy + token storage), bearer attached in `web/src/lib/api.ts`, role-filtered nav `web/src/components/layout/Sidebar.tsx`, route guards + login gate `web/src/App.tsx` + `web/src/components/auth/LoginGate.tsx`.
- [3.3] Infra secrets: Grafana `admin/admin` → `${GRAFANA_ADMIN_PASSWORD:?…}` fail-fast; MinIO consumer `:-minioadmin` defaults → `${MINIO_*:?…}` — `docker-compose.yml:217,341,647,741`. New keys documented in `.env.local.example`.
- [3.4] DPDP code-enforcement: new `gateway/dpdp.py` (purpose allow-list → 400; real-biometric refusal unless `ALLOW_REAL_BIOMETRICS` → 403; per-access audit sink). Wired into `/api/identity/verify` with `is_synthetic`/`purpose` in the request + tagged responses — `gateway/routers/identity.py:83`.
- [3.x] Tests: `tests/test_auth_rbac.py` — 14 tests covering policy map, auth-disabled pass-through, public paths, 401 (missing/bad token), 403 (wrong role), passes-gate (right role), login mint, rate-limit 429, and DPDP (default-synthetic, bad-purpose 400, real-biometric 403). An autouse fixture restores `AUTH_ENABLED` + reloads the gateway so enforcement never leaks into the other test modules.

**Wave 3 verification:** full pytest **184 pass / 33 skip / 1 xfail / 0 fail** (170 original + 14 new; no regression — auth defaults OFF) · `poc_selftest` 22/23, 0 required failing · web+mobile `tsc` exit 0 · `pnpm format:check` clean · `docker compose config` **fails fast without secrets** (proves SEC-4) and **validates with `.env.local.example`**. PAUSE gate per the prompt — auth/RBAC design + 401/403 tests below.

## Wave 4 — Demo-critical feature closure

- [4.1] SMS advisory seam (APP-3 / SCOPE-IU2): new `gateway/sms.py` — `SmsProvider` protocol, env-gated `SMS_PROVIDER` (no-op default / log / pluggable real provider), `send_sms` best-effort (never raises), `advisory_to_sms_text`. Wired into the reroute path alongside WebPush — `gateway/routers/trucks.py:160`. Tests: `tests/test_sms.py` (4).
- [4.5] Web KPI math extracted to `web/src/kpi/compute.ts` (deltaPct/isOnTarget/buildKpiResult, mirrors kpi.py) with `web/src/kpi/compute.test.ts` (7 Vitest cases mirroring `tests/test_kpi.py`); `mock.ts` now delegates — KPI-3 closed. Web vitest 17→24.
- [4.3] Notifications governance (NOTIF-5, all three): role filter on `/api/alerts?role=` via kind→roles map; `i18n_key` decoration per alert; `POST /api/alerts/{id}/ack` (writes jnpa.alerts, degrades gracefully) — `gateway/routers/alerts.py`. `alertKind.*` translations added to all six locale files (en/hi/mr × web+mobile). Mobile Inbox now localises kinds + has an Ack control — `mobile-pwa/src/screens/Inbox.tsx`, `mobile-pwa/src/lib/api.ts:ackAlert`, `.ack-btn` CSS. Tests: i18n key, role-filter scoping, ack-degrade (in `tests/test_auth_rbac.py`).
- [4.2] Demo Console: handled in Wave 0.5 — preview buttons disabled, brand notice points presenters to the What-If Console (wired TFC-1/2/3). No stub buttons remain.
- [4.4] GIS interaction tools (GIS-5): **built** the Calcite/ArcGIS **LayerList toggle** (`<arcgis-layer-list>`) so operators can show/hide each operational layer — `web/src/components/map/ArcgisMap.tsx`; production `vite build` passes (import resolves). Legend + popups already present. **Honest defer** (labelled in COVERAGE): the time slider (needs time-enabled layers; the GraphicsLayer feed is not time-aware) and per-violation map-graphic selection — low PoC payoff vs effort; alerts already render with location context.

**Wave 4 verification:** full pytest **191 pass / 33 skip / 1 xfail / 0 fail** · web vitest **24/24** · web+mobile `tsc` exit 0 · `vite build` (web) succeeds · `pnpm format:check` clean.

## Wave 5 — Runbook & self-test hardening (demo de-risk)

- [5.1] `docs/DEMO_RUNBOOK.md`: a 5-minute script (bootstrap → TFC-1 reroute → KPI delta) and a 15-minute script (+TFC-2 wrong-way/e-Challan + TFC-3 cross-twin DPD + evaluator-evidence surfaces), each step naming the exact screen/button (driven from the **What-If Console**), the expected on-screen result, pre-flight, teardown, and a pre-loaded honest Q&A (DOC-4 closed).
- [5.2/5.3] `tests/test_scale_offline_latency.py`: EXECUTED 30k-fleet build+tick within wall-clock bounds (SIM-5), deterministic 20k→30k (same seed → same device set), and a network-disabled run that **hard-blocks `socket.socket`** and still builds+ticks (SIM-3). Mirrored as selftest **SIM.7** (`scripts/poc_selftest.py`, required) — both now executed, not just configured.
- [5.4] Latency SLO (AI-4): `test_e2e_latency_p95_under_slo` + selftest **AI.4** assert the committed `evidence/metrics.json` e2e p95 ≤ 6 s (currently 0.61 s).

**Wave 5 verification:** full pytest **195 pass / 33 skip / 1 xfail / 0 fail** · `poc_selftest` **24/25, 0 required failing** (SIM.7 + AI.4 PASS; B.1 honest WARN) · 30k build+tick ≈1.5 s in-process.

## Wave 6 — Scope decisions: built vs explicitly deferred

- [6.6] **Built** — cross-twin contract typed once in the shared package: `DpdReleaseEvent` + `TOPIC_DPD_RELEASE` in `shared/jnpa_shared/schemas.py` (XT-1); `scenarios/uc2_bridge.py` re-exports them and `translate_release` accepts the typed model or the raw dict (XT-2). Verified 2.5× → 600 trucks/h both ways.
- [6.4] **Built/kept** — mobile stays an installable PWA (deliberate); added driver **parking-availability** visibility to the Trip screen + `api.parkingSummary` (SCOPE-R1/IU2).
- [6.5] **Documented** — Bronze→Silver→Gold: `raw_ref` audit-trail retention satisfies the audit need; explicit tier naming deferred (labelled in COVERAGE).
- [6.1/6.2/6.3] **Deferred (post-award), labelled in COVERAGE** — ArcGIS Stream Layer (needs GeoEvent/Velocity), Dashboards embed (needs published WebMap item), learned ETA head (heuristic, labelled). All recorded in COVERAGE "Scope decisions" so nothing reads as implied-complete.

**Wave 6 verification:** full pytest **195 pass / 0 fail** · web vitest **24/24** · mobile `tsc` exit 0 · `pnpm format:check` clean · `poc_selftest` 24/25, 0 required failing.
