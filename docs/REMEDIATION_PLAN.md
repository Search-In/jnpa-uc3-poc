# UC-3 Remediation Plan — from Audit Baseline (2026-07-31)

Derived from the 6-agent production audit. Issues: 7 Critical (C1–C7), 12 High (H1–H12), 16 Medium (M1–M16), 10 Low (L1–L10). Ordered into 4 phases: **Phase 0** makes tomorrow's demo safe, **Phase 1** makes the cross-twin story true, **Phase 2** closes security/correctness, **Phase 3** makes the repo production-bootstrappable. Implement in the exact order listed.

Conventions: "Effort" = one focused developer. Every fix ends with the named verification. Run tests per-file (`pytest tests/test_x.py`), never the whole suite on this Mac (known native abort).

---

## PHASE 0 — Demo hotfixes (Day 1, ~1.5 dev-days total)

### 0.1 (C5) Seed scripts target `jnpa.*` but runtime reads `core.*` + demo container IDs missing

- **Root cause:** All `scripts/seed_*.sql` were written pre-migration against the legacy `jnpa` schema; the v3 cutover (v3/0101/0201) moved runtime reads to `core.*` but the seeds were never ported. The doc-04 Auto-LEO containers (APLU0896946, CMAU3549370, MSCU1234566, MAEU7654320) are pinned only in `gate-data/seed.py:250-251` (service-internal), so they 404 in cargo/customs screens.
- **Why it happens:** Seeds run fine against a legacy DB, so the drift was invisible until v3-only environments; after `v3/0900` drops `jnpa`, they fail outright.
- **Files:** `scripts/seed_demo_p0.sql`, `scripts/seed_demo_cargo.sql`, `scripts/seed_uc3_completion.sql`, `scripts/seed_fleet_vehicles.sql` (audit and port every `INSERT INTO jnpa.` — 31 targets).
- **Function/Class:** SQL only.
- **Steps:**
  1. `grep -n "jnpa\." scripts/*.sql` — build the rename map (`jnpa.gates→core.gate`, `jnpa.trucks→core.truck`…). Column names differ in places (v3 renamed several); diff each against `v3/0101_core_operational_ext.sql`.
  2. Rewrite each INSERT with core table + column list; keep `ON CONFLICT DO NOTHING` everywhere for idempotency.
  3. Add the four doc-04 containers to `seed_demo_cargo.sql` (INSERT INTO `core.cargo` and, if the customs demo needs them, `core.igm_line_container`) so cross-module lookups resolve.
- **Sample:**
  ```sql
  -- seed_demo_cargo.sql (append)
  INSERT INTO core.cargo (container_number, status, shipping_line, terminal, created_at)
  VALUES ('APLU0896946','GATE_PENDING','APL','GTI', now()),
         ('CMAU3549370','GATE_PENDING','CMA','NSICT', now()),
         ('MSCU1234566','GATE_PENDING','MSC','BMCT', now()),
         ('MAEU7654320','GATE_PENDING','MAERSK','NSFT', now())
  ON CONFLICT (container_number) DO NOTHING;
  ```
  (Adjust column list to the actual `core.cargo` DDL in v3/0101.)
- **Dependencies:** none (do first — everything demo-related depends on it).
- **Effort:** 0.5 day.
- **Testing:** run each script twice against local Postgres (5434 with override) — second run must be a no-op; then `curl :8000/api/cargo/APLU0896946` → 200.
- **Verify:** every demo screen that previously showed seeded rows still shows them from `core.*`; the four container IDs resolve in Follow-the-Box/cargo search.
- **Cross-module impact:** none at runtime (data-only); unblocks 0.8 (evidence pack) and all rehearsals.

### 0.2 (C3) TFC-2 evidence "MP4" URL is fabricated

- **Root cause:** `_evidence_mp4_url()` in `scenarios/tfc2.py:189-191` mints `http://localhost:9000/evidence/{camera_id}-last10s.mp4`; nothing ever writes that MinIO object. Real evidence is a JPG written by `ai/anomaly/evidence.py` (`evidence/{alert_id}.jpg`), unhashed.
- **Why it happens:** The scenario was written against an intended clip-builder that was never implemented; the dashboard dialog trusts the payload URL.
- **Files:** `scenarios/tfc2.py` (function `_evidence_mp4_url`, and the `_enrich_alert` call that stamps it), `ai/anomaly/evidence.py` (add SHA), optionally `gateway/routers/evidence.py`.
- **Steps (demo-safe minimal fix):**
  1. Delete `_evidence_mp4_url`; instead read the real `evidence_url` off the alert payload returned by `_await_alert` (the anomaly sink already stamps it).
  2. In `ai/anomaly/evidence.py`, compute `sha256` of the JPG bytes before the MinIO put and include `evidence_sha256` in the alert payload.
  3. Route the URL through the gateway proxy (`/api/evidence/…`) instead of `localhost:9000` so it works off-box.
- **Sample:**
  ```python
  # scenarios/tfc2.py — in run(), replace the enrichment
  evidence_url = (alert.get("payload") or {}).get("evidence_url")
  await _enrich_alert(cfg, alert_id, {
      "evidence_url": evidence_url,          # real JPG, gateway-proxied
      "evidence_sha256": (alert.get("payload") or {}).get("evidence_sha256"),
  })
  ```
  ```python
  # ai/anomaly/evidence.py — before upload
  digest = hashlib.sha256(jpg_bytes).hexdigest()
  ...
  payload["evidence_sha256"] = digest
  ```
  4. Frontend: `AlertEvidenceDialog.tsx` already falls back to image rendering for non-mp4 URLs — confirm; if not, add `<img>` branch.
- **Dependencies:** none. (Full MP4 clip from `shared/jnpa_shared/frame_bus.py` is a Phase-1 nice-to-have, not needed for demo honesty.)
- **Effort:** 0.5 day.
- **Testing:** run TFC-2 (`POST /api/scenarios/tfc2/run`), open the alert in Alerts Center → evidence renders (JPG), SHA shown; `curl -I` the URL → 200.
- **Verify:** no `last10s.mp4` string remains in repo (`grep -r last10s`).
- **Cross-module impact:** Alerts Center dialog (display only); docs 04 must change "plays the 10-s MP4" → "shows the hash-verified evidence frame".

