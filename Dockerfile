# One image, four processes. `docker-compose.yml` decides which one a container
# runs — the API, the actor pool, the scheduler, or the one-shot migration. There
# is deliberately no HEALTHCHECK in this file: an image-level check is inherited
# by every container built from it, and the worker and the scheduler serve no
# HTTP, so they would sit permanently `unhealthy` and block `depends_on`. The
# checks live per service in compose, where they can differ.
#
# Pinned by tag AND digest, the convention `.github/workflows/ci.yml` sets and
# argues for: the tag says what it is to a human, the digest is what Docker
# actually resolves. The Debian codename is in the tag too, because plain
# `python:3.12-slim` currently *is* trixie and will silently become the next
# Debian on its release day — which would change the ffmpeg major version
# underneath the transcode worker on a rebuild that changed no code.
#
# To bump: pick a newer tag and resolve its index digest.
#
#   TOKEN=$(curl -sS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | jq -r .token)
#   curl -sSI -H "Authorization: Bearer $TOKEN" \
#     -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/python/manifests/<TAG> | grep -i docker-content-digest
#
# Resolved 2026-08-03 — Python 3.12.13 on Debian 13 (trixie), linux/amd64 and
# linux/arm64 among others. 3.12 because `pyproject.toml` says
# `requires-python = ">=3.12"` and CI tests exactly 3.12.
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# The console's toolchain. Resolved 2026-08-03, same procedure as above.
#
# **This must be here, above the first FROM, and not beside the stage that uses
# it.** An ARG declared after a FROM belongs to that stage; only the ones before
# the first FROM are global, and only global ones can be interpolated into a
# later FROM. It was written next to `FROM ${NODE_IMAGE} AS web` — which reads
# far better and does not build:
#
#     UndefinedArgInFrom: FROM argument 'NODE_IMAGE' is not declared (line 224)
#     failed to solve: base name (${NODE_IMAGE}) should not be blank
#
# And the whole file fails to parse, so nothing builds — not the `web` stage that
# wanted it, not `runtime`, not `dev`. `scripts/check_build_definition.py` now
# fails the build for an interpolated FROM whose ARG is not global.
ARG NODE_IMAGE=node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32


# ── build: third-party dependencies only ─────────────────────────────────────
FROM ${PYTHON_IMAGE} AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1

# A venv rather than the system site-packages, because a venv is one directory
# that copies cleanly into the next stage — which is what keeps the build tooling
# out of the runtime layer.
#
# `--without-pip`, and then the SYSTEM pip installs into it with `--python`. A
# plain `python -m venv` seeds pip and setuptools into the venv, and the venv is
# the thing that gets copied forward — so the runtime image would ship a package
# installer despite the multi-stage build. Verified: it did, until this line.
# `pip --python` exists for exactly this and writes the venv's own shebang into
# the console scripts.
RUN python -m venv --without-pip /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Only the lock. This layer must not depend on `app/`, or every one-line
# handler change re-downloads the wheels for a 192 MB venv (measured) over a
# Tashkent uplink. It used to be `pyproject.toml`, which was worse in both
# directions: a ruff-config edit re-downloaded the venv, and a dependency edit
# did not change what got installed so much as WHEN it was resolved — see the
# lock paragraph below.
COPY requirements.txt /src/requirements.txt

