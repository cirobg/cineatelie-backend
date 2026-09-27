# One image for the API, the worker and the migration step (ADR-010: "the worker is the
# same container image as the API... there is no second codebase and no drift"). Which one
# runs is decided by the command docker-compose.yml / the deploy passes, not by the image.
#
# `slim-bookworm` named explicitly rather than the floating `slim` tag, so a future Debian
# release bump is a deliberate version-string change here, not a silent surprise on the next
# unrelated rebuild. Rebuild with `docker build --pull --no-cache` periodically (or let
# Dependabot/Renovate bump the digest) to actually pick up upstream OS security patches —
# pinning a tag only helps reproducibility, it does nothing for CVEs on its own.
#
# On the base image's CVE count (2026-09-27): python:3.12-slim-bookworm's OS packages carry
# 3 critical / 12 high CVEs per Docker Hub's own scan of that exact layer -- none of it is
# anything this Dockerfile's own lines put there (non-root/HEALTHCHECK don't touch OS package
# versions). Tried python:3.13-slim-bookworm as a fix, on the hypothesis that a newer Python
# line ships against a more recently-patched Debian snapshot: pulled, built, smoke-tested
# (boots, /healthz 200) -- fully compatible, every dependency already ships a cp313 wheel --
# but its OS layer actually carries MORE high CVEs (15), disproving the hypothesis. This is
# base-image snapshot timing, not something version-picking fixes. Staying on 3.12. The real
# levers, if this needs to actually go down: rebuild periodically with `--pull --no-cache` to
# catch upstream Debian patches as they ship, or move to a non-Debian minimal base
# (distroless/Chainguard) as its own separate effort with its own wheel-compatibility check --
# not attempted here. Meanwhile, running as non-root already bounds the practical blast radius
# of most of these regardless of the raw count.
FROM python:3.12-slim-bookworm AS base

WORKDIR /app

# psycopg[binary] and asyncpg both ship prebuilt wheels for this platform, so no compiler
# toolchain is needed here — keeps the image small and the build fast.
COPY pyproject.toml README.md ./
COPY src ./src
COPY db ./db
COPY alembic.ini ./

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as an unprivileged user (debian_slim already ships one) rather than root — the
# default before this change, and the single highest-value hardening step for a container
# with no other reason to run as root: an RCE in the app or a dependency is then confined to
# this user's own (nonexistent) privileges, not root-in-container.
USER nobody

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]

CMD ["uvicorn", "cineatelie.main:app", "--host", "0.0.0.0", "--port", "8000"]