### 0.3 (H1) Police report date filter silently returns `[]`

- **Root cause:** `gateway/routers/reports.py:97-101` binds `since`/`until` as raw ISO **strings** to a `timestamptz` param; asyncpg via SQLAlchemy `text()` raises, and the `except` at :113-115 logs at debug and returns `[]`.
- **Why:** copy of a pattern that works on psycopg but not asyncpg; `trt.py:56-57` already documents the correct approach.
- **Files/Function:** `gateway/routers/reports.py`, the alert-fetch helper inside the police report route.
- **Steps:** parse to timezone-aware `datetime` before binding; log swallowed failures at `warning`.
- **Sample:**
  ```python
  from datetime import datetime, timezone

  def _parse_ts(v: str | None) -> datetime | None:
      if not v:
          return None
      dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
      return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

  if since:
      clauses.append("ts >= :since"); params["since"] = _parse_ts(since)
  if until:
      clauses.append("ts <= :until"); params["until"] = _parse_ts(until)
  ...
  except Exception as exc:
      log.warning("police_alerts_failed", error=str(exc))   # was debug
  ```
- **Dependencies:** none. **Effort:** 1 h.
- **Testing:** `curl ':8000/api/reports/police?since=2026-07-01T00:00:00Z'` → non-empty when alerts exist; add a pytest hitting the route with date params (new `tests/test_reports.py`).
- **Verify:** PDF export with a date range contains rows.
- **Cross-module impact:** Police Reports screen only.

### 0.4 (H4) FASTag `/health` permanently "degraded"

- **Root cause:** `gateway/routers/fastag.py` health probe checks `core.fastag_transactions` (plural); schema-v3 table is `core.fastag_transaction` (v3/0101:898).
- **Why:** table renamed in the v3 port; health tuple not updated.
- **Files/Function:** `gateway/routers/fastag.py`, the health endpoint's table tuple (~line 504).
- **Sample:** `for t in ("fastag_balance", "fastag_transaction", "toll_enroute"):`
- Also: `ok` requires `ulip_configured` — in demo mode this reports degraded even when healthy; add `"mode": "demo" if not ulip_configured else "live"` and drop `ulip_configured` from the `ok` conjunction (or keep and document).
- **Dependencies:** none. **Effort:** 30 min.
- **Testing/Verify:** `curl :8000/api/fastag/health` → `status: ok`, all three tables true.
- **Cross-module impact:** System Health screen card flips green.

### 0.5 (H2) Post-restart scenario reset removes zero trucks (+ mid-run reset 404)

- **Root cause:** `scenarios/runner.py:112` builds a stub cleanup tag `f"{name.upper()}:{handle_id}"` → `TFC1:…`, but scenarios tag trucks `TFC-1:{id}`, `TFC-3:{id}`, `MONSOON:demand:{id}` / `MONSOON:queue:{id}` (tfc1.py:47, tfc3.py:53, monsoon_friday.py:54-55). Also `_HANDLES` is populated only **after** `module.run()` completes (runner.py:88), so reset-by-name mid-run 404s.
- **Files:** `scenarios/runner.py`, `scenarios/tfc1.py`, `scenarios/tfc2.py`, `scenarios/tfc3.py`, `scenarios/monsoon_friday.py`.
- **Steps:**
  1. Export a `stub_cleanup(handle_id) -> dict` from each scenario module returning exactly what `reset()` needs.
  2. Runner uses it for the post-restart stub; fall back to old format only if absent.
  3. Fix mid-run: record `_PENDING[handle_id] = name` at submit; `_resolve_handle` returns a stub for pending handles too (reset then races run — acceptable; reset ops are idempotent).
- **Sample:**
  ```python
  # scenarios/tfc1.py
  def stub_cleanup(handle_id: str) -> dict:
      return {"gate_id": DEFAULT_GATE, "truck_tag": f"TFC-1:{handle_id}",
              "spillover_gates": SPILLOVER_GATES}
  ```
  ```python
  # scenarios/runner.py (stub branch)
  stub_fn = getattr(module, "stub_cleanup", None)
  cleanup = stub_fn(handle_id) if stub_fn else {"truck_tag": f"{name.upper()}:{handle_id}"}
  stub = ScenarioHandle(handle_id=handle_id, name=name, params={}, cfg=cfg, cleanup=cleanup)
  ```
- **Dependencies:** none. **Effort:** 0.5 day (incl. tests).
- **Testing:** run TFC-1 → restart scenarios container → `POST /scenarios/tfc1/reset {handle_id}` → truck-sim `GET /devices` shows zero `TFC-1:` tagged devices; repeat for monsoon (both tags).
- **Verify:** `tests/test_scenarios.py` add a stub-cleanup tag-format assertion per module.
- **Cross-module impact:** none outside scenarios; makes demo "reset twice in a row" drill safe.

### 0.6 (H3) Scenario-triggered congestion alerts leak past reset

- **Root cause:** the auto-alert raiser in `gateway/routers/traffic.py:119-157` fires TRAFFIC_CONGESTION alerts when the (scenario-nudged) forecast crosses threshold, with no scenario tag; scenario `_resolve_alerts` acks only `payload->>'scenario'` matches.
- **Fix (cheapest):** in each scenario's `reset()`, additionally ack TRAFFIC_CONGESTION alerts raised during the run window on the nudged segments:
  ```python
  await execute(
      "UPDATE core.alert SET ack = true WHERE kind='TRAFFIC_CONGESTION' AND ack=false "
      "AND ts >= :started AND payload->>'segment_id' = ANY(:segs)",
      {"started": handle.started_at, "segs": list(SPILLOVER_SEGMENTS)})
  ```
  (Better long-term: thread a `scenario` tag through the nudge → snapshot `source` column → alert payload; note as Phase-3 refactor.)
