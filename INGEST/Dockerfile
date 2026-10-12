# One image for every Python service; the command picks the role (collect, sink, detect, api, sim).

# --- build the UI -------------------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
ARG VITE_API_BASE=""
RUN npm run build

# --- build the Python wheel and its dependencies ------------------------------------------------
FROM python:3.11-slim AS build
WORKDIR /src
RUN python -m venv /venv
COPY pyproject.toml ./
COPY ghostfleet ./ghostfleet
RUN /venv/bin/pip install --no-cache-dir .

# --- runtime: no compiler, no node, no pip cache, not root ---------------------------------------
FROM python:3.11-slim
RUN useradd --system --uid 10001 --home /app ghost && mkdir -p /app/data/raw && chown -R ghost /app
COPY --from=build /venv /venv
# The API serves the UI from <site-packages>/web/dist, next to the installed package.
COPY --from=web /web/dist /venv/lib/python3.11/site-packages/web/dist
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 GHOST_ARCHIVE_DIR=/app/data/raw
WORKDIR /app
USER ghost
ENTRYPOINT ["ghostfleet"]
CMD ["api", "--host", "0.0.0.0", "--port", "8000"]
