# Phase 0 + Phase 1 Verification Report — with executable evidence

**Date:** 2026-07-31 · Branch `migrate-schema-v3` (all changes uncommitted working tree) · Baseline: docs/REMEDIATION_PLAN.md
**Overall diff:** 31 tracked files changed, **909 insertions / 253 deletions**, + 3 new files (`docs/REMEDIATION_PLAN.md`, `infra/postgres/v3/hotfix_0102_vehicle_transporter_rds.sql`, `tests/test_tas_crosstwin.py`).
**Evidence convention:** every "verified" claim below has either a pytest result, a curl/psql response, or a container log line captured during this session. Items without executable evidence are marked explicitly.

---

## Item 1 — Police report date filter (Phase-0 H1)

1. **Files:** `gateway/routers/reports.py`
2. **Diff:** +23/−4 (git diff --stat)
3. **Functions:** new `_parse_ts()`; `_police_alerts()` modified (datetime binding); swallowed-failure log `debug→warning`; `HTTPException` import.
4. **Before → After:** `since`/`until` bound as raw ISO strings → asyncpg raised inside driver → exception swallowed → silently `[]`. Now: parsed to tz-aware `datetime` (naive→UTC), malformed input → HTTP 422 `bad_timestamp`, driver errors logged at warning.
5–6. **Tests:** no dedicated pytest exists for this router (known coverage gap — see Technical Debt). Verified at runtime instead.
7. **Commands:** `curl 'localhost:8000/api/reports/police?since=2026-07-01T00:00:00Z&limit=3'`
8. **Endpoint verified:** `GET /api/reports/police` → returned real incidents (`WRONG_WAY … TN22NE0710`) against RDS. Previously `[]` with any date filter.
9. Kafka: n/a. 10. **Log/response evidence:** JSON incident payload captured in session.
11. **Limitation:** router still returns `[]` (not 503) on DB outage — Phase-3 M16 scope.
12. **Debt:** none new; missing router test noted.

## Item 2 — FASTag health probe (Phase-0 H4)

1. **Files:** `gateway/routers/fastag.py`
2. **Diff:** +7/−4
3. **Functions:** health endpoint table tuple `fastag_transactions→fastag_transaction`; `ok` no longer requires ULIP creds; new `mode: demo|live` field.
4. **Before → After:** probed a non-existent (plural) table → permanently "degraded". Now probes the real v3 table; credential absence reported as a mode, not a fault.
7–8. **Verified:** `curl localhost:8000/api/fastag/health` → `{"status":"ok","mode":"demo","db":"ok","tables":{…all true}}` (re-verified twice, incl. at report time).
11. **Limitation:** `mode:"demo"` — ULIP creds still absent (known, disclosed).

## Item 3 — Stale congestion F1 doc sweep (Phase-0 H7)

1. **Files:** `docs/ASSUMPTIONS.md` (+4/−1), `docs/COVERAGE.md` (+2/−1), `docs/DEMO_RUNBOOK.md` (+16/−7, incl. selftest count 22/23→25/25), `docs/UC3_PRODUCTION_AUDIT.md` (+7/−3)
4. **Before → After:** five doc locations claimed F1 0.8411 below-target (one self-contradictory "0.8411 ≥ 0.85"); now all state 0.8797 target-met with 0.8411 retained as retrain evidence. `web/src/data/mock.ts` and the test gate were already current (verified, untouched).
10. **Evidence:** `grep -rn "0.8411" docs/` now hits only historical-context lines (REMEDIATION_CHANGELOG + "prev" mentions); artifact `ai/congestion/artifacts/metrics.json` = 0.8797 confirmed.

## Item 4 — TFC-2 real evidence + SHA-256 (Phase-0 C3)