# The locked dependencies, and NOT the project itself.
#
# Not installing `app` is the point, and it is not tidiness.
# `app/modules/qtypes/registry.py` resolves the question-type definitions
# relative to its own file:
#
#     REGISTRY_ROOT = Path(__file__).resolve().parents[3] / "registry"
#
# With `app/` in site-packages that is `/opt/venv/lib/python3.12/registry`, which
# does not exist — and `Registry.from_directory` over a missing directory does
# not raise. Verified against this tree: the same call that loads 17 question
# types from the source layout loads 0 from a site-packages layout, silently. The
# image boots, `/healthz` is green, and every authored question is an unknown
# type. So `app` ships as a source tree on PYTHONPATH beside `registry/`, which
# is the layout the code was written against.
#
# `requirements.txt` is `pyproject.toml`'s `[project].dependencies` resolved and
# hash-pinned (`make lock`; `make lock-check` fails CI when the two disagree).
# This RUN used to read the floors out of pyproject.toml with `tomllib` and
# hand them to pip, on the claim that it "cannot drift from what `make install`
# resolves". The constraints could not drift; the resolution did, every time.
# Every floor is a `>=`, so this layer installed whatever PyPI served the last
# time pyproject.toml changed, CI installed whatever it served on every run,
# and a laptop a third set — three environments, three resolutions, and no
# file anywhere saying which versions production actually had. Now the file
# says, and `--require-hashes` refuses a wheel that is not the one it names.
# Extras are excluded: `[dev]` is pytest, ruff and mypy, none of which belong
# on the box — that lock is `requirements-dev.txt`, installed by `dev` below.
RUN /usr/local/bin/pip --python /opt/venv/bin/python install --require-hashes -r /src/requirements.txt


# ── dev ──────────────────────────────────────────────────────────────────────
#
# The image `docker-compose.dev.yml` runs. NOT a smaller production image and
# not a bigger one — a different job, and the differences are all deliberate:
#
#   * **No source is copied in.** `docker-compose.dev.yml` bind-mounts the
#     working tree over /app, so the code the container runs is the code in your
#     editor and `--reload` means what it says. Copying it here would produce an
#     image that goes stale the moment you type.
#   * **Runs as root.** The runtime stage runs as uid 10001, which is right for a
#     server and wrong for a bind mount: on Linux the container would write
#     `var/media` and `__pycache__` as a uid your host user does not own, and on
#     a first run it cannot write them at all. A container that only ever has
#     your own laptop's source in it is not the place to spend that.
#   * **pip is put back and the dev extras come with it**, so a machine with no
#     Python on it can still run the suite. See the RUN below.
#
# Dependencies come from the same `deps` stage the production image uses, and
# that stage installs from the lock, so a developer and the server get the
# identical set — by hash, not by a shared list of floors resolved on two days.
FROM ${PYTHON_IMAGE} AS dev

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
# The wheel cache would be baked into the layer below and never read again — a
# rebuild reuses the Docker layer or starts clean, never the pip cache.
ENV PIP_NO_CACHE_DIR=1

# Same ffmpeg the runtime stage installs and for the same reason — the transcode
# worker shells out to it. Kept here so `docker compose -f docker-compose.dev.yml
# up` can run the real worker rather than a worker that fails on the first audio
# upload.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && command -v ffmpeg && command -v ffprobe

COPY --from=deps /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# `app` is a source tree on PYTHONPATH, never an installed package. The deps
# stage explains why at length: `registry/` is resolved relative to
# `qtypes/registry.py`, and an installed layout silently loads zero question
# types.
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# watchfiles is what `uvicorn --reload` uses to notice an edit. It arrives with
# `uvicorn[standard]`, so this is an assertion rather than an install — a
# reload flag with nothing behind it is a dev container that quietly needs
# restarting after every change.
RUN python -c "import watchfiles"

# pip INTO the venv, and the dev extras with it.
#
# The venv arrives from `deps` built `--without-pip`, which is right for
# production — an installer in a server image is how a file-write bug fetches its
# second stage. Here it is a trap: `/opt/venv/bin` is first on PATH but has no
# pip, so `pip install anything` falls through to the SYSTEM pip and installs
# into the system interpreter, which is not the one `python` resolves to. The
# package appears to install and then does not import.
#
# The dev extras come too, because the whole point of this file is a machine with
# no Python and no make on it:
#
#     docker compose -f docker-compose.dev.yml exec api pytest tests -q --ignore=tests/integration
#     docker compose -f docker-compose.dev.yml exec api ruff check .
#
# The lock ONLY — never `pip install -e .`. The deps stage explains why at
# length: an installed `app` resolves `registry/` into site-packages and loads
# zero question types, silently. `PYTHONPATH=/app` above is how `app` is found,
# and it must stay the only way.
#
# `requirements-dev.txt` is the base lock plus the `[dev]` extra, resolved
# together so the two files cannot disagree on a shared package. Everything
# the venv already has from `deps` is satisfied and skipped; what this adds is
# the toolchain. pip itself is the one unhashed install, in its own command,
# because hash-checking mode refuses to share a command line with anything
# that has no hash.
COPY requirements-dev.txt /src/requirements-dev.txt
RUN /usr/local/bin/pip --python /opt/venv/bin/python install pip \
 && /opt/venv/bin/pip install --require-hashes -r /src/requirements-dev.txt \
 && command -v pip && command -v pytest && command -v ruff

