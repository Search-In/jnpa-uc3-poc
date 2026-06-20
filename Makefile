# ============================================================================
# JNPA UC-III PoC — developer Makefile
# ----------------------------------------------------------------------------
# One-command bring-up:
#   cp .env.local.example .env.local && make up && make bootstrap-check
# ============================================================================

SHELL := /bin/bash

# Prefer the project virtualenv's interpreter if it exists (it holds the pinned
# jnpa-shared deps); otherwise fall back to the system python3.
ifneq (,$(wildcard .venv/bin/python))
PY := .venv/bin/python
else
PY := python3
endif

# Tell docker compose to use .env.local for ${...} interpolation when present
# (compose reads .env by default, not .env.local).
ENV_FILE := $(wildcard .env.local)
ifeq ($(ENV_FILE),.env.local)
COMPOSE := docker compose --env-file .env.local
else
COMPOSE := docker compose
endif

# Also load .env.local into make's own environment for host-side targets
# (bootstrap-check / test read POSTGRES_PASSWORD etc.).
ifneq (,$(wildcard .env.local))
include .env.local
export
endif

.DEFAULT_GOAL := help

.PHONY: help venv up down logs ps psql redis-cli test bootstrap-check install-shared vahan-seed vahan-verify rfid-verify truck-verify anpr-verify anpr-bench anpr-eval-real anpr-eval-selftest congestion-train congestion-verify anomaly-train anomaly-verify gateway-verify dev-web web-build web-build-mock web-verify-live web-verify web-e2e scenarios-verify tfc1 tfc2 tfc3 vapid-keys dev-pwa pwa-build pwa-verify pwa-e2e preflight e2e demo demo-record evidence demo-reset

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## docker compose up -d (build + start all services)
	$(COMPOSE) up -d

down: ## docker compose down -v (stop + remove volumes)
	$(COMPOSE) down -v

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

psql: ## Open psql inside the postgres container
	$(COMPOSE) exec postgres psql -U postgres -d postgres

redis-cli: ## Open redis-cli inside the redis container
	$(COMPOSE) exec redis redis-cli

venv: ## Create .venv and install shared + vahan + rfid services (host-side, for tests)
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e "shared[dev]"
	.venv/bin/python -m pip install -e "ingest/vahan_sim[dev]" -e "ingest/vahan_live[dev]"
	.venv/bin/python -m pip install -e "ingest/rfid[dev]"
	.venv/bin/python -m pip install -e "ingest/trucking_app[dev]"
	.venv/bin/python -m pip install -e "ai/anpr[dev]"
	.venv/bin/python -m pip install -e "ai/congestion[dev]"
	.venv/bin/python -m pip install -e "ai/anomaly[dev]"
	.venv/bin/python -m pip install -e "gateway[dev]"

install-shared: ## pip install -e the shared + vahan + rfid + trucking packages into the active interpreter
	$(PY) -m pip install -e "shared[dev]"
	$(PY) -m pip install -e "ingest/vahan_sim[dev]" -e "ingest/vahan_live[dev]"
	$(PY) -m pip install -e "ingest/rfid[dev]"
	$(PY) -m pip install -e "ingest/trucking_app[dev]"
	$(PY) -m pip install -e "ai/anpr[dev]"
	$(PY) -m pip install -e "ai/congestion[dev]"
	$(PY) -m pip install -e "ai/anomaly[dev]"
	$(PY) -m pip install -e "gateway[dev]"
	$(PY) -m pip install -e "scenarios[dev]"

test: ## Run pytest -x in shared/ and tests/
	$(PY) -m pytest -x shared tests

bootstrap-check: ## Run the end-to-end bootstrap self-test
	$(PY) scripts/bootstrap_check.py

vahan-seed: ## Regenerate data/fixtures/known_plates.json (25k plates, 50-plate fixture)
	PYTHONPATH=ingest:shared $(PY) -m vahan_sim.seed --out data/fixtures/known_plates.json