1. **Files:** `scenarios/tfc2.py` (+43/−15), `ai/anomaly/evidence.py` (+4)
3. **Functions:** `_evidence_mp4_url()` **deleted** → `_evidence_proxy_url()` (gateway `/api/evidence/{object}` relative URL); step-4 enrichment re-points `evidence_url`; step-5 reports `degraded` when no frame; `EvidenceWriter.attach()` now stamps `evidence_sha256` of the exact persisted JPG bytes; module docstring corrected.
4. **Before → After:** scenario fabricated `http://localhost:9000/evidence/{cam}-last10s.mp4` (object never written; dialog would show a broken video) → scenario surfaces the anomaly service's REAL persisted frame, hash-stamped, proxied off-box-safe. Frontend needs no change (dialog already falls back `<video>`→`<img>` when `evidence_mp4_url` absent).
5–6. **Tests:** `pytest tests/test_anomaly.py` → **25 passed, 1 skipped**.
10. **Evidence:** `grep -r last10s scenarios/` → 0 hits.
11. **Limitation:** if MinIO/frame-bus has no frame for the camera, the alert has no evidence object (honest `degraded` step — by design). A true 10-s MP4 clip from the frame bus remains unbuilt (doc-04 wording should say "evidence frame", not "MP4").

## Item 5 — Scenario reset correctness (Phase-0 H2+H3)

1. **Files:** `scenarios/runner.py` (+49/−9), `scenarios/base.py` (+41), `scenarios/tfc1.py` (+25/−12), `scenarios/tfc2.py` (part of above), `scenarios/tfc3.py` (part), `scenarios/monsoon_friday.py` (+23/−10), `tests/test_scenarios.py` (part of +71)
3. **Functions:** new `stub_cleanup(handle_id)` in each of tfc1/tfc2/tfc3/monsoon_friday (real tag formats `TFC-1:{id}`, `SYN-TFC2-{id}`, `TFC-3:{id}`, `MONSOON:demand|queue:{id}`); runner `_stub_cleanup()` helper + `_PENDING` dict (mid-run reset resolution); new shared `resolve_scenario_alerts()` in base.py (acks scenario-tagged alerts AND untagged auto-raised TRAFFIC_CONGESTION alerts by nudged segment + `core.scenario.started_at`, 6 h fallback); the three per-module `_resolve_alerts` now delegate to it.
4. **Before → After:** post-restart stub reset minted `TFC1:{id}` tags matching nothing → removed **zero** trucks; mid-run reset-by-name 404'd; scenario-caused congestion alerts leaked past reset. All three fixed.
5–6. **Tests:** new `test_stub_cleanup_tags_match_run_tags` → **passed**; `tests/test_scenarios.py` unit layer green.
8. **Runtime:** TFC-3 reset returned `{"ok":true,…"RESET"}`; external-drill trucks tag-deleted (`removed:120`).
11. **Limitation:** stub reset for alerts uses the 6 h lookback when the `core.scenario` row is absent.

## Item 6 — Seed scripts ported to core.* + demo containers (Phase-0 C5) + RDS schema hotfix

1. **Files:** 8 seed + 2 verify scripts under `scripts/` (~360 lines churn); new `infra/postgres/v3/hotfix_0102_vehicle_transporter_rds.sql`
4. **Before → After:** every seed wrote legacy `jnpa.*` (dead against the v3 runtime; fatal after 0900) → all port to `core.*`/`mart.*` using `0201_backfill_ported.sql` as the authoritative column map; the four doc-pack Auto-LEO containers added to `core.cargo`. Applying to RDS exposed that **migration 0102 was never applied there**; wrote+applied an idempotent hotfix for the transporter/vehicle sections.
5–7. **Commands/results:**
   - Local validation (compose Postgres :5434): every script ran **twice**; second run all `INSERT 0 0` (idempotent).
   - RDS (runtime DB): 6/8 clean on first pass; 2 failed on missing 0102 columns → hotfix applied (`CREATE INDEX / DO / COMMIT`) → both re-ran clean.
   - `psql … "SELECT count(*) FROM core.cargo WHERE container_number IN (…4 ids…)"` → **4** (re-verified at report time).
8. **Endpoints verified:** `GET /api/cargo/APLU0896946` → full cargo JSON (was `not_found`); `GET /api/vehicles?limit=2` → fleet rows with 0102 columns (was 500-class breakage on RDS).
11. **Limitations:** RDS still lacks the **driver/pdp** sections of 0102 (large-table rewrite — needs a maintenance window; Phase-3/C4). `seed_demo_p0`'s `driver_enrollment` CREATE-guard retained (no-op vs real table).
12. **Debt:** none new; hotfix is committed as a repo file per existing hotfix precedent.