- **Files:** `scenarios/tfc1.py`, `tfc3.py`, `monsoon_friday.py` (`reset`); optionally `gateway/routers/traffic.py` to copy `source` into alert payload.
- **Effort:** 2–3 h. **Testing:** run+reset TFC-1 twice; `GET /api/alerts?kind=TRAFFIC_CONGESTION` shows no un-acked leftovers. **Impact:** Alerts Center count returns to baseline after reset — the documented "fully reversible" claim becomes true.

### 0.7 (H7) Stale congestion F1 0.8411 in five docs

- **Root cause:** model retrained (artifact: F1 0.8797, `prev_f1` 0.8411, TARGET_MET true); docs never swept.
- **Files:** `docs/ASSUMPTIONS.md:40`, `docs/COVERAGE.md:59`, `docs/DEMO_RUNBOOK.md:23,27,99`, `docs/UC3_PRODUCTION_AUDIT.md:34`. Also fix DEMO_RUNBOOK selftest count "22/23" → 25 checks / B.1 required-passing, and Postgres 5433→5434-override note, Grafana 3001.
- **Effort:** 1 h. **Verify:** `grep -rn "0.8411" docs/` returns only historical "prev" mentions. **Impact:** none (docs).

### 0.8 (H9 + H8-part) Evidence pack empty + ANPR SHA pin unset

- **Root cause:** `evidence/` is gitignored output, never regenerated after the last stack rebuild; `ANPR_YOLO_SHA256` present-but-empty in `.env.local` so the integrity check self-skips (`ai/anpr/src/anpr/detect.py:41-89`).
- **Steps:** (1) `shasum -a 256 ai/anpr/resources/license_plate_detector.pt` → paste into `.env.local` `ANPR_YOLO_SHA256=`; restart anpr; `curl :8301/healthz` shows the digest. (2) With the full stack up: `make evidence` (runs `scripts/build_evidence.py`), then `make demo-record`/screenshots.
- **Dependencies:** 0.1 (seeds) so evidence reflects demo data; stack must be up.
- **Effort:** 2 h wall-clock. **Verify:** `evidence/metrics.json` exists, latency/throughput populated; if throughput < 5 msg/s target, record it as the disclosed shortfall (do not edit the number).
- **Impact:** selftest AI.4 switches from skip to a real assertion — confirm it still passes.

---

## PHASE 1 — Make the cross-twin story true (Days 2–5)

### 1.1 (C2) TFC-3 "consumes" its own local dict — add a real `cargo.dpd_release` consumer

- **Root cause:** `scenarios/tfc3.py:60-70` publishes the event to Kafka then calls `translate_release(event)` on the same in-memory dict; `scenarios/uc2_bridge.py` has no consumer (its docstring admits the inline drive). Nothing in the repo subscribes to `TOPIC_DPD_RELEASE`.
- **Why:** PoC shortcut to keep TFC-3 self-contained without a long-running UC-II producer.
- **Good news:** the typed contract already exists — `DpdReleaseEvent` + `TOPIC_DPD_RELEASE` in `shared/jnpa_shared/schemas.py:461,488`; `jnpa_shared/kafka_io.py` already has `get_consumer`/`consume` (:150,:165).
- **Files/Functions:** `scenarios/uc2_bridge.py` (new `listen()`), `scenarios/runner.py` (start listener in app lifespan), `scenarios/tfc3.py` (`run` — wait on the consumer instead of inline call).
- **Steps:**
  1. `uc2_bridge.listen(on_profile)` — background task wrapping `kafka_io.consume(TOPIC_DPD_RELEASE, group="uc3-uc2-bridge", ...)` (blocking confluent consumer → run in `asyncio.to_thread`, marshal back with `run_coroutine_threadsafe`; adapt to the actual `consume()` signature at kafka_io.py:165).
  2. Bridge keeps an `asyncio.Queue` of received events; on receipt logs `dpd_release_consumed` and calls `translate_release`.
  3. TFC-3: publish, then `await bridge.next_event(timeout=10)`; on timeout, fall back to the inline dict **and record the step as `status="degraded", detail={"consumed": "inline-fallback"}`** so the demo never stalls but the timeline is honest.
  4. Start/stop the listener in the scenarios FastAPI lifespan (`runner.py`).
- **Sample:**
  ```python
  # scenarios/uc2_bridge.py
  _EVENTS: asyncio.Queue = asyncio.Queue(maxsize=16)

  async def listen() -> None:
      loop = asyncio.get_running_loop()
      def _pump():
          for evt in kafka_io.consume(TOPIC_DPD_RELEASE, group="uc3-uc2-bridge"):
              asyncio.run_coroutine_threadsafe(_EVENTS.put(evt), loop)
      await asyncio.to_thread(_pump)

  async def next_event(timeout: float = 10.0):
      return await asyncio.wait_for(_EVENTS.get(), timeout)
  ```
  ```python
  # scenarios/tfc3.py step 2
  try:
      consumed = await uc2_bridge.next_event(timeout=10)
      via = f"kafka:{TOPIC_DPD_RELEASE}"
  except asyncio.TimeoutError:
      consumed, via = event, "inline-fallback"
  profile = translate_release(consumed)
  ```
- **Dependencies:** Kafka up (compose); 0.5 (reset fix) merged first to avoid rebase churn.
- **Effort:** 1 day.
- **Testing:** `tests/test_scenarios.py` — publish a `DpdReleaseEvent` with `kafka_io.produce` from the test, assert `next_event` yields it; run TFC-3 end-to-end, timeline step 2 shows `kafka:` trigger not `inline-fallback`.
- **Verify:** during a live run, `kafka-console-consumer --topic cargo.dpd_release --group uc3-uc2-bridge` shows committed offsets advancing.
- **Cross-module impact:** none to gateway; docs 05 flow-2 verification checkbox becomes truthful; an external UC-II stack can now genuinely drive TFC-3 by publishing to the topic.

### 1.2 (C1) `jnpa.crosstwin.deferred-arrival` / `DeferredArrivalWindow` contract + TAS consumer missing

