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

.PHONY: help venv up down logs ps psql redis-cli test bootstrap-check install-shared

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

venv: ## Create .venv and install the shared package + dev deps (host-side)
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e "shared[dev]"

install-shared: ## pip install -e the shared package into the active interpreter
	$(PY) -m pip install -e "shared[dev]"

test: ## Run pytest -x in shared/ and tests/
	$(PY) -m pytest -x shared tests

bootstrap-check: ## Run the end-to-end bootstrap self-test
	$(PY) scripts/bootstrap_check.py