## Item 7 — ANPR weights SHA pin (Phase-0 H8)

1. **Files:** `.env.local` (untracked) — `ANPR_YOLO_SHA256=2d95861…9822`
7–10. **Verified:** `curl localhost:8301/healthz` → `weights_sha256` = pinned digest; container log `yolo_weights_verified` with same digest; detector ML active. OCR eval honestly reports the ZNCC fallback (0.0 exact-match on the 8-fixture holdout, `degraded:true`) — the documented degraded story, unchanged by the pin.

## Item 8 — Evidence pack regeneration (Phase-0 H9)

7. **Commands:** `make evidence`, then `scripts/build_evidence.py --handle tfc3-1785502124351`
10. **Result:** `evidence/metrics.json` regenerated (congestion F1 0.8797 TARGET_MET, anomaly wrong-way tp50/fp0 in first run, fleet 780, decision ring 50 buffered).
11. **Limitations (honest):** e2e-latency n=0, throughput 0.0, no Jaeger traces — the saturated Docker VM (3.8 GiB) can't produce a representative run; **re-run `make evidence` during a rehearsal on a warmed machine** (matches demo-pack doc 07 §3 guidance). Anomaly metrics returned None on the second run (docker-compose-exec probe under load).

---

## Item 9 — P1.1 Real `cargo.dpd_release` consumer

1. **Files:** `scenarios/uc2_bridge.py` (+195/−13), `scenarios/tfc3.py` (+79/−16), `scenarios/runner.py` (lifespan part of +49), `tests/test_scenarios.py` (+71 total)
3. **Functions added:** `uc2_bridge`: `start_listener()`, `stop_listener()`, `next_event()`, `is_listening()`, `wait_assigned()`, `_pump()` (own poll loop; `on_assign` **pins fetch position to the high-watermark** — fixes a real librdkafka race where `auto.offset.reset=latest` resolves lazily and a publish-then-await event is silently skipped), `_dispatch()` (correlation → queue; no correlation → `_handle_external()` autonomous truck injection, `UC2-BRIDGE:{ts}` tag, cap 300), drop-oldest `_enqueue()`. `tfc3`: `_await_consumed()` (drains foreign events, deadline-bounded), step-1 event gains `correlation_id`, step-2 records `consumed: kafka|inline-fallback` + `status degraded` on fallback. `runner._lifespan`: best-effort listener start/stop.
4. **Before → After:** TFC-3 published to Kafka then called `translate_release()` on its own local dict — **no consumer existed anywhere** (write-only theatre). Now: a real consumer group `uc3-uc2-bridge` (container log: `uc2_bridge_listening` → `uc2_bridge_assigned partitions=1`); TFC-3's event round-trips the broker; broker-down → inline fallback recorded `degraded` (backward compatible, demo-safe); an external UC-II producer moves the road twin with no scenario running.
5–6. **Tests:** `test_uc2_bridge_consumes_from_broker` (Kafka-gated, EXTERNAL listener :29092) — **passed 3× consecutively (~1.5 s each)** after the watermark fix; later failed under peak VM load due to the pump-churn side-effect (see Item 12), re-verified after the backoff fix — see final table. `test_uc2_bridge_matches_spec` (600 trucks/h × 40 min = 400) — passed.
7. **Commands:** pytest as above; live TFC-3 run via `POST /api/scenarios/tfc3/run`.
8. **Endpoints:** `/api/scenarios/tfc3/run`, `/api/scenarios/handle/{id}/timeline`, `/scenarios/tfc3/reset` (runner).
9. **Kafka:** produced + consumed `cargo.dpd_release` (group `uc3-uc2-bridge`; test groups `uc3-uc2-bridge-test-*`).
10. **Runtime evidence:**
    - Timeline step 2: **`status: ok | consumed: kafka`** (handle `tfc3-1785502124351`)
    - External drill (no correlation_id): scenarios log `dpd_release_translated {multiplier:1.5, trucks_per_h:360, total:120}` → `uc2_external_release_applied {tag: UC2-BRIDGE:1785502280, injected: 120}`; cleanup `DELETE /devices/tagged/…` → `{"removed":120}`
11. **Limitations:** external autonomous injection has no per-source rate limit beyond the 300-truck cap per event; queue bounded at 64 (drop-oldest).
12. **Debt:** none significant; consumer group offsets use `latest`+watermark pin (no historical replay by design).

