# BotTalk — Docker
#
# Multi-stage build:
#   1. Build stage — installs Rust + compiles moofile native extension
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
FROM python:3.14-slim-bookworm AS builder

WORKDIR /build

# Install Rust (needed to compile moofile's native extension from source)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain
RUN curl --proto '=https' --tls v1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install moofile from PyPI (it will build the native extension from source
# since no pre-built wheel matches the container's Python exactly)
RUN pip install --no-cache-dir moofile "fastapi[standard]" uvicorn python-multipart itsdangerous "jinja2<3.1.6"

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS runtime

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
