# AdmitLens root Makefile: the only entry point for repo-wide setup, scans and hooks.
#
# Portable to GNU Make 3.81 (macOS) and 4.x (CI): no .ONESHELL, .SHELLFLAGS,
# .RECIPEPREFIX, file/let functions, != or ::= assignments, or grouped targets.
# Every recipe line starts with @. This file never includes .env and never
# expands a secret as a Make variable; values that come from git hooks (remote
# name, message path) are read from the environment as $$VAR inside the shell,
# so Make never expands them. The hooks run `make -f Makefile` with MAKEFILES and
# MAKEFLAGS cleared, so an inherited make environment cannot neuter them.
# Level Makefiles live in their own folders; this file never names a level.

SHELL := /bin/sh

# Simple (:=) assignments win over the environment, so an inherited GUARDS_DIR or
# GITLEAKS cannot redirect the hooks; only an explicit command-line override can.
GUARDS_DIR := tools/guards
GITLEAKS := .tools/bin/gitleaks
HOOKS_DIR := .githooks
HOOKS := $(HOOKS_DIR)/pre-commit $(HOOKS_DIR)/commit-msg $(HOOKS_DIR)/pre-push
LEVEL_MAKE = $(MAKE)

# The guards CLI, run from the repo root (it scans the repo it is run in).
GUARDS = PYTHONPATH="$(GUARDS_DIR)" GUARDS_GITLEAKS="$(GITLEAKS)" uv run --quiet --locked --project "$(GUARDS_DIR)" python -m guards

.DEFAULT_GOAL := help
.PHONY: help setup hooks doctor secrets-scan guards-test lint parity datagen
.PHONY: hook-pre-commit hook-commit-msg hook-pre-push

help: ## Show this help
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

setup: hooks ## Install pinned gitleaks, sync the guards env, set local git config (idempotent)
	@sh "$(GUARDS_DIR)/bin/install-gitleaks.sh" "$(GITLEAKS)"
	@uv sync --quiet --locked --project "$(GUARDS_DIR)"
	@git config --local user.useConfigOnly true
	@git config --local push.default simple
	@git config --local push.followTags false
	@git config --local status.showUntrackedFiles all
	@git config --local core.autocrlf false
	@n=$$(git config --get user.email | grep -c '@users\.noreply\.github\.com$$' || true); \
	if [ "$$n" -eq 0 ]; then \
		echo "warn  user.email is not a GitHub noreply address: run git config --local user.email <id>+<user>@users.noreply.github.com"; \
	fi
	@echo "setup: done (run make doctor to verify)"

hooks: ## Point git at .githooks and make the hooks executable
	@git config --local core.hooksPath $(HOOKS_DIR)
	@chmod +x $(HOOKS)

doctor: ## Check hooks, local git config, gitleaks version and identity (prints ok/warn/FAIL, never values)
	@GUARDS_MAKE_VERSION="$(MAKE_VERSION)" $(GUARDS) doctor

secrets-scan: ## Full scan: work tree plus every commit on every ref (merges, messages, tags, identities)
	@tree_rc=0; hist_rc=0; \
	$(GUARDS) tree || tree_rc=$$?; \
	$(GUARDS) history || hist_rc=$$?; \
	if [ "$$tree_rc" -ge 2 ] || [ "$$hist_rc" -ge 2 ]; then exit 2; fi; \
	if [ "$$tree_rc" -ne 0 ] || [ "$$hist_rc" -ne 0 ]; then exit 1; fi

guards-test: ## Run the guards test suite
	@cd "$(GUARDS_DIR)" && uv run --quiet --locked pytest

lint: ## Lint Makefiles, .gitignore files and workflows with the guards
	@$(GUARDS) lint

parity: ## (reserved, L2-P1) make parity FROM=<path> TO=<path> MODE=tree|exact|ignore-imports|symbols
	@echo "parity: implemented in L2-P1 (ADR-0007 E7.7)" >&2; exit 2

datagen: ## (reserved, L1-P5) build the seed data
	@echo "datagen: implemented in L1-P5" >&2; exit 2

# --- Internal targets, called by the git hooks in .githooks/ -----------------

hook-pre-commit:
	@$(GUARDS) staged

hook-commit-msg:
	@if [ -z "$$MSG_FILE" ]; then echo "hook-commit-msg: MSG_FILE is required" >&2; exit 2; fi; \
	$(GUARDS) message "$$MSG_FILE"

hook-pre-push:
	@if [ -z "$$REMOTE" ]; then echo "hook-pre-push: REMOTE is required" >&2; exit 2; fi; \
	umask 077; refs=$$(mktemp) || exit 2; trap 'rm -f "$$refs"' EXIT INT TERM; \
	cat >"$$refs" || exit 2; \
	$(GUARDS) prepush "$$REMOTE" <"$$refs" || exit $$?; \
	LEVEL_MAKE="$(LEVEL_MAKE)" sh "$(GUARDS_DIR)/bin/level-checks.sh" "$$REMOTE" "$$refs"
