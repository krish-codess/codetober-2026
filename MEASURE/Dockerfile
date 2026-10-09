# syntax=docker/dockerfile:1
# Two images from one file:
#   --target app   the batch jobs and the serving API (python -m wrapped ...)
#   --target web   the static UI behind nginx, standing in for a CDN edge
# Neither carries a compiler, Node, or npm.

# ---- build the UI
FROM node:22-slim AS web-build
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

# ---- app: interpreter, pinned wheels, source, dbt project, migrations
FROM python:3.11-slim AS app
RUN useradd --create-home --uid 10001 wrapped && mkdir /data && chown wrapped /data
WORKDIR /app
COPY --from=deps /venv /venv
COPY wrapped/ wrapped/
COPY dbt/ dbt/
COPY migrations/ migrations/
COPY alembic.ini ./
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WRAPPED_DATA_DIR=/data
USER wrapped
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["python", "-m", "wrapped"]
CMD ["serve", "--host", "0.0.0.0"]

# ---- web: static files and the edge configuration, as an unprivileged user
FROM nginxinc/nginx-unprivileged:1.29-alpine AS web
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build /web/dist /usr/share/nginx/html
EXPOSE 8080