- **Root cause:** the UC-2→UC-3 contract #1 (doc 05) was never implemented in this repo — no topic constant, no typed event, no consumer; TAS is the in-memory mock `gateway/tas_mock.py`.
- **Files/Functions:** `shared/jnpa_shared/schemas.py` (new model + topic), `gateway/tas_mock.py` (new `apply_deferred_window`), `gateway/main.py` (new pump alongside `alert_pump`, ~:399), `gateway/pumps.py`, optionally `gateway/routers/rms_tas.py` (surface applied windows).
- **Steps:**
  1. Add to schemas:
     ```python
     class DeferredArrivalWindow(_Base):
         correlation_id: str
         window_start: datetime
         window_min: int = 90
         slot_cap: int = 4
         gate_id: str | None = None
         source: str = "UC-II"

     TOPIC_DEFERRED_ARRIVAL = "jnpa.crosstwin.deferred-arrival"
     ```
  2. `tas_mock.apply_deferred_window(win)`: mark slots inside `[window_start, +window_min]` as `RESCHEDULED`, cap new bookings at `slot_cap`, remember `correlation_id`.
  3. New `deferred_arrival_pump` in gateway startup: consume topic → validate to the model → apply → `ws_hub.broadcast("tas", …)` → record a decision (`fallback.record_decision`-style) so it shows in `/api/debug/decisions`.
  4. Expose `GET /api/tas/deferred-windows` for the demo proof.
- **Dependencies:** 1.1 (shares the consumer pattern). Coordinate the event shape with the UC-2 repo (PoC_2) — its S2 scenario is the producer; agree field names before wiring their side.
- **Effort:** 1–1.5 days.
- **Testing:** unit-test `apply_deferred_window`; integration: `kafka_io.produce(TOPIC_DEFERRED_ARRIVAL, DeferredArrivalWindow(...).model_dump())` → within 2 s `GET /api/tas/slots` shows RESCHEDULED slots + cap.
- **Verify:** fire UC-2's S2 against this stack (or the test producer) → System Health decision log shows the consumption; What-If narration can point at it.
- **Cross-module impact:** `rms_tas`/TAS booking honors the cap (booking endpoint must check active windows — one extra guard in `rms_tas.py`).

### 1.3 (H11) WS `traffic` frame is dead (consumer of a topic nothing produces)

- **Root cause:** gateway `traffic_pump` (main.py:403) consumes Kafka `traffic.snapshots`; no producer exists — the congestion service persists snapshots to Postgres and publishes only `traffic.predictions`.
- **Fix (choose one; recommend A):**
  - **A.** Publish: in `ai/congestion/infer.py` (~:299 where snapshots are persisted), also `kafka_io.produce("traffic.snapshots", row)`. One line + import.
  - **B.** Remove: delete the pump registration and the dead listener `web/src/screens/LiveOperations.tsx:158`.
- **Effort:** 2 h. **Testing:** open Live Ops with devtools WS tab → `traffic` frames arrive each minute; heatmap updates without polling.
- **Impact:** Live Ops becomes push-updated (A); no other consumers.

### 1.4 (M1) TFC-2 bypasses the real enforcement lifecycle

- **Root cause:** TFC-2 calls the `/api/echallan/issue` stub (`scenario_ext.py:112-169` — flat fine, sha ID) instead of the real `/api/violations` machine (`gateway/enforcement.py` — DETECTED→CLOSED, multipliers, `core.challan_seq`, hash-chained audit).
- **Fix:** in `tfc2.py` after the alert lands, open a case via the violations API (`POST /api/violations/commit` with the WRONG_WAY detection + plate + evidence), then advance it to CHALLAN_ISSUED; stamp the real challan number onto the alert payload. Keep the stub for backward compatibility but stop calling it from TFC-2.
- **Files:** `scenarios/tfc2.py`, (read-only reference) `gateway/routers/violations.py:326+`.
- **Dependencies:** 0.2 (evidence URL fix) so the case carries the hashed evidence.
- **Effort:** 0.5–1 day. **Testing:** run TFC-2 → `GET /api/violations/cases` shows a case with audit rows; challan number in the `ECH-…` sequence starting 1001; fine = ₹9,375 for night-time HGV.
- **Verify:** demo beat "case lifecycle + hash-chained audit" now runs off TFC-2 itself.
- **Impact:** Police Reports challan tab now includes scenario-generated challans (desired).

---

## PHASE 2 — Security & correctness (Days 6–9)

### 2.1 (C7) Auth off by default + RBAC policy blind spots

- **Root cause:** `AUTH_ENABLED` defaults false (`gateway/auth.py:316`); `_POLICY` (auth.py:78-138) ends with "everything else visible to any authenticated stakeholder", leaving `/api/cargo` writes, `PUT /api/zones` (via `/api/zones`), `/api/workflows` CRUD, `/api/parking` writes, `/api/trucks/{id}/route` (reroute) etc. open to any role.
- **Why:** policy grew module-by-module; operational routers added after the policy freeze fell through to the default.
- **Files/Function:** `gateway/auth.py` `_POLICY` (+ make it method-aware), `.env.local` (`AUTH_ENABLED=true` for demo profile), `docs/DEMO_RUNBOOK.md` (login step).
- **Steps:**
  1. Extend policy entries to `(prefix, roles, methods|None)`; `None` = all methods. Enforcement loop checks method when present.
  2. Add:
     ```python
     ("/api/cargo",     CONTROL_ROOM | {Role.CUSTOMS.value}, None),
     ("/api/zones",     CONTROL_ROOM, frozenset({"PUT", "POST", "DELETE"})),  # reads stay open
     ("/api/geo/evaluate",     {Role.DRIVER.value} | CONTROL_ROOM, None),    # PWA needs this
     ("/api/geo/zones-active", {Role.DRIVER.value} | CONTROL_ROOM | {Role.CUSTOMS.value}, None),
     ("/api/workflows", CONTROL_ROOM, None),
     ("/api/parking",   {Role.DRIVER.value} | CONTROL_ROOM, None),
     ("/api/trucks",    {Role.DRIVER.value} | CONTROL_ROOM, frozenset({"POST"})),
     ("/api/accidents", CONTROL_ROOM | {Role.TRAFFIC_POLICE.value}, frozenset({"POST","PUT","PATCH"})),
     ("/api/ai",        CONTROL_ROOM, frozenset({"POST"})),
     ("/api/camera-ai", CONTROL_ROOM, frozenset({"POST"})),
     ("/api/nvr",       CONTROL_ROOM, None),
     ("/api/rms-tas",   CONTROL_ROOM | {Role.CUSTOMS.value}, frozenset({"POST"})),
     ("/api/reefer",    CONTROL_ROOM, frozenset({"POST"})),
     ("/api/ldb",       CONTROL_ROOM | {Role.CUSTOMS.value}, frozenset({"POST"})),
     ("/api/trt",       CONTROL_ROOM, frozenset({"POST"})),
     ```
     **Careful:** longest-prefix-wins ordering; PWA (DRIVER) must keep: trucks reads, route/latest+ack, parking allocate/release, geo evaluate/zones-active, alerts read/ack, identity enrol-request, push, driver profile, vahan rc — run the PWA e2e after.
  3. Add the missing `require_uploader` dependency to `POST /api/shipping-lines/import` (`gateway/routers/shipping_lines.py:281-290`) to match its `/upload` sibling.
  4. Demo profile: set `AUTH_ENABLED=true` in `.env.local`; rehearse the 6-user login matrix.