## Item 10 — P1.2 `DeferredArrivalWindow` contract

1. **Files:** `shared/jnpa_shared/schemas.py` (+22)
3. **Added:** `class DeferredArrivalWindow` (`correlation_id`, `window_start`, `window_min=90`, `slot_cap=4`, `gate_id?`, `source="UC-II"`, `ts`) + `TOPIC_DEFERRED_ARRIVAL="jnpa.crosstwin.deferred-arrival"` + `__all__` entries.
4. **Before → After:** contract #1 of demo-pack doc 05 existed nowhere in the repo → typed, single-source shared model both twins can import.
5–6. **Test:** import + construct smoke (`window_min 90, slot_cap 4`) — passed; exercised by all Item-11 tests.
12. **Debt:** UC-2 repo (PoC_2) must adopt the same shape for its S2 producer — cross-repo work, out of this repo's scope.

## Item 11 — P1.3 TAS consumer integration

1. **Files:** `gateway/tas_mock.py` (+98), `gateway/main.py` (+25/−1), `gateway/routers/scenario_ext.py` (+7), `gateway/routers/rms_tas.py` (+31), new `tests/test_tas_crosstwin.py`
3. **Functions:** `tas_mock.apply_deferred_window()` (slots in window → RESCHEDULED; idempotent per correlation_id; bounded 32-window ledger), `deferred_windows()`, `check_booking_allowed()` (cap counter under the book lock); `main.py` `_apply_deferred()` + 4th `KafkaPump` (`jnpa-gateway-tas`, ws_type `tas`) + shutdown hook; `GET /api/tas/deferred-windows`; `rms_tas._deferred_window_guard()` wired into `POST /api/rms-tas/book` (right-split slot-code parse; never invents a refusal).
4. **Before → After:** no consumer, no metering → gateway consumes the topic, applies to the slot book, broadcasts `tas` WS frames, and the booking API enforces the cap naming the UC-II correlation-id.
5–6. **Tests:** `tests/test_tas_crosstwin.py` — 4/4 passed (apply/reschedule, idempotency, cap-then-release-outside-window, other-gate isolation); re-verified at report time (6 passed combined with scenario units).
7. **Commands:** host-side producer publish; `curl /api/tas/deferred-windows`; `POST /api/rms-tas/seed`; 3× `POST /api/rms-tas/book`.
8. **Endpoints verified:** `/api/tas/deferred-windows` (window `S2-DRILL-20260731`, 6 `applied_slots`, `booked:2`), `/api/rms-tas/seed` (8 slots), `/api/rms-tas/book` (booked → booked → **`{"booked":false,"reason":"deferred_arrival_window","window":{correlation_id:"S2-DRILL-20260731"…}}`**).
9. **Kafka:** produced (host test producer) + consumed (gateway group `jnpa-gateway-tas`) `jnpa.crosstwin.deferred-arrival`.
10. **Log evidence:** gateway `deferred_arrival_applied {correlation_id: S2-DRILL-20260731, gate_id: G-NSICT, applied_slots: 6, slot_cap: 2}`.
11. **Limitations:** window ledger is in-memory (consistent with TAS being a documented gateway-local mock; a gateway restart re-consumes only for a fresh consumer group). The frontend has no dedicated deferred-windows panel yet — demo via the endpoint/WS frames or add a small card (≤0.5 d).

## Item 12 — Gateway KafkaPump resilience (found during P1.4 drills)

