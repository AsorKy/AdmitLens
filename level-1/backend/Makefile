# AdmitLens level-1 backend: every operation of this level is a target here (contract KM).
#
# Portable to GNU Make 3.81 (macOS) and 4.x (CI): no .ONESHELL, .SHELLFLAGS,
# .RECIPEPREFIX, file/let functions, != or ::= assignments, or grouped targets.
# Every recipe line starts with @. This file never includes .env and never expands
# a secret as a Make variable: recipes that need one read it from the shell
# environment ($$VAR) or inside the database container, so `make -n` shows names only.

SHELL := /bin/sh

UV_RUN := uv run --quiet --locked
COMPOSE := docker compose
SQL_FILE := ops/postgres/ensure-test-db.sql
SQL_IN_DB := /opt/admitlens/postgres/ensure-test-db.sql
# Typed by the user to confirm `make db-down` (it deletes the database volume).
CONFIRM_WORD := admitlens-l1

.DEFAULT_GOAL := help
.PHONY: help install lint typecheck test-unit test check openapi openapi-check
.PHONY: env db-up db-stop db-down db-check migrate seed dev

help: ## Show this help
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Create .venv with the runtime and dev dependencies from uv.lock
	@uv sync --locked

lint: ## ruff check and ruff format --check
	@$(UV_RUN) ruff check .
	@$(UV_RUN) ruff format --check .

typecheck: ## mypy (informational: not part of check or CI)
	@$(UV_RUN) mypy app tests

test-unit: ## Tests that need no database (no Docker)
	@$(UV_RUN) pytest -m "not docker"

test: db-check ## All tests, with coverage; needs the database (make db-up)
	@$(UV_RUN) pytest --cov=app

check: lint test-unit ## Quality gate run by pre-push: lint + test-unit, no Docker

openapi: ## (reserved, L1-P4) export openapi.json
	@echo "openapi: implemented in L1-P4" >&2; exit 2

openapi-check: ## (reserved, L1-P4) fail if openapi.json is stale
	@echo "openapi-check: implemented in L1-P4" >&2; exit 2

env: ## Create .env from .env.example with generated passwords (never overwrites)
	@sh ops/make-env.sh .env.example .env

# db-up checks the password over the service address `db` (scram), because loopback
# and the socket use trust: the volume keeps the password of its first init, so a
# newer .env must not look healthy. The password is read inside the container only.
db-up: ## Start PostgreSQL 16, wait until healthy, ensure the admitlens_test database
	@if [ ! -f "$(SQL_FILE)" ]; then echo "db-up: missing $(SQL_FILE)" >&2; exit 2; fi
	@if [ ! -f .env ]; then echo "db-up: no .env; run make env first" >&2; exit 2; fi
	@$(COMPOSE) up -d --wait --wait-timeout 90 db
	@$(COMPOSE) exec -T db sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -f $(SQL_IN_DB)' >/dev/null
	@if $(COMPOSE) exec -T db sh -c 'PGPASSWORD="$$POSTGRES_PASSWORD" psql -X -q -w -h db -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "select 1"' >/dev/null 2>&1; then :; else \
		echo "db-up: could not log in over the service address (usually the volume was initialised with another password); run make db-down" >&2; exit 1; fi
	@echo "db-up: database healthy on 127.0.0.1; admitlens_test ensured"

db-stop: ## Stop the database and keep its volume
	@$(COMPOSE) stop db

db-down: ## Remove the database AND its volume (asks you to type admitlens-l1)
	@if [ ! -t 0 ]; then echo "db-down: needs an interactive terminal to confirm; aborted" >&2; exit 2; fi; \
	printf 'This deletes the level-1 database volume. Type %s to confirm: ' "$(CONFIRM_WORD)"; \
	read -r answer || answer=; \
	if [ "$$answer" != "$(CONFIRM_WORD)" ]; then echo "db-down: not confirmed; nothing removed" >&2; exit 1; fi; \
	$(COMPOSE) down -v

# CI counts only when it is `true` or `1` (GitHub sets CI=true); CI=false is local.
ifneq ($(filter true 1,$(CI)),)
db-check:
	@echo "db-check: skipped in CI (service container)"
else
# db-check asks the Docker daemon: if the daemon does not answer, it reports the database as down.
db-check: ## Check the database is up via Docker (fails fast with a hint; skipped when CI=true)
	@if [ ! -f .env ]; then echo "db-check: no .env; run make env, then make db-up" >&2; exit 1; fi
	@if $(COMPOSE) exec -T db pg_isready -q -h 127.0.0.1 >/dev/null 2>&1; then :; else \
		echo "db-check: the level-1 database is not running; run make db-up" >&2; exit 1; fi
endif

migrate: ## (reserved, L1-P2) alembic upgrade head
	@echo "migrate: implemented in L1-P2" >&2; exit 2

seed: ## (reserved, L1-P4) load data/seeds/*.csv
	@echo "seed: implemented in L1-P4" >&2; exit 2

dev: ## (reserved, L1-P4) run the API on 127.0.0.1 with reload
	@echo "dev: implemented in L1-P4" >&2; exit 2