- **Dependencies:** run before 2.2–2.4 (they assume role context exists).
- **Effort:** 1 day incl. regression pass.
- **Testing:** extend `tests/test_auth_rbac.py`: DRIVER token → `PUT /api/zones` 403, `POST /api/cargo/.../release` 403, `POST /api/geo/evaluate` 200; CONTROL_ROOM → all 200.
- **Verify:** with auth on, walk every PWA screen and every web screen per role — no unexpected 403s.
- **Cross-module impact:** wide — every frontend; budget the regression pass.

### 2.2 (C6) `/api/violations/enforce` fabricates classification and challans synthetic plates

- **Root cause:** `gateway/routers/violations.py:418-447` derives the violation class from `sha256(image bytes)`; :581-605 falls back to a synthetic plate when ANPR is down and continues into a real sequenced, hash-chained challan.
- **Why:** demo continuity was prioritized over enforcement integrity.
- **Files/Function:** `gateway/routers/violations.py` (`enforce`, the detect/classify helpers).
- **Steps:**
  1. When ANPR is unavailable or plate confidence < threshold: create the case as `DETECTED` with `provisional: true`, **do not issue a challan**; return 202 with `reason: "plate_unverified"` and surface it in the console's pending queue for operator confirmation (the Commit→Enforce UI already exists — enforce just refuses the last hop).
  2. Replace sha-derived classification with the committed detection's class; if absent, `classification: "UNVERIFIED", degraded: true`.
  3. Never write `degraded/provisional` results into the audit chain as confirmed facts — audit rows record the provisional state explicitly.
- **Sample:**
  ```python
  if plate_source != "ANPR_LIVE" or plate_conf < settings.enforce_min_conf:
      case_id = await enforcement.open_case(..., status="DETECTED",
                                            payload={"provisional": True, "reason": "plate_unverified"})
      return JSONResponse(status_code=202, content={"case_id": case_id,
                          "challan": None, "provisional": True})
  ```
- **Dependencies:** 2.1 (roles), 1.4 (TFC-2 now uses this path — keep its committed detection flowing so TFC-2 still issues a challan: scenario supplies a confirmed plate).
- **Effort:** 1 day.
- **Testing:** force Camera fault chain to SYNTHETIC via Demo Console → Enforce → 202 provisional, no challan row; restore LIVE → full challan. Unit tests in a new `tests/test_violations.py` (router currently has zero tests).
- **Verify:** `core.challan` gains no rows with `provisional` plates; demo narration changes from stub-excuse to "human gate on degraded input" (a scoring asset).
- **Cross-module impact:** TFC-2 (1.4), Police Reports enforcement tab (shows provisional queue).

### 2.3 (H5) `journey.py` labels fabricated timeline as live

- **Root cause:** timeline timestamps are offsets from a fixed anchor `2026-06-13` (`gateway/routers/journey.py:50,59-60`), IDs sha-minted, yet stages tagged `data_mode:"live"`, `simulated:false` (:383-434).
- **Fix:** tag fabricated stages `data_mode:"replay"`, `simulated:true`; where a stage IS backed by a real row (`core.cargo`, `core.gate_event`, `core.parking_transaction`), keep `live` — compute per-stage, not globally. Frontend FollowTheBox already renders badges — verify it picks up the new values.
- **Files:** `gateway/routers/journey.py` (stage-builder functions).
- **Effort:** 0.5 day. **Testing:** `GET /api/journey/container/{seeded}` → mixed badges; stage backed by a real gate_event says live, synthesized ones say replay.
- **Verify:** matches the pack's "three data classes, badge on screen, never blur" ground rule.
- **Impact:** Follow-the-Box screen badges only.

### 2.4 (H6) Hash chain: no verify endpoint + write race

- **Root cause:** `gateway/enforcement.py:212-237` `_audit` SELECTs prev hash and INSERTs in two autocommit statements (fork risk under concurrency); no endpoint recomputes the chain despite doc 04 "offer to verify the chain".
- **Files/Functions:** `gateway/enforcement.py` (`_audit`), `gateway/routers/violations.py` (new route).
- **Steps:**
  1. Make `_audit` atomic: single `engine.begin()` transaction, `SELECT … FROM core.violation_case WHERE id=:cid FOR UPDATE` to serialize per-case, then read prev hash + insert.
  2. Add:
     ```python
     @router.get("/cases/{case_id}/verify-chain")
     async def verify_chain(case_id: str):
         rows = await fetch_all("SELECT id, prev_hash, hash, actor, action, detail, ts "
                                "FROM core.case_audit WHERE case_id=:c ORDER BY id", {"c": case_id})
         prev = ""
         for r in rows:
             expected = _hash_row(prev, r)          # reuse the writer's canonical-json helper
             if r["hash"] != expected or r["prev_hash"] != prev:
                 return {"valid": False, "broken_at": r["id"]}
             prev = r["hash"]
         return {"valid": True, "length": len(rows)}
     ```
  3. Add a "Verify chain" button in the case dialog (`web/src` violations panel) calling it.