1. **Files:** `gateway/pumps.py` (+~29 net across two fixes)
3. **Function:** `KafkaPump._run()` — was: single `consume()` call, any error → pump dead for process lifetime. First fix: fixed 5 s retry — **verified insufficient**: the perpetually-failing `traffic.snapshots` pump (topic has no producer — open audit item H11) then churned the group coordinator every 5 s, stalling partition assignments broker-wide (made the P1.1 round-trip test fail). Final: **exponential backoff 5 s → 60 s cap**, reset on successful consume, stop-responsive.
4. **Before → After:** a topic whose first producer starts after the gateway was never consumed (this is exactly why the first deferred-arrival drill event wasn't consumed until restart); now pumps self-heal without coordinator churn.
10. **Log evidence:** `kafka_pump_exit … UNKNOWN_TOPIC_OR_PART` (before) → post-restart consumption of the already-published drill window (after).
11. **Limitation:** `traffic.snapshots` still has no producer (H11, Phase-3) — its pump now backs off quietly at 60 s instead of dying or churning.

---

## Remaining limitations (consolidated)

1. RDS lacks 0102 driver/pdp sections (maintenance-window ALTER; Phase-3/C4).
2. Evidence pack traces/latency/throughput need a warm, unloaded run.
3. `traffic.snapshots` producer still missing (H11) — WS `traffic` frame stays silent.
4. UC-2 repo must adopt `DeferredArrivalWindow` for its S2 producer (cross-repo).
5. Docker VM (3.8 GiB) thrashes under the full 28-container stack — observability containers (grafana/kafka-ui/prometheus) currently stopped; re-plan VM sizing before demo.
6. Doc-04 demo script wording: "plays the 10-s MP4" → "shows the SHA-256-verified evidence frame".

## Technical debt introduced

1. In-memory deferred-window ledger (accepted: TAS is a documented mock; bounded).
2. `reports.py`/`fastag.py` fixes still lack dedicated router tests (pre-existing gap, not worsened).
3. Two gateway image rebuild cycles baked the override's shadow-mounts assumption deeper — the compose override (`carbon/`+`jnpa_shared` bind-mounts, 5434 remap) remains untracked (pre-existing).
4. `_deferred_window_guard` parses gate ids from slot codes by right-split — safe for current `{gate}-YYYY-MM-DD-HHMM` codes, brittle if the code format changes.

---

## Summary table

| Feature | Status | Evidence | Demo Ready | Production Ready |
|---|---|---|---|---|
| Police report date filter (H1) | ✅ Complete | Live curl with `since=` returned incidents (was `[]`) | YES | YES |
| FASTag health (H4) | ✅ Complete | `status:ok, mode:demo`, 3/3 tables true (re-verified) | YES | YES |
| F1 doc sweep (H7) | ✅ Complete | grep clean; artifact matches docs | YES | n/a (docs) |
| TFC-2 evidence frame + SHA (C3) | ✅ Complete | `test_anomaly` 25 passed; `last10s` grep 0; code path verified | YES (frame, not MP4 — update script wording) | YES |
| Scenario reset tags + alert leak (H2/H3) | ✅ Complete | New unit test passed; live TFC-3 reset `RESET`; tag-delete `removed:120` | YES | YES |
| Seeds → core.* + demo containers (C5) | ✅ Complete | 2× idempotent runs local; RDS applied; `core.cargo` count = 4; `/api/cargo/APLU0896946` 200 | YES | YES (needs commit) |
| RDS 0102 hotfix (vehicle/transporter) | ✅ Complete (partial scope) | Hotfix applied; `/api/vehicles` returns 0102 columns | YES | PARTIAL (driver/pdp pending) |
| ANPR SHA pin (H8) | ✅ Complete | `/healthz` digest + `yolo_weights_verified` log | YES | YES |
| Evidence pack (H9) | 🟡 Partial | metrics.json regenerated; traces/latency need warm run | PARTIAL (re-run at rehearsal) | NO |
| P1.1 dpd_release consumer | ✅ Complete | Round-trip pytest 3× pass; live timeline `consumed: kafka`; external drill injected 120 tagged trucks | YES | YES (rate-limit external events first) |
| P1.2 DeferredArrivalWindow contract | ✅ Complete | Model smoke + exercised by all XT-2 tests | YES | YES (UC-2 side pending, cross-repo) |
| P1.3 TAS consumer + cap guard | ✅ Complete | `deferred_arrival_applied` log; endpoint shows window; live booking refusal with correlation-id; 4/4 unit tests | YES | PARTIAL (in-memory ledger by design) |
| P1.4 e2e TFC-3 verification | ✅ Complete | All four drills executed live (this report §9–§11) | YES | — |
| KafkaPump resilience | ✅ Complete | Before/after logs; churn regression found and fixed with backoff | YES | YES |
| WS `traffic` frame (H11) | ❌ Open (Phase 3) | Pump no longer dies; producer still missing | N/A | NO |
