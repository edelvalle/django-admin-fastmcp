.PHONY: help bootstrap install sync lock upgrade update test coverage lint ruff typecheck \
        format check migrate makemigrations superuser serve shell clean build publish publish-test check-dist

PATH := $(HOME)/.local/bin:$(PATH)
SHELL := /bin/bash
PROJECT_NAME := django_admin_fastmcp
PYTHON_VERSION ?= 3.14
MANAGE := tests/project/manage.py

# Load .envrc when inside Emacs, Claude Code, or pi
_LOAD_ENVRC :=
ifdef INSIDE_EMACS
    _LOAD_ENVRC := 1
endif
ifdef CLAUDECODE
    _LOAD_ENVRC := 1
endif
ifeq ($(LLM_AGENT_PUPPETEER),pi)
    _LOAD_ENVRC := 1
endif
ifdef _LOAD_ENVRC
    ifneq (,$(wildcard .envrc))
        export BASH_ENV := $(CURDIR)/.envrc
        _ := $(shell bash -c 'set -a; source $(CURDIR)/.envrc 2>/dev/null && env | grep -E "^[A-Za-z_][A-Za-z0-9_]*=" | grep -v "^SHELL=" | grep -v "^MAKEFLAGS=" | grep -v "^MFLAGS=" | grep -v "^MAKEFILE_LIST=" > $(CURDIR)/.envrc.make.tmp')
        -include .envrc.make.tmp
    endif
endif

UV ?= uv
RUN ?= $(UV) run

help: ## Show this help message
	@echo "django-admin-fastmcp development commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Pin the Python version for uv
	@$(UV) python pin $(PYTHON_VERSION)

install: bootstrap ## Install all dependencies (frozen when the lock allows it)
	@$(UV) sync --frozen || $(UV) sync

sync: bootstrap ## Install from the lock file, fail when it is stale
	@$(UV) sync --frozen

lock: bootstrap ## Refresh the lock file
	@$(UV) sync

upgrade update: bootstrap ## Upgrade every dependency and refresh the lock file
	@$(UV) sync -U

test: ## Run the test suite (the permission matrix)
	@$(RUN) pytest

coverage: ## Run the tests with coverage
	@$(RUN) pytest --cov --cov-report=term

lint: ## Check code style without changing files
	@$(RUN) ruff check $(PROJECT_NAME)/ tests/
	@$(RUN) ruff format --check $(PROJECT_NAME)/ tests/

ruff: ## Run the ruff linter only
	@$(RUN) ruff check $(PROJECT_NAME)/ tests/

typecheck: ## Run the zuban type checker
	@$(RUN) zuban check

format: ## Auto-format the code (includes import sorting)
	@$(RUN) ruff check --fix $(PROJECT_NAME)/ tests/
	@$(RUN) ruff format $(PROJECT_NAME)/ tests/

check: format lint typecheck test ## Format, lint, typecheck, and test

migrate: ## Apply migrations in the test project
	@$(RUN) python $(MANAGE) migrate

makemigrations: ## Create migrations from model changes
	@$(RUN) python $(MANAGE) makemigrations

superuser: ## Create a superuser in the test project
	@$(RUN) python $(MANAGE) createsuperuser

serve: ## Run the MCP server against the test project
	@$(RUN) python $(MANAGE) admin_mcp_serve --port 8765

shell: ## Open a Django shell in the test project
	@$(RUN) python $(MANAGE) shell

clean: ## Remove build artifacts and caches
	@rm -rf build/ dist/ *.egg-info/ .ruff_cache/ .pytest_cache/ .coverage htmlcov/
	@find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -path ./.venv -prune -o -name "*.pyc" -delete 2>/dev/null || true

build: clean ## Build the sdist and wheel
	@$(UV) build

publish-test: build ## Publish to TestPyPI
	@$(UV) publish --index-url https://test.pypi.org/simple/

publish: build ## Publish to PyPI (requires credentials in .envrc)
	@$(UV) publish

check-dist: build ## Build and list the distribution files
	@ls -la dist/
