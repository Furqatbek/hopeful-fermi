# The build entrypoint. Every gate CI runs is a target here.
#
# The point is not convenience. It is that every GATE lives here rather than in
# `.github/workflows/ci.yml`, so there is no second place for the real build to
# live. The workflow does carry a few commands of its own — starting MinIO,
# installing ffmpeg, `npm run build` in the web job — and it may: those are
# environment setup, not gates. `scripts/check_ci_parity.py` enforces the half
# that matters, that every target in `ci-checks`/`ci-tests` has a CI step, and it
# exists because the two DID drift: three gates were in `make ci` and in no
# workflow step at all. If you can run `make ci` you can reproduce a
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
# The lock tool. Only `lock` and `lock-check` use it; `install` puts it on the
# box so `make ci` on a laptop runs the same gate CI does.
UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install lint format contracts types spec test test-unit test-fast \
        migrations invariants smoke coverage ci ci-checks ci-tests clean \
        lock lock-check

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the locked dev dependencies, then the package (editable, no deps)
	@# From the lock, not from `pip install -e ".[dev]"`. Every entry in
	@# pyproject.toml is a `>=` floor, so a resolve-from-floors install fetched
	@# whatever PyPI served that day: CI resolved fresh on every run, the
	@# Dockerfile resolved whenever pyproject.toml last changed, and a laptop
	@# resolved whenever it last ran this — three environments, three sets. The
	@# lock is resolved FROM the floors (`make lock`) and checked against them
	@# (`make lock-check`), so the floors stay the policy and the lock is the
	@# resolution. Hashes in the file switch pip into hash-checking mode by
	@# themselves; the editable install is a second command because pip refuses
	@# to mix an unhashed editable with a hashed requirements file — which is why
	@# it is `--no-deps`: the dependencies are the lock's business.
	$(PYTHON) -m pip install --require-hashes -r requirements-dev.txt
	$(PYTHON) -m pip install -e . --no-deps
	@# The lock tool itself. Not in the lock, because the lock is what it writes.
	$(PYTHON) -m pip install --quiet "uv>=0.8"

# ---------------------------------------------------------------- static checks

lint:  ## Ruff
	ruff check $(SOURCES)

format:  ## Ruff, fixing what it can
	ruff check --fix $(SOURCES)

contracts:  ## import-linter: the module boundaries that make this a modular monolith
	lint-imports

types:  ## mypy, on the layers that are clean today (see docs/design/0011-ci.md §5)
	mypy app/platform app/modules --ignore-missing-imports

spec:  ## The OpenAPI document, the routes served, and the fields implemented
	$(PYTHON) scripts/validate_openapi.py
	$(PYTHON) scripts/check_api_coverage.py
	$(PYTHON) scripts/check_schema_conformance.py

console:  ## Every admin endpoint has a screen, and every exemption is current
	$(PYTHON) scripts/check_console_coverage.py

build-def:  ## FAIL when the Dockerfile, .dockerignore and compose files disagree
	$(PYTHON) scripts/check_build_definition.py

case:  ## FAIL on names differing only by case — invisible here, fatal on Windows/macOS
	$(PYTHON) scripts/check_case_collisions.py

# ---------------------------------------------------------------------- the lock

# The flags, spelled once, because the header uv writes into the file records
# the command that produced it — flag order included — and `lock-check` must
# reproduce that header byte for byte. `--universal` resolves for every
# platform at once so one file serves an amd64 runner, an arm64 laptop and the
# image; `--generate-hashes` is what lets pip refuse a substituted wheel. The
# floors in pyproject.toml are still the policy — this is their resolution,
# not their replacement, and `.github/dependabot.yml`'s argument against upper
# bounds stands: Dependabot moves the lock weekly exactly as it moved the
# floors before.
LOCK_FLAGS = --universal --python-version 3.12 --generate-hashes

lock:  ## Re-resolve requirements*.txt from pyproject.toml (after editing its dependencies)
	$(UV) pip compile pyproject.toml $(LOCK_FLAGS) --no-progress -o requirements.txt
	$(UV) pip compile pyproject.toml --extra dev $(LOCK_FLAGS) --no-progress -o requirements-dev.txt

