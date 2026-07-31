FROM python:3.12-slim

WORKDIR /app

# Install the package first so source edits don't bust the dependency layer
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY kb ./kb

# SQLite lives on a mounted volume in production (fly.toml mounts /data)
ENV RELAY_DB_PATH=/data/relay.db
RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
