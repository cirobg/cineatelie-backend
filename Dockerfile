# One image for the API, the worker and the migration step (ADR-010: "the worker is the
# same container image as the API... there is no second codebase and no drift"). Which one
# runs is decided by the command docker-compose.yml / the deploy passes, not by the image.
FROM python:3.12-slim AS base

WORKDIR /app

# psycopg[binary] and asyncpg both ship prebuilt wheels for this platform, so no compiler
# toolchain is needed here — keeps the image small and the build fast.
COPY pyproject.toml README.md ./
COPY src ./src
COPY db ./db
COPY alembic.ini ./

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "cineatelie.main:app", "--host", "0.0.0.0", "--port", "8000"]