- **Effort:** 0.5–1 day. **Testing:** new `tests/test_violations.py::test_chain_verify` — happy path + manual UPDATE of one hash → `valid:false, broken_at` correct; concurrency test firing 10 parallel `_audit`s on one case → chain stays linear.
- **Verify:** demo Q&A "offer to verify the chain" is now a click.
- **Impact:** none beyond violations.

### 2.5 (H12) PWA reroute endpoints unscoped + `LAST_REROUTE` in-memory

- **Root cause:** `GET /api/trucks/{id}/route/latest` and `POST /route/ack` (`gateway/routers/trucks.py:240-251`) never compare the DRIVER token's device binding to the path id; `LAST_REROUTE` dict lost on gateway restart (breaks the PWA's polling fallback).
- **Steps:** (1) in both handlers, when role==DRIVER, read `device_id` from token claims (minted by `/api/auth/device-token`) and 403 on mismatch. (2) persist advisories to `core.reroute_advisory` (new small table or reuse an existing notification table) with write-through to the dict as cache; `latest` reads dict→DB.
- **Files:** `gateway/routers/trucks.py`, `gateway/auth.py` (claim helper), migration for the table (fold into Phase-3 DDL work if preferred).
- **Dependencies:** 2.1 (claims available). **Effort:** 0.5–1 day.
- **Testing:** pair as TRK-000001, request TRK-000002's `/route/latest` → 403; restart gateway mid-scenario → PWA poll still finds the advisory.
- **Impact:** PWA only; TFC-1 unaffected (server-side dispatch path unchanged).

### 2.6 (M8) OTP surface: dead endpoints, `dev_otp` echo, ungated secret

- **Root cause:** PWA OTP/Phone-auth flows were removed but `otp.py` remains fully mounted (`gateway/main.py:561`) incl. `firebase-verify`; `dev_otp` is echoed to callers outside prod (`otp.py:126-127`) → mintable DRIVER JWTs; OTP hashing uses its own ungated default secret `"jnpa-dev-secret"` (:56) not covered by `validate_auth_config`.
- **Steps:** unmount the router (delete the `include_router` line) — or if kept for the standalone check-in flow, gate `dev_otp` echo behind `settings.environment == "dev"` AND `AUTH_ENABLED is false`, and source the secret from `AUTH_JWT_SECRET` config with the same startup guard.
- **Files:** `gateway/main.py:561`, `gateway/routers/otp.py:56,126-127`; delete stale "Phone Auth" comment `mobile-pwa/src/lib/firebase.ts:1`.
- **Effort:** 2 h. **Testing:** `POST /api/auth/otp/request` → 404 (or no `dev_otp` field). **Impact:** none — no client uses it.

---

## PHASE 3 — Production bootstrap & hardening (Days 10–16)

### 3.1 (C4) v3 schema is not bootstrappable from the repo

- **Root cause:** ~20 runtime-critical `core` base tables (transporter, driver, vehicle, customs family, shipping-lines family, `bathymetry_survey`) have no DDL in-repo — v3/0102 ALTERs them assuming an external `schema.sql`; `CREATE SCHEMA mart` exists nowhere; no migration runner (compose mounts only `init.sql`); duplicate migration number 0038.
- **Steps:**
  1. From the known-good local v3 DB: `pg_dump --schema-only --no-owner --schema=core --schema=mart jnpa_schema_v3 > infra/postgres/v3/0099_base_schema.sql`; prepend `CREATE SCHEMA IF NOT EXISTS core; CREATE SCHEMA IF NOT EXISTS mart;`. Review: strip objects already created by later numbered files to keep the chain linear (or regenerate 0101+ as no-ops — simpler: keep 0099 as full baseline and mark 0101–0110 as already-applied for fresh installs).
  2. Write `scripts/apply_migrations.sh`: creates `core.schema_migrations(filename, applied_at)`, applies `infra/postgres/v3/*.sql` in filename order inside transactions, skipping applied ones; wire into compose as a one-shot `migrate` service and into `make bootstrap-check`.
  3. Rename `migrations/0038_perf_pdf_upload.sql` → `0052_perf_pdf_upload.sql` (kill the duplicate number).
  4. Fix `v3/0104` ordering: either move its content into `0203` or guard it with a preflight `DO $$ … RAISE IF backfill missing $$`.
- **Files:** `infra/postgres/v3/` (new 0099 + runner), `docker-compose.yml`, `Makefile`, `infra/postgres/migrations/0038_perf_pdf_upload.sql` (rename).
- **Dependencies:** access to a provisioned v3 DB to dump (exists locally). Do **after** demo week freeze — this touches infra.
- **Effort:** 1.5–2 days incl. fresh-machine dry run.
- **Testing:** on a clean Postgres container: run the runner → all migrations apply → run ported seeds (0.1) → `make bootstrap-check` OK → gateway boots, `/healthz` green, smoke a screen per module.
- **Verify:** `docker compose down -v && make up` succeeds on a machine that has never seen the external schema.sql.
- **Cross-module impact:** everything (bootstrap path) — hence phase-gated behind demo.

### 3.2 (M10) `core.challan_seq` loses START 1001 on fresh v3

- **Root cause:** `v3/0101:2005` creates the sequence without `START 1001` (legacy `init.sql:574` had it); only the 0201 backfill `setval` restores it.
- **Fix:** in the new 0099/0101 consolidation (3.1): `CREATE SEQUENCE IF NOT EXISTS core.challan_seq START 1001;` plus an idempotent guard migration `SELECT setval('core.challan_seq', GREATEST(nextval('core.challan_seq')-1, 1000));`
- **Effort:** 30 min (fold into 3.1). **Verify:** fresh DB → first challan is `ECH-YYYY-001001`-series.

