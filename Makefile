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

.PHONY: help venv up down logs ps psql redis-cli test bootstrap-check install-shared vahan-seed vahan-verify rfid-verify truck-verify

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

install-shared: ## pip install -e the shared + vahan + rfid + trucking packages into the active interpreter
	$(PY) -m pip install -e "shared[dev]"
	$(PY) -m pip install -e "ingest/vahan_sim[dev]" -e "ingest/vahan_live[dev]"
	$(PY) -m pip install -e "ingest/rfid[dev]"
	$(PY) -m pip install -e "ingest/trucking_app[dev]"

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
