# Build stage: compile the wheel and its dependencies into a virtualenv.
FROM python:3.11-slim AS build
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

# Runtime stage: the virtualenv only. No pip cache, no sources, no build backend.
FROM python:3.11-slim
RUN useradd --create-home --uid 10001 tydlc && mkdir /data && chown tydlc /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH TYDLC_DATA_DIR=/data PYTHONUNBUFFERED=1
USER tydlc
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
CMD ["tydlc", "serve", "--host", "0.0.0.0", "--port", "8000"]
