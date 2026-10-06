# syntax=docker/dockerfile:1

# ---- build the UI. Node never reaches the final image.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- resolve Python dependencies into a virtualenv, from the pins in pyproject.toml
FROM python:3.11-slim AS deps
COPY pyproject.toml /tmp/pyproject.toml
RUN python -m venv /venv \
 && python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# ---- runtime: interpreter, pinned wheels, source, built UI, published results
FROM python:3.11-slim
RUN useradd --create-home --uid 10001 bakeoff && mkdir /data && chown bakeoff /data
WORKDIR /app
COPY --from=deps /venv /venv
COPY bakeoff/ bakeoff/
COPY results/ results/
COPY --from=web /web/dist web/dist
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LAKE_DATA_DIR=/data \
    LAKE_PUBLISHED_DIR=/app/results
USER bakeoff
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
ENTRYPOINT ["python", "-m", "bakeoff"]
CMD ["serve"]