### 3.3 (M11 + M12) FK and index gaps on hot paths

- **Root cause:** v3/0101 ships 204 indexes but only ~15 FKs; `core.alert` lacks a `kind` index though `/api/alerts` filters on it; `core.rfid_read` lacks `tag_id`.
- **Fix (new migration `v3/0111_constraints.sql`):**
  ```sql
  CREATE INDEX IF NOT EXISTS idx_alert_kind_ts ON core.alert (kind, ts DESC);
  CREATE INDEX IF NOT EXISTS idx_rfid_read_tag ON core.rfid_read (tag_id, ts DESC);
  ALTER TABLE core.challan     ADD CONSTRAINT fk_challan_case    FOREIGN KEY (case_id) REFERENCES core.violation_case(id) NOT VALID;
  ALTER TABLE core.case_audit  ADD CONSTRAINT fk_audit_case      FOREIGN KEY (case_id) REFERENCES core.violation_case(id) NOT VALID;
  ALTER TABLE core.cargo_event ADD CONSTRAINT fk_cargoev_cargo   FOREIGN KEY (container_number) REFERENCES core.cargo(container_number) NOT VALID;
  -- VALIDATE CONSTRAINT in a follow-up window after orphan cleanup
  ```
  Precede with orphan-detection SELECTs; clean or archive orphans before `VALIDATE`.
- **Effort:** 0.5 day. **Testing:** orphan queries return 0 post-cleanup; `EXPLAIN` on `/api/alerts?kind=` uses the new index. **Impact:** none at API level.

### 3.4 (M2) Workflow engine: in-memory source of truth, `actions_fired` fires nothing

- **Root cause:** rules persisted to `core.automation_rule` but runtime evaluates the `_RULES` dict (`gateway/routers/workflows.py:64-66`); `/evaluate` computes matches then returns `actions_fired` without dispatching (:329-334).
- **Steps:** (1) load rules from DB at startup + on mutation (drop the dict as truth, keep as cache). (2) implement the two demo-relevant actions: `NOTIFY` → `notifications.dispatch_alert(...)`; `PROPOSE_REPLAN`/`ADVISORY` → insert a pending run row + WS `operator_banner`. (3) ADVISORY vs AUTO gate: AUTO dispatches immediately, ADVISORY waits for the existing Approve endpoint.
- **Files:** `gateway/routers/workflows.py`; reuse `gateway/notifications.py`.
- **Effort:** 1 day. **Testing:** new `tests/test_workflows.py`: create rule (queue≥15→NOTIFY), inject condition, `/evaluate` → notification row + WS frame; ADVISORY rule → pending run, approve → fires.
- **Verify:** doc-04 checklist "Workflows: 4 templates, rule CRUD + evaluate" demo beat produces observable output.
- **Impact:** Notifications/Alerts volume — rate-limit AUTO rules (max 1 fire/rule/5 min).

### 3.5 (M14 + M16) In-memory `CHECKINS` + `/checkin` poisoning; error-handling normalization

- **Root cause:** trucks TERTIARY position source is a process-local dict fed by the unauthenticated `/checkin` form (`trucks.py:47`); router error handling is bimodal (raw-500 vs silent-`[]`).
- **Steps:** (1) persist check-ins to `core.truck_checkin` (device_id, lat, lon, ts) with upsert; require the device-token when auth is on (add `/checkin` policy entry accordingly). (2) introduce one `guard_db()` context helper in `shared/jnpa_shared/db.py` that converts DB exceptions into structured 503 `{"error":"db_unavailable"}`; sweep the raw-500 routers (marine/berthing/cfs-ecy/performance/shipping-lines/drivers-master/vahan-intel/trt/reefer/nvr/camera_ai/double_trip/accidents) and the silent-`[]` routers onto it; fix `alerts/ack` to report failed UPDATE.
- **Effort:** 1.5 days (sweep). **Testing:** stop Postgres → sampled endpoints return 503 JSON not 500/`[]`; ack on missing id → `ack:false`.
- **Impact:** frontends already handle non-200 (verify toast paths).

### 3.6 (M13) Mock rows laundered into real tables (LDB, bottlenecks)

- **Root cause:** `ldb.py:150-172` persists mock movements into `core.ldb_movement`; `bottlenecks.py:232-300` persists fallback jams (`source` column is the only discriminator).
- **Fix:** default-exclude mock rows in read queries (`WHERE source != 'MOCK'` unless `?include_mock=true`); badge them in the UI where shown; never alert off mock-sourced bottleneck snapshots.
- **Effort:** 0.5 day. **Verify:** with LDB in mock rung, the movements table shows badge; KPI aggregates exclude mock.

### 3.7 (M3 + M4 + M5) PWA parity & tests

- **M3 gate-queue parity:** add a queue chip to PWA Home/Trip reading the same `GET /api/trucks?state=AT_GATE_QUEUE` count (or better, `kpi/strip` queue_length) the control room uses. Files: `mobile-pwa/src/screens/Home.tsx`, `lib/api.ts`. 0.5 day.
- **M4 stale e2e:** update `mobile-pwa/e2e/reroute.spec.ts:34,68` selectors ("Start Trip" → "Start Navigation"); re-record the <5 s reroute evidence run. 2 h.
- **M5 i18n:** add the 38 `enrol.*` keys to `hi.json`/`mr.json`; route the hardcoded Trip/AlertCenter/Inbox strings through i18n. 0.5 day.
- **Testing:** Playwright green on both PWA specs; language switch on Enroll shows translated wizard.
- **Verify:** doc-05 checklist "PWA gate-queue strip == control-room T-02" now passes with the same number on both screens.

### 3.8 (M6 + M7) Customs upload UI + td-upload cap (audit-pack F1/F2)