vahan-verify: ## Smoke-test the Vahan simulator + live adapter (stack must be up)
	@echo "== sim RC ==" && curl -s http://localhost:8201/vahan/rc/MH04AB1234 | $(PY) -m json.tool || true
	@echo "== sim health ==" && curl -s http://localhost:8201/healthz | $(PY) -m json.tool || true
	@echo "== live (expect 503 without token) ==" \
		&& curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:8202/vahan/rc/MH04AB1234 || true
	@echo "== vehicle_master count ==" \
		&& $(COMPOSE) exec -T postgres psql -U postgres -d postgres \
		-c "select count(*) from jnpa.vehicle_master;" || true

rfid-verify: ## Verify RFID reads landed + a vehicle.confirmed fired (stack must be up)
	@echo "== busiest readers (rfid_reads) ==" \
		&& $(COMPOSE) exec -T postgres psql -U postgres -d postgres \
		-c "select reader_id, count(*) from jnpa.rfid_reads group by 1 order by 2 desc limit 5;" || true
	@echo "== waiting (<=30s) for a vehicle.confirmed in the correlator log ==" \
		&& ($(COMPOSE) logs --since 2m rfid-correlator 2>/dev/null | grep -m1 vehicle.confirmed \
			|| echo "  none yet — inject a matching ANPR read or wait for one") || true

truck-verify: ## Verify the trucking-app sim: population + a few live MQTT pings (stack must be up)
	@echo "== population ==" && curl -s http://localhost:8240/devices | $(PY) -m json.tool || true
	@echo "== 5 live telemetry pings (trucks/+/telemetry) ==" \
		&& (timeout 15 $(COMPOSE) exec -T mosquitto mosquitto_sub -t 'trucks/+/telemetry' -C 5 \
			|| echo "  none yet — give the sim a few seconds to warm up") || true
	@echo "== rows in jnpa.truck_telemetry ==" \
		&& $(COMPOSE) exec -T postgres psql -U postgres -d postgres \
		-c "select count(*) from jnpa.truck_telemetry;" || true

anpr-verify: ## Smoke-test the ANPR+OCR service: /infer on the sample + /eval (stack must be up)
	@echo "== /infer (sample plate) ==" \
		&& curl -s -F "image=@./ai/anpr/resources/sample_plate.jpg" \
		http://localhost:8301/infer | $(PY) -m json.tool || true
	@echo "== /eval (OCR_TARGET_MET + per-slice metrics) ==" \
		&& curl -s http://localhost:8301/eval \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d,indent=2)); print('OCR_TARGET_MET=%s' % str(d['OCR_TARGET_MET']).lower())" || true

anpr-bench: ## Run the ANPR/OCR benchmark in-process (no stack needed) -> metrics.json
	PYTHONPATH=ai/anpr/src:shared $(PY) ai/anpr/eval/bench.py

anpr-eval-real: ## EVIDENCE-GRADE OCR eval on REAL plates. IMAGES=dir LABELS=csv [OUT=dir]
	@test -n "$(IMAGES)" -a -n "$(LABELS)" || { echo "usage: make anpr-eval-real IMAGES=data/anpr_real/images LABELS=data/anpr_real/labels.csv"; exit 2; }
	$(PY) ai/anpr/eval/evaluate_real.py --images "$(IMAGES)" --labels "$(LABELS)" --out "$(or $(OUT),ai/anpr/eval/real)"

anpr-eval-selftest: ## Harness self-test on synthetic plates (NON-EVIDENTIAL; proves the runner works)
	$(PY) ai/anpr/eval/evaluate_real.py --synthetic 200 --out ai/anpr/eval/baseline_synthetic

congestion-train: ## Train the congestion forecaster in-process (no stack needed) -> artifacts + metrics.json
	PYTHONPATH=ai:shared $(PY) -m congestion.train

congestion-verify: ## Smoke-test the congestion service: /predict length + F1 from /metrics (stack must be up)
	@echo "== /predict (per-segment probabilities) ==" \
		&& curl -s -XPOST http://localhost:8311/predict -d '{"horizon_min":15}' \
		-H 'content-type: application/json' | $(PY) -m json.tool || true
	@echo "== segment count ==" \
		&& curl -s -XPOST http://localhost:8311/predict -d '{"horizon_min":15}' \
		-H 'content-type: application/json' | $(PY) -c "import sys,json; print('segments=%d' % len(json.load(sys.stdin)))" || true
	@echo "== /metrics (congestion_onset_f1 + target) ==" \
		&& curl -s http://localhost:8311/metrics \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d,indent=2)); print('congestion_onset_f1=%s' % d.get('congestion_onset_f1')); print('TARGET_MET=%s' % str(d.get('TARGET_MET')).lower())" || true