WORKDIR /app
CMD ["uvicorn", "app.api.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]


# ── runtime ──────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

# ffmpeg and ffprobe. `app/platform/audio.py` shells out to both, and
# `scripts/smoke_workers.py` fails rather than skips when they are missing for
# exactly this reason: an image without them passes the entire test suite and
# fails every listening upload a teacher makes.
#
# `nice` is checked in the same breath because `audio.py::_niced` degrades
# silently without it — the transcode still runs, just at normal priority, where
# a 30-minute WAV is 30-60 s of ffmpeg competing with the web workers for the
# same 4 vCPU. It comes from coreutils, which Debian marks Essential, so this is
# an assertion rather than an install.
#
# --no-install-recommends, and the package lists are dropped in the same layer so
# they are not carried in the image.
#
# ffmpeg with `--no-install-recommends` is still a 457 MB layer of codec
# libraries — measured, and about half of a ~1.05 GB image. That is the price of
# the one hard dependency this product has on a binary. It is paid on a rebuild
# of THIS layer only; a code change lands below it and re-pushes a few megabytes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && command -v ffmpeg && command -v ffprobe && command -v nice

# The base image ships a working pip for the SYSTEM interpreter, and it survives
# the multi-stage build because it arrives in the `FROM` rather than in anything
# copied — `--without-pip` on the venv does not touch it. Verified: it was still
# there, on PATH, in the built image. A production container has nothing to
# install, and an installer left in one is how code execution here fetches its
# second stage from PyPI.
#
# Runs before the PATH below, so `python3` is unambiguously the system one.
RUN python3 -m pip uninstall -y pip

ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONPATH=/app
# structlog writes JSON to stdout and Docker reads stdout. Buffered, a crashed
# container's last and most useful lines are still sitting in the pipe.
ENV PYTHONUNBUFFERED=1
# Nothing may write into /app at runtime; see the compileall below.
ENV PYTHONDONTWRITEBYTECODE=1
# Fail closed when this image runs outside `docker-compose.yml` — a hand
# `docker run` to debug something, a systemd unit, a second host. `Settings`
# defaults `environment` to "development" (tests/platform/test_config.py pins
# it: `git clone && make test` must work without a .env), and every consumer
# treats that word as the permissive branch: `config.py::_no_published_secrets`
# only refuses the published placeholder secrets when the environment is NOT
# development, the refresh cookie is only `Secure` then, and /docs is only
# hidden then. Compose sets `ENVIRONMENT: production` and was the only place
# that said so, so a container started any other way booted with the
# published JWT key and no error. Set here, the image itself says it; compose
# still sets it explicitly, and the `dev` stage above keeps development.
ENV ENVIRONMENT=production

COPY --from=deps /opt/venv /opt/venv

# A fixed uid, not just a name. Named volumes inherit ownership from the image at
# first mount (see below), but an operator who ever bind-mounts a host directory
# needs a number to chown it to, and a distro-assigned uid would move between
# rebuilds and lock the process out of its own media directory.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# A source tree, not an installed package — see the deps stage. `registry/` must
# be a sibling of `app/`. `alembic.ini` and `migrations/` are here because
# `alembic upgrade head` runs from this directory and `alembic.ini` says
# `script_location = migrations`.
COPY app        /app/app
COPY registry   /app/registry
COPY migrations /app/migrations
COPY alembic.ini pyproject.toml /app/

# The one script the production box runs. `docker-compose.yml` chains
# `python scripts/bootstrap.py` after `alembic upgrade head` in the `migrate`
# one-shot, and every other service waits on that one-shot completing
# successfully. `.dockerignore` excludes `scripts/` as CI-only — which was true
# until the bootstrap commit wired this file into the deploy path and touched
# neither this file nor the ignore file. Result: `alembic upgrade head`
# succeeded, `python` exited 2 with "can't open file", `sh -c` propagated it,
# and api, worker and scheduler sat behind `service_completed_successfully`
# forever. The dev stack bind-mounts the tree over /app and so never showed it.
# `scripts/check_build_definition.py` now fails when a compose service built
# from this stage names a `scripts/*.py` that no COPY here brings in.
COPY scripts/bootstrap.py /app/scripts/bootstrap.py

# Owned by root, read-only to the app user: a process that can rewrite its own
# code turns a file-write bug into remote code execution. Precompiled here as
# root because the app user cannot write __pycache__ at runtime, so the api's
# four workers, the actor pool and the scheduler would otherwise each re-parse
# the tree on every boot and every restart.
RUN python -m compileall -q /app/app /app/migrations

# Created in the image so the named volumes mounted at these paths come up owned
# by `app`: Docker seeds an empty named volume from whatever is at that path in
# the image, ownership included. Without these two lines the volumes appear owned
# by root and every upload fails with EACCES under a non-root user.
RUN mkdir -p /var/lib/ielts/media /var/lib/ielts/scratch \
 && chown -R app:app /var/lib/ielts

USER app

EXPOSE 8000

# The default is the API. Every other process is spelled out in compose, so that
# file stays a complete description of what runs on the box.
CMD ["gunicorn", "app.api.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000"]


# ── build: the admin console ─────────────────────────────────────────────────
#
# Its own stage so Node never reaches the runtime image: the API container has no
# reason to carry a JavaScript toolchain, and `node_modules` is larger than
# everything else in this file put together.
#
# `NODE_IMAGE` is declared at the top of this file rather than here, where it
# belongs by every other measure. The comment up there says why it has to be.
FROM ${NODE_IMAGE} AS web

WORKDIR /build
# Lockfile first, so a source edit does not re-resolve the dependency tree.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

# The contract, because the client is GENERATED from it. Copying it in rather
# than committing only the output would be the same trap `make web-codegen-check`
# exists to close: a build that silently uses a stale client.
COPY openapi/openapi.yaml /openapi/openapi.yaml
COPY web/ ./
RUN npx openapi-typescript /openapi/openapi.yaml -o src/api/schema.d.ts \
 && npm run build


# ── build: the student app ───────────────────────────────────────────────────
#
# A second Vite build, and a second stage rather than a second container. The
# separation ADR-0002 requires is between ORIGINS, which the Caddyfile provides
# with two site blocks; a second web server on the same box would double the TLS
# machinery to serve static files that are already separated by hostname.
#
# Identical shape to the `web` stage above, including regenerating the typed
# client from the contract rather than trusting the committed copy — the same
# trap `make web-codegen-check` exists to close, and it applies to whichever app
# is being built.
FROM ${NODE_IMAGE} AS student

WORKDIR /build
COPY student/package.json student/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY openapi/openapi.yaml /openapi/openapi.yaml
COPY student/ ./
RUN npx openapi-typescript /openapi/openapi.yaml -o src/api/schema.d.ts \
 && npm run build


# ── serve: Caddy with both front ends baked in ───────────────────────────────
#
# Built here rather than using the stock image with a bind mount, so
# `docker compose up --build` is self-contained. A mount would mean `dist/` has
# to exist on the host first, and the failure when it does not is a blank page
# rather than an error — the worst kind.
#
# The base is the same pinned digest the stock image would have been.
FROM caddy:2-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648 AS caddy

COPY --from=web /build/dist /srv/web
COPY --from=student /build/dist /srv/student
COPY Caddyfile /etc/caddy/Caddyfile