- **M6:** add `POST /api/customs/upload` (multipart wrapper dispatching CHPOI03/10/13, .TXT→RMS, xlsx→LEO/SB via the existing `services/customs` parser dispatch) + a Customs UploadPanel clone of `cfs/UploadPanel.tsx`. Files: `gateway/routers/customs.py`, `services/customs/service.py`, `web/src/screens/` (new panel). 1 day.
- **M7:** raise `_MAX_UPLOAD_BYTES` to 30 MB (`transporters_drivers_upload.py:38`) so the 24 MB PDP file imports from the UI. 15 min.
- **Testing:** upload an IGM XML twice → SUCCESS then SKIPPED_DUPLICATE; upload `PDP Details.xlsx` → 200.

### 3.9 (M9) Orphaned Kafka surface

- **Root cause:** contract-evidence producers with no consumers: `vehicle.confirmed`, `traffic.predictions`, `truck.eta`, 5 backbone topics; fastag service instantiated without a producer (`fastag.py:80`); 3 unused topic constants.
- **Fix:** decide per topic — wire the cheap wins (`vehicle.confirmed` → gateway pump persisting to `core.vehicle_confirmation` + WS frame proves the RFID↔ANPR fusion live; `traffic.predictions` → optional Live Ops overlay), delete the never-used constants, and pass the producer to `FastagService` or delete its publish path. Document the remaining backbone topics as "contract evidence, consumer post-award" in ASSUMPTIONS.md.
- **Effort:** 1 day. **Verify:** `kafka-ui` (:8080) shows no consumer-less topics except documented ones.

### 3.10 (M15) Engine cache eviction vs 5 DSNs

- **Root cause:** `shared/jnpa_shared/db.py:28` `@lru_cache(maxsize=4)` with 5 configured DSNs → silent re-create/leak of pooled engines.
- **Fix:** `maxsize=None` (bounded naturally by distinct DSNs) or explicit dict keyed by DSN with `dispose()` on replacement.
- **Effort:** 30 min. **Testing:** unit test creating 6 engines → same object identity per DSN. **Impact:** all services (shared lib) — deploy together.

---

## LOW (fold into Phase 3 tail, ~2 days total)

| # | Issue | Root cause | Fix | Files | Effort | Verify |
|---|---|---|---|---|---|---|
| L1 | Orphaned screen | superseded by GeoAnalytics | delete file | `web/src/screens/GeofenceEnforcement.tsx` | 10 min | build passes, no imports break |
| L2 | Hardcoded Jaeger link | dev-only URL committed | `import.meta.env.VITE_JAEGER_URL ?? hide link` | `web/src/screens/WhatIfConsole.tsx:388` | 15 min | link hidden when env unset |
| L3 | Dead fallback endpoint | path typo'd vs real route | change 2nd candidate to `/api/traffic/congestion/metrics` | `web/src/data/live.ts:337` | 10 min | fallback resolves when 1st blocked |
| L4 | "Decision ring" missing | docs describe unbuilt viz | either add a small donut (healthy/degraded/down) to SystemHealth or fix docs to "decision log" | `web/src/screens/SystemHealth.tsx` or docs | 2 h / 10 min | screen matches doc wording |
| L5 | Police tab-count drift | docs say 12, code has 11+6 | fix doc 04 §4 | demo pack doc | 10 min | — |
| L6 | Duplicated helpers | copy-paste growth | extract `require_uploader`, `_iso`, `_parse_ts`, `Page` into `gateway/util.py` / `jnpa_shared`; drop camera_ai's 2nd ISO-6346 impl for `jnpa_shared.iso6346` | ~10 routers | 0.5 day | tests green, `grep -c "def _iso"` == 1 |
| L7 | README/runbook drift | ports/counts changed | Grafana 3001, Postgres 5434-override note, selftest 25 checks, QR-pairing → Vehicle-ID in PWA README | `README.md:49,221,229`, `mobile-pwa/README.md`, `docs/DEMO_RUNBOOK.md` | 1 h | docs match compose |
| L8 | Healthchecks 5/26 services | only infra containers covered | add `healthcheck:` (curl /healthz) to gateway, scenarios, congestion, anomaly, truck-sim, parking, gate-data, identity, carbon, empty-container | `docker-compose.yml` | 2 h | `docker compose ps` all healthy |
| L9 | Debug leftovers | dev scraping code | remove `print("payload ------>")` (`gateway/routers/marine_live_vessels.py:124`), delete unreachable 502 branch (:172-178), add `monsoon` to `.PHONY`; decide the MarineTraffic scrape (ToS risk — recommend feature-flag default-off) | as listed | 1 h | grep clean |
| L10 | Stale tooling defaults | pre-v3 era | `tools/migrate_to_rds/config.py:93` default `MIGRATE_SCHEMA=core`, dbname `jnpa_schema_v3`; also restore/point `ai/anpr/eval/evaluate_real.py` or retarget the Makefile anpr-eval targets at `bench.py` (pairs with H8) | 2 files | 1 h | `make anpr-eval-real --dry-run` resolves |

---

## Rollout summary

| Phase | Items | Effort | Exit criterion |
|---|---|---|---|
| 0 (Day 1–2) | C5, C3, H1, H4, H2, H3, H7, H9/H8-pin | ~1.5 d | Demo dry-run: all 4 scenarios run+reset twice; all doc-04 worked examples resolve on-screen; evidence pack regenerated |
| 1 (Day 2–5) | C2, C1, H11, M1 | ~4 d | `kafka-console-consumer` proves both cross-twin topics consumed; TFC-2 drives the real case machine |
| 2 (Day 6–9) | C7, C6, H5, H6, H12, M8 | ~4 d | RBAC matrix test green; provisional-enforcement path demoable; chain-verify button works |
| 3 (Day 10–16) | C4, M10–M16, M2–M7, M9, L1–L10 | ~6.5 d | `docker compose down -v && make up` on a clean machine → green bootstrap; fresh seeds; Playwright + targeted pytest green |

Total ≈ **16 dev-days** (one developer) to 100% of this list; demo-ready after Phase 0, cross-twin-credible after Phase 1, production-candidate after Phase 3.

**Standing rules while executing:** never run the whole pytest suite on this Mac (native abort — run per-file); rebuild the gateway image after any `shared/` edit; web build needs the 8 GB Node heap; freeze + tag the repo before demo week and stop CI deploys per pack doc 07 §1.4.
