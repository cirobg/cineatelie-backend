# cineatelie-backend

API, worker and database schema for Cine Ateliê (ADR-017: this repository owns the schema
outright, since it is the database's only consumer). Architecture, ADRs and specs live in
the sibling [`cineatelie`](https://github.com/cirobg/cineatelie) repository — start there,
specifically `03-spec/spec-20260920-implementation-plan.md`.

## Stack

FastAPI + SQLAlchemy 2.x async + `asyncpg` (ADR-004), PostgreSQL 15+ with Row-Level
Security (ADR-001), Alembic migrations applied verbatim from a hand-maintained baseline
(database spec §8.3 — read this before touching `db/`).

## Running locally

```bash
cp .env.example .env
docker compose up
```

This starts three containers (`cineatelie-db`, `cineatelie-migrate`, `cineatelie-api`):
Postgres boots, `alembic upgrade head` applies the baseline schema and reference data, and
the API comes up on `http://localhost:8000` once the migration succeeds. Check
`http://localhost:8000/healthz`.

See `db/README.md` for exactly what the migration step does and why there are three
database roles.

## Development without Docker

```bash
pip install -e ".[dev]"
ruff check src tests
pytest
```

Running the API or the migration outside Docker needs a real PostgreSQL reachable at the
URLs in `.env` — see `.env.example`.

## Repository layout

```
src/cineatelie/
  core/            framework-free primitives: config, errors, logging, ids, dates, pagination
  shared_kernel/   domain value objects: Money, Percentage, DocumentNumber, DateRange, enums
  platform/        cross-cutting infrastructure (db, auth, storage, jobs, middleware, audit)
  modules/         vertical business modules, one per bounded context (ADR-004)
db/
  baseline/        the design baseline, applied verbatim — see db/README.md
  versions/        Alembic revisions
  seeds/           reference.sql (every environment), demo.sql (local only)
  roles/           per-environment role bootstrap for anything that isn't Docker Compose
  tests/           the tenant-isolation suite (built in M1)
contract/          openapi.json (generated), VERSION
```

## Status

M0 — Foundations. See `cineatelie/04-implementation/` for what has been built, what is
pending, and which of the implementation plan's highest-risk items are addressed.