lock-check:  ## FAIL when requirements*.txt no longer match pyproject.toml
	@# Same shape as `web-codegen-check`: regenerate beside the committed file
	@# and diff. uv reuses the pins already in the output file, so a clean tree
	@# reproduces itself and the only thing that moves the result is a change to
	@# pyproject.toml's dependencies — a floor raised past the lock, a package
	@# added and not locked. `--custom-compile-command` keeps the header naming
	@# the real command rather than the scratch path, so the diff is content only.
	@set -e; for spec in ":requirements.txt" "--extra dev:requirements-dev.txt"; do \
		extra="$${spec%%:*}"; file="$${spec#*:}"; \
		cp "$$file" "/tmp/$$file.check"; \
		$(UV) pip compile pyproject.toml $$extra $(LOCK_FLAGS) --no-progress --quiet -o "/tmp/$$file.check" \
			--custom-compile-command "uv pip compile pyproject.toml $${extra:+$$extra }$(LOCK_FLAGS) -o $$file" \
			| sed 's/^/      /'; \
		if ! diff -q "$$file" "/tmp/$$file.check" >/dev/null; then \
			echo "FAIL  $$file is stale against pyproject.toml — run \`make lock\` and commit it"; \
			diff -u "$$file" "/tmp/$$file.check" | head -40; exit 1; \
		fi; \
	done; echo "PASS  requirements.txt and requirements-dev.txt match pyproject.toml"

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

path-params:  ## FAIL when a route declares a path parameter its handler ignores
	$(PYTHON) scripts/check_path_params.py

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

student-install:  ## npm ci in student/ (the lockfile, as with the console)
	cd student && npm ci

student-codegen:  ## Regenerate the student app's typed client
	cd student && npm run codegen

student-codegen-check:  ## FAIL when the student app's committed client has drifted
	@# The console has had this gate since it was written; the student app never
	@# did, and had silently drifted — `section_expired`, and three entitlement
	@# fields from work weeks earlier. The app a candidate sits the exam in was
	@# compiling against a contract the server had moved on from, and nothing
	@# anywhere would have said so.
	@cd student && cp src/api/schema.d.ts /tmp/student.schema.before.d.ts && npm run --silent codegen && \
		if ! diff -q /tmp/student.schema.before.d.ts src/api/schema.d.ts >/dev/null; then \
			echo "FAIL  student/src/api/schema.d.ts is stale — run \`make student-codegen\` and commit it"; \
			diff -u /tmp/student.schema.before.d.ts src/api/schema.d.ts | head -40; \
			cp /tmp/student.schema.before.d.ts src/api/schema.d.ts; exit 1; \
		fi; echo "PASS  the student app's generated client matches openapi/openapi.yaml"

student-test:  ## The student app's unit tests (clock, outbox, marking, themes)
	@# The app a CANDIDATE sits the exam in, and it had no gate at all: 101 tests
	@# and a build that CI never ran, so a break in the exam runner reached a
	@# student before it reached anybody else.
	cd student && npm test

student-typecheck:  ## tsc over the student app (strict, noUncheckedIndexedAccess, exactOptionalPropertyTypes)
	@# The tests above run under vitest, which strips types with esbuild and
	@# checks none of them. student/tsconfig.app.json calls its three strict
	@# flags "the point of this project", and until this target nobody enforced
	@# them: a type error in a screen no test imports (the runner, the review
	@# page) passed CI and failed the Docker `student` stage, which is the first
	@# place `tsc -b` ever ran.
	cd student && npm run typecheck

student-lint:  ## eslint over the student app
	@# `web-lint` has existed since the console got an eslint config; the
	@# student app had the same config and script and no target.
	cd student && npx eslint src

student-build: student-codegen-check student-typecheck student-test  ## Codegen drift check, typecheck, test and build the student app
	cd student && npm run build

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

client-parity:  ## FAIL when the two apps' copies of the API transport layer drift
	$(PYTHON) scripts/check_client_parity.py

# `web-build` and `student-build` are here as well as on their own: the console
# was built on every push and the app a candidate sits the exam in was not,
# and because neither was in an aggregate `ci-parity` could not see the
# asymmetry. Their prerequisites are already in this list; make runs each
# target once per invocation, so nothing runs twice.
ci-checks: lint web-lint contracts types spec console build-def case path-params ci-parity lock-check client-parity web-codegen-check student-codegen-check student-test student-typecheck student-lint test-unit web-build student-build  ## Everything that needs no services

ci-tests: coverage migrations invariants write-paths smoke  ## Everything needing PostgreSQL, ffmpeg, Redis, MinIO

ci: ci-checks ci-tests  ## The whole pipeline, exactly as CI runs it

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage coverage.json var/media