anomaly-train: ## Train the trajectory autoencoder in-process (needs torch) -> artifacts + metrics.json
	PYTHONPATH=ai:shared $(PY) -m anomaly.train

anomaly-verify: ## Smoke-test the anomaly detector: /alerts/recent length + /health (stack must be up)
	@echo "== /health ==" \
		&& curl -s http://localhost:8321/health | $(PY) -m json.tool || true
	@echo "== /alerts/recent?since=PT1H (count) ==" \
		&& curl -s 'http://localhost:8321/alerts/recent?since=PT1H' \
		| $(PY) -c "import sys,json; print('alerts=%d' % len(json.load(sys.stdin)))" || true
	@echo "== alerts by kind (jnpa.alerts) ==" \
		&& $(COMPOSE) exec -T postgres psql -U postgres -d postgres \
		-c "select kind, severity, count(*) from jnpa.alerts group by 1,2 order by 3 desc;" || true

gateway-verify: ## Smoke-test the API gateway: orchestrated RC lookup + decision evidence (stack must be up)
	@echo "== /healthz ==" && curl -s http://localhost:8000/healthz | $(PY) -m json.tool || true
	@echo "== /api/vahan/rc/MH04AB1234 (orchestrated) ==" \
		&& curl -s http://localhost:8000/api/vahan/rc/MH04AB1234 | $(PY) -m json.tool || true
	@echo "== last decision (/api/debug/decisions [0]) ==" \
		&& curl -s http://localhost:8000/api/debug/decisions \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d[0],indent=2) if d else 'no decisions yet')" || true
	@echo "== System-Health sources (/api/kpi/sources) ==" \
		&& curl -s http://localhost:8000/api/kpi/sources | $(PY) -m json.tool || true
	@echo "== provisional vehicles still in cure window ==" \
		&& $(COMPOSE) exec -T postgres psql -U postgres -d postgres \
		-c "select plate, provisional_until from jnpa.vehicle_master where provisional = true and provisional_until > now();" || true

dev-web: ## Run the dashboard dev server on :5173 (Vite, proxies /api -> :8000)
	cd web && npm install && npm run dev

web-build: ## Build the production dashboard bundle (web/dist) — LIVE by default
	cd web && npm install && npm run build
	$(MAKE) web-verify-live

web-build-mock: ## Build a LOCAL mock dashboard bundle (never deploy this)
	cd web && npm install && npm run build:mock

web-verify-live: ## Assert web/dist is a live bundle (no mock shipped)
	bash scripts/verify_web_live_build.sh web/dist

web-verify: ## Smoke-test the dashboard's gateway surface (stack must be up)
	@echo "== /api/gates ==" && curl -s http://localhost:8000/api/gates | $(PY) -m json.tool || true
	@echo "== /api/corridor (segment_count) ==" \
		&& curl -s http://localhost:8000/api/corridor \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print('segments=%d length_km=%s' % (d['segment_count'], d['length_km']))" || true
	@echo "== /api/zones (count) ==" \
		&& curl -s http://localhost:8000/api/zones \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print('source=%s zones=%d' % (d['source'], len(d['zones'])))" || true
	@echo "== /api/reports/police?format=json (count) ==" \
		&& curl -s 'http://localhost:8000/api/reports/police?format=json' \
		| $(PY) -c "import sys,json; print('incidents=%d' % json.load(sys.stdin)['count'])" || true
	@echo "== dashboard root (nginx :3000) ==" \
		&& curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:3000/live || true

web-e2e: ## Run the Playwright e2e suite against the running dashboard
	cd web && npm install && npx playwright install --with-deps chromium && npm run test:e2e

tfc1: ## Run the TFC-1 gate-closure scenario (stack must be up)
	@curl -s -XPOST http://localhost:8400/scenarios/tfc1/run \
		-d '{"gate_id":"G-NSICT","duration_minutes":120}' -H 'content-type: application/json' \
		| $(PY) -m json.tool || true

