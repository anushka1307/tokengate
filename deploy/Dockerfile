# syntax=docker/dockerfile:1
#
# TokenGate container image (docs/DECISIONS.md, D22).
# Build from the repo root:
#   docker build --platform linux/amd64 -f deploy/Dockerfile -t tokengate .
#
# Base images are pinned by digest: a tag can be re-pointed, a digest can't. To update, look up
# the new digest (e.g. `docker buildx imagetools inspect python:3.12-slim`) and change both.

# uv binary only (copied into the build stage below).
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, so this layer is cached until pyproject.toml or uv.lock change.
# --locked fails the build if uv.lock is out of date; --no-dev leaves test tools out.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------- runtime stage
# Same base image as the build stage, so the venv's interpreter path is valid here.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    TOKENGATE_ENV=prod \
    PORT=8080
RUN groupadd --system --gid 10001 tokengate \
 && useradd --system --uid 10001 --gid tokengate --no-create-home --shell /usr/sbin/nologin tokengate
WORKDIR /srv
COPY --from=build /opt/venv /opt/venv
# App code is owned by root and only readable by the app user: a compromised process can't
# rewrite its own code.
COPY app ./app
USER 10001:10001
EXPOSE 8080
# Exec form: python is PID 1 and receives SIGTERM directly, so graceful shutdown works.
CMD ["python", "-m", "app"]
