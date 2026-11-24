.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash

CLUSTER ?= sre-copilot
SCENARIO ?= crashloop
MODE ?= replay

.PHONY: help
help: ## Show targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

.PHONY: install
install: ## Install dependencies with uv
	uv sync

.PHONY: lint
lint: ## ruff lint + format check
	uv run ruff check src tests
	uv run ruff format --check src tests

.PHONY: typecheck
typecheck: ## mypy --strict
	uv run mypy

.PHONY: test
test: ## Offline test suite (no API key, no cluster)
	uv run pytest

.PHONY: check
check: lint typecheck test ## Everything CI runs offline

.PHONY: replay
replay: ## Replay a recorded incident offline (SCENARIO=crashloop|oom)
	SRE_COPILOT_STATE_DIR=$$(mktemp -d) uv run sre-copilot investigate \
		--replay scenarios/checkout-$(SCENARIO)/script.yaml \
		--fixtures scenarios/checkout-$(SCENARIO)/fixtures.json \
		--alert-file scenarios/checkout-$(SCENARIO)/alert.json

.PHONY: demo-up
demo-up: ## Create the kind cluster with Prometheus, Loki and the sample app
	CLUSTER=$(CLUSTER) ./scripts/demo-up.sh

.PHONY: demo-run
demo-run: ## Inject a fault and run alert -> investigate -> approve -> verify (MODE=replay|live)
	MODE=$(MODE) ./scripts/demo-run.sh $(SCENARIO)

.PHONY: demo-reset
demo-reset: ## Restore the healthy checkout deployment
	./scripts/fault.sh reset

.PHONY: demo
demo: demo-up demo-run ## demo-up + demo-run

.PHONY: demo-down
demo-down: ## Delete the kind cluster and local demo state
	kind delete cluster --name $(CLUSTER)
	rm -rf .demo
