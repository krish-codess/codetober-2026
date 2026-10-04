# Build context is the repository root (see docker-compose.yml) so the committed reports/ can be copied.
# syntax=docker/dockerfile:1

# --- build stage: resolve and install dependencies; nothing from here but /install is shipped ---
FROM python:3.11-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY backend/pyproject.toml ./
COPY backend/parse_app ./parse_app
RUN pip install --prefix=/install .

# --- runtime stage: interpreter + installed packages only (no pip cache, no compiler, no tests) ---
FROM python:3.11-slim
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    DATA_DIR=/data REPORTS_DIR=/app/reports
RUN useradd --system --uid 10001 --create-home app && mkdir /data && chown app /data
COPY --from=build /install /usr/local
WORKDIR /app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY reports ./reports
USER app
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "parse_app.api:app", "--host", "0.0.0.0", "--port", "8000"]