tfc2: ## Run the TFC-2 wrong-way scenario (stack must be up)
	@curl -s -XPOST http://localhost:8400/scenarios/tfc2/run \
		-d '{"camera_id":"C-KARAL-EXIT"}' -H 'content-type: application/json' \
		| $(PY) -m json.tool || true

tfc3: ## Run the TFC-3 cargo-surge cross-twin scenario (stack must be up)
	@curl -s -XPOST http://localhost:8400/scenarios/tfc3/run \
		-d '{"dpd_release_spike":2.5}' -H 'content-type: application/json' \
		| $(PY) -m json.tool || true

scenarios-verify: ## Smoke-test the scenarios runner end-to-end (stack must be up)
	@echo "== /healthz ==" && curl -s http://localhost:8400/healthz | $(PY) -m json.tool || true
	@echo "== run tfc1 ==" \
		&& HID=$$(curl -s -XPOST http://localhost:8400/scenarios/tfc1/run \
			-d '{"gate_id":"G-NSICT","duration_minutes":120}' -H 'content-type: application/json' \
			| $(PY) -c "import sys,json; print(json.load(sys.stdin)['handle_id'])") \
		&& echo "handle=$$HID" \
		&& echo "== timeline ==" \
		&& curl -s http://localhost:8400/scenarios/$$HID/timeline \
			| $(PY) -c "import sys,json; d=json.load(sys.stdin); print('steps=%d status=%s' % (d['count'], d.get('status'))); [print(' ', s['step_no'], s['status'], s['title']) for s in d['steps']]" \
		&& echo "== reset ==" \
		&& curl -s -XPOST http://localhost:8400/scenarios/tfc1/reset -d "{\"handle_id\":\"$$HID\"}" \
			-H 'content-type: application/json' | $(PY) -m json.tool || true
	@echo "== Jaeger UI: http://localhost:16686  ·  What-If: http://localhost:3000/whatif =="

# ============================================================================
# Trucking-App PWA (Prompt 11) — driver-side ETA / re-route advisory
# ============================================================================
vapid-keys: ## Generate VAPID keypair for WebPush and append to .env.local
	@$(PY) scripts/gen_vapid_keys.py

dev-pwa: ## Run the PWA dev server on :3002 (Vite, proxies /api -> :8000)
	cd mobile-pwa && npm install && npm run dev

pwa-build: ## Build the production PWA bundle (mobile-pwa/dist)
	cd mobile-pwa && npm install && npm run build

pwa-verify: ## Smoke-test the PWA surface + push channel (stack must be up)
	@echo "== PWA served at /pwa (nginx :3000) ==" \
		&& curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:3000/pwa/ || true
	@echo "== /api/push/status ==" \
		&& curl -s http://localhost:8000/api/push/status | $(PY) -m json.tool || true
	@echo "== /api/push/vapid-public-key ==" \
		&& curl -s http://localhost:8000/api/push/vapid-public-key \
		| $(PY) -c "import sys,json; d=json.load(sys.stdin); print('configured=%s' % d['configured'])" || true
	@echo "== Open the PWA: http://localhost:3000/pwa  (web variant: ?device=DEV-000001) =="

pwa-e2e: ## Run the PWA Playwright e2e suite (stack must be up)
	cd mobile-pwa && npm install && npx playwright install --with-deps chromium && npm run test:e2e

# ============================================================================
# Integration, demo & evaluator evidence pack (Prompt 12)
# ============================================================================
preflight: ## Hard-coded demo sanity checks (refuse to launch if a prereq is missing)
	$(PY) -m scripts.preflight

e2e: ## End-to-end smoke test — exit 0 means every assertion passed (stack must be up)
	$(PY) tests/e2e/test_full_pipeline.py

demo: ## Walk the operator through the on-screen demo (interactive; stack must be up)
	$(PY) scripts/demo_drive.py

demo-record: ## Run the demo + capture screenshots + build the evidence pack (stack must be up)
	$(PY) scripts/demo_drive.py --record

evidence: ## (Re)build ./evidence (metrics.json + Jaeger traces + POC_SUMMARY.md)
	$(PY) scripts/build_evidence.py

demo-reset: ## Return the stack to a clean baseline (wipes ephemeral data, keeps trained models)
	$(PY) scripts/demo_reset.py
