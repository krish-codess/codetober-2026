# Two images from one file. Neither carries Node, a compiler, or the other's dependencies.
#
#   --target pipeline   the batch projection job (numpy, pyarrow, duckdb, umap-learn, scikit-learn)
#   --target api        the reference API plus the built page (no ML libraries); the default

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim AS deps-api
WORKDIR /src
COPY pyproject.toml ./
COPY dots/__init__.py dots/__init__.py
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS deps-ml
WORKDIR /src
COPY pyproject.toml ./
COPY dots/__init__.py dots/__init__.py
RUN pip install --no-cache-dir --prefix=/install ".[ml]"

FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOTS_DATA_DIR=/var/lib/dots NUMBA_CACHE_DIR=/tmp/numba
RUN useradd --system --uid 10001 dots && mkdir -p /var/lib/dots && chown dots /var/lib/dots
WORKDIR /app

FROM base AS pipeline
COPY --from=deps-ml /install /usr/local
COPY dots/ dots/
USER dots
# Fetch (or generate), validate, project, evaluate and tile. Idempotent: a second run is a no-op.
CMD ["python", "-m", "dots", "all"]

FROM base AS api
ENV DOTS_WEB_DIR=/app/web DOTS_HOST=0.0.0.0 DOTS_PORT=8015
COPY --from=deps-api /install /usr/local
COPY dots/ dots/
COPY --from=web /web/dist web/
USER dots
EXPOSE 8015
# Readiness, not liveness: it reads the manifest, decodes a tile and queries the metadata table.
HEALTHCHECK --interval=10s --timeout=5s --retries=6 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8015/readyz').status != 200)"
CMD ["python", "-m", "dots", "serve"]
