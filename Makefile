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

console:  ## Every admin endpoint has a screen, and every exemption is current
	$(PYTHON) scripts/check_console_coverage.py

build-def:  ## FAIL when the Dockerfile, .dockerignore and compose files disagree
	$(PYTHON) scripts/check_build_definition.py

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

write-paths:  ## FAIL when a query filters a column no code path writes
	$(PYTHON) scripts/check_write_paths.py

smoke:  ## Boot a real dramatiq worker against a real Redis and run one job
	$(PYTHON) scripts/smoke_workers.py

coverage:  ## Measure coverage and enforce the per-path floors
	$(PYTEST) tests -q -n $(PARALLEL) --cov=app --cov-report=term:skip-covered \
		--cov-report=json:coverage.json
	$(PYTHON) scripts/check_coverage.py

# ------------------------------------------------------------------------- web

web-install:  ## npm ci in web/ (uses the lockfile; `npm install` is for adding deps)
	cd web && npm ci

web-codegen:  ## Regenerate the typed client from openapi/openapi.yaml
	cd web && npm run codegen

web-codegen-check:  ## FAIL when the committed client has drifted from the contract
	@# The reason the frontend lives in this repository. `check_api_coverage.py`
	@# proves the contract matches the running application; this proves the
	@# generated client matches the contract. Together they make a schema change
	@# a compile error in the same CI run, instead of an undefined at runtime in
	@# front of a teacher.
	@cd web && cp src/api/schema.d.ts /tmp/schema.before.d.ts && npm run --silent codegen && 		if ! diff -q /tmp/schema.before.d.ts src/api/schema.d.ts >/dev/null; then 			echo "FAIL  web/src/api/schema.d.ts is stale — run \`make web-codegen\` and commit it"; 			diff -u /tmp/schema.before.d.ts src/api/schema.d.ts | head -40; 			cp /tmp/schema.before.d.ts src/api/schema.d.ts; exit 1; 		fi; echo "PASS  the generated client matches openapi/openapi.yaml"

web-test:  ## The console's unit tests (ordering; no DOM, no server)
	cd web && npm test

web-build: web-codegen-check web-test  ## Typecheck, test and build the admin console
	cd web && npm run build

# ------------------------------------------------------------------------- gates

# `web-codegen-check` belongs here and was only in `web-build`. Three separate
# times this session a contract edit passed `ci-checks` and shipped a stale
# generated client, caught later by a build nobody was obliged to run. It needs
# no services — there was never a reason for it to sit outside this list, and a
# gate that only runs when you remember it is not a gate.
web-lint:  ## eslint over the console
	cd web && npx eslint src

dev:  ## Zero-config local launch: services, venv, migrate, seed, run the API
	@bash scripts/dev.sh

dev-web:  ## The admin console on :5173 (needs `make dev` in another terminal)
	@bash scripts/dev-web.sh

dev-stop:  ## Stop the development services, keeping the data
	docker compose -f docker-compose.dev.yml down

dev-reset:  ## Stop them and DELETE the development database
	docker compose -f docker-compose.dev.yml down -v

api-docs:  ## Regenerate docs/api/student-app.md by performing the flows
	WRITE_API_DOCS=1 $(PYTEST) tests/integration/test_api_examples.py -q

ci-parity:  ## FAIL when a gate in `make ci` has no CI step
	$(PYTHON) scripts/check_ci_parity.py

ci-checks: lint web-lint contracts types spec console build-def ci-parity web-codegen-check test-unit  ## Everything that needs no services

ci-tests: coverage migrations invariants write-paths smoke  ## Everything needing PostgreSQL, ffmpeg, Redis, MinIO

ci: ci-checks ci-tests  ## The whole pipeline, exactly as CI runs it

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage coverage.json var/media
