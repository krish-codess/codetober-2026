# syntax=docker/dockerfile:1.7
# Multi-stage: dependencies are resolved and installed in `builder`; the runtime image gets only
# the finished virtualenv (no uv, no compiler, no build cache). EXTRAS picks the role:
#   api       -> FastAPI service         (docker build --build-arg EXTRAS=api)
#   pipeline  -> Dagster webserver/daemon (docker build --build-arg EXTRAS=pipeline)
ARG PYTHON=python:3.11.11-slim-bookworm

FROM ${PYTHON} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ARG EXTRAS=api
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra ${EXTRAS}
COPY src ./src
# --reinstall-package: uv caches local builds keyed on pyproject.toml only; without it a code change
# with an unchanged version would silently ship the previously built wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra ${EXTRAS} --reinstall-package goldstandard

FROM ${PYTHON} AS runtime
# pip/setuptools are build tooling: the runtime never installs anything.
RUN python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1;     rm -rf /usr/local/lib/python3.11/ensurepip &&     groupadd -r app && useradd -r -g app -u 10001 -d /app app \
    && mkdir -p /data /opt/dagster/home && chown app:app /data /opt/dagster/home
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY migrations ./migrations
COPY reference ./reference
COPY dagster_home/dagster.yaml /opt/dagster/home/dagster.yaml
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GS_MIGRATIONS_DIR=/app/migrations \
    GS_REFERENCE_DIR=/app/reference \
    GS_DATA_DIR=/data
USER app
EXPOSE 8000 3000
CMD ["uvicorn", "goldstandard.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
