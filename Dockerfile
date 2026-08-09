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

# sh -c stays because ${PORT:-8000} needs a shell to expand, but exec replaces that
# shell with uvicorn so uvicorn is PID 1 — otherwise the platform's SIGTERM is
# delivered to sh, which does not forward it, and the drain never runs at all.
# --timeout-graceful-shutdown because uvicorn's default is None: it waits forever for
# an in-flight SSE stream, and Fly SIGKILLs at kill_timeout instead. 20 nests inside
# fly.toml's kill_timeout (30) and outside settings.shutdown_drain_seconds (5).
CMD ["sh", "-c", "exec uvicorn relay.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 20"]
