# BotTalk — Docker
#
# Multi-stage build:
#   1. Build stage — pip-installs the Python dependencies
#   2. Runtime stage — minimal image with just the Python app
#
# Usage:
#   docker build -t bottalk .
#   docker run -p 8000:8000 \
#       -e BOTTALK_API_KEY=my_secret \
#       -e BOTTALK_WEB_PASSWORD=my_password \
#       -v bottalk_data:/data \
#       bottalk

# ── Build stage ────────────────────────────────────────────────────────────
FROM python:3.14-slim-trixie AS builder

WORKDIR /build

# Install moofile from PyPI.  It ships prebuilt abi3 wheels for x86_64 and
# aarch64, so nothing is compiled here.  There is no sdist: on any other
# architecture pip falls back to the pure-Python wheel, which cannot autoembed —
# BotTalk needs one of the two native platforms.
#
# The base image must be trixie (glibc 2.41), not bookworm (2.36): the wheels
# are tagged manylinux_2_28 but the native library references glibc 2.38's
# __isoc23_strtol* symbols (unversioned, so auditwheel misses them).  On older
# glibc the native module fails to import and moofile silently falls back to
# pure Python, with no autoembedding.  True of moofile 1.2.5 and 1.3.0.
RUN pip install --no-cache-dir moofile==1.3.0 "fastapi[standard]" uvicorn python-multipart itsdangerous "jinja2<3.1.6"

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.14-slim-trixie AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY bot_talk/ ./bot_talk/
COPY main.py .
COPY .env.template .

# Create a volume mount point for the database
VOLUME /data

# Expose the web/API port
EXPOSE 8000

# Default environment
ENV BOTTALK_HOST=0.0.0.0
ENV BOTTALK_PORT=8000
ENV BOTTALK_DB_PATH=/data/bottalk.bson
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

ENTRYPOINT ["python3", "-m", "bot_talk.main"]
