# The build entrypoint. CI runs these targets and nothing else.
#
# The point is not convenience. It is that `.github/workflows/ci.yml` contains no
# commands of its own, so there is no second place for the real build to live and
# no way for the two to drift. If you can run `make ci` you can reproduce a
# failing pipeline exactly, without reading YAML.
#
# Everything past `lint` needs a PostgreSQL you may create databases on:
#
#     export TEST_DATABASE_URL="postgresql+psycopg://postgres@localhost/postgres"
#
# The role needs CREATE DATABASE and superuser — see scripts/README.md §5 for
# why (`session_replication_role`, which the per-test reset depends on).

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
PARALLEL ?= 4
SOURCES = app tests scripts migrations

.DEFAULT_GOAL := help
.PHONY: help install lint format contracts types spec test test-unit test-fast \
        migrations invariants smoke coverage ci ci-checks ci-tests clean

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package and dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"

# ---------------------------------------------------------------- static checks

lint:  ## Ruff
	ruff check $(SOURCES)

format:  ## Ruff, fixing what it can
	ruff check --fix $(SOURCES)

contracts:  ## import-linter: the module boundaries that make this a modular monolith
	lint-imports

types:  ## mypy, on the layer that is clean today (see docs/design/0011-ci.md §5)
	mypy app/platform --ignore-missing-imports

spec:  ## The OpenAPI document, the routes served, and the fields implemented
	$(PYTHON) scripts/validate_openapi.py
	$(PYTHON) scripts/check_api_coverage.py
	$(PYTHON) scripts/check_schema_conformance.py

# ---------------------------------------------------------------------- testing

test-unit:  ## The pure domain suites — no database, no ffmpeg
	$(PYTEST) tests -q --ignore=tests/integration

test:  ## Everything, serially (~1m20s)
	$(PYTEST) tests -q

test-fast:  ## Everything, in parallel (~32s)
	$(PYTEST) tests -q -n $(PARALLEL)

# ------------------------------------------------------------------- the schema

migrations:  ## upgrade head -> downgrade base -> upgrade head, on a scratch database
	$(PYTHON) scripts/check_migrations.py

invariants:  ## The database-enforced invariants, and the zero-DDL acceptance test
	$(PYTHON) scripts/check_invariants.py

smoke:  ## Boot a real dramatiq worker against a real Redis and run one job
	$(PYTHON) scripts/smoke_workers.py

coverage:  ## Measure coverage and enforce the per-path floors
	$(PYTEST) tests -q -n $(PARALLEL) --cov=app --cov-report=term:skip-covered \
		--cov-report=json:coverage.json
	$(PYTHON) scripts/check_coverage.py

# ------------------------------------------------------------------------- gates

ci-checks: lint contracts types spec test-unit  ## Everything that needs no services

ci-tests: coverage migrations invariants smoke  ## Everything needing PostgreSQL, ffmpeg, Redis, MinIO

ci: ci-checks ci-tests  ## The whole pipeline, exactly as CI runs it

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage coverage.json var/media
