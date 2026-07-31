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

# Honour $PORT so the image runs unchanged on Fly, Render, Railway, Cloud Run…
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health')"

CMD ["sh", "-c", "uvicorn relay.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
