# One image serves the API and the built web UI. Neither Node nor a compiler reaches the final stage.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim AS deps
WORKDIR /src
COPY pyproject.toml ./
COPY squeeze/__init__.py squeeze/__init__.py
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 SQUEEZE_DB=/var/lib/squeeze/squeeze.db SQUEEZE_RAW_DIR=/var/lib/squeeze/raw \
    SQUEEZE_PACKAGES_DIR=/app/release SQUEEZE_WEB_DIR=/app/web
RUN useradd --system --uid 10001 squeeze && mkdir -p /var/lib/squeeze && chown squeeze /var/lib/squeeze
COPY --from=deps /install /usr/local
WORKDIR /app
COPY squeeze/ squeeze/
COPY targets.json ./
COPY results/raw/ seed/
COPY release/ release/
COPY --from=web /web/dist web/
USER squeeze
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status != 200)"
# Seed from the committed raw results (idempotent), then serve.
CMD ["sh", "-c", "python -m squeeze ingest seed && exec python -m squeeze serve --host 0.0.0.0 --port 8000"]
