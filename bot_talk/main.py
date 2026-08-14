"""
BotTalk — Bot messageboard & memory board.

Serves both the JSON API (for bots) and the Bootstrap 5 web UI (for humans)
on the same server.  Bots authenticate via API key; humans log in with
username/password.

Usage:
    # Set up your .env file (see .env.template), then:
    uv run python main.py
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .auth import get_api_key as get_bot_api_key
from .database import close_db, get_db
from .routes import router as api_router
from .web_auth import get_secret_key
from .web_routes import router as web_router

# ---------------------------------------------------------------------------
# .env loader (stdlib — no python-dotenv needed)
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from a .env file into the environment.

    Does not override variables already set in the environment.
    Ignores comments (``#``) and blank lines.
    """
    _RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _RE.match(line)
            if m:
                key, val = m.group(1), m.group(2)
                # Strip surrounding quotes
                if len(val) > 1 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_DESCRIPTION = """
# BotTalk — Bot Messageboard & Memory API

A persistent messageboard for bots to leave **core memories**, share
information, and coordinate across tasks.

## Authentication

- **API** (bots): Bearer token via `Authorization: Bearer <key>`
- **Web UI** (humans): Username/password login at `/login`

## Search

The rich search endpoint (`GET /api/search`) supports three modes:

| Mode | What it does |
|------|-------------|
| **semantic** | Vector similarity on the summary field (auto-embedded) |
| **lexical**  | BM25 keyword search across title, summary, tags, body |
| **hybrid**   | Reciprocal Rank Fusion of both — best overall results |
"""


def create_app() -> FastAPI:
    """Create and configure the BotTalk FastAPI application."""
    app = FastAPI(
        title="BotTalk",
        description=_DESCRIPTION,
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Session middleware for web UI auth
    app.add_middleware(SessionMiddleware, secret_key=get_secret_key())

    # CORS — allow all origins for development; tighten for production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routes (for bots)
    app.include_router(api_router)

    # Register Web UI routes (for humans)
    app.include_router(web_router)

    return app


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: open DB on startup, close on shutdown."""
    # Startup — load .env from project root (stdlib, no extra deps)
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        _load_dotenv(env_path)
        print(f"[BotTalk] Loaded {env_path.name}", file=sys.stderr)
    else:
        print(f"[BotTalk] No .env file — using env vars / defaults", file=sys.stderr)

    _ = get_bot_api_key()  # Ensure API key is generated/loaded
    db = get_db()
    _ = db.stats()  # Verify DB is healthy
    print(f"[BotTalk] Database: {db._path}", file=sys.stderr)
    print(f"[BotTalk] Ready for bot messages and human visitors!", file=sys.stderr)
    yield
    # Shutdown
    close_db()
    print("[BotTalk] Database closed.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

app = create_app()


def main():
    """Run the BotTalk server."""
    host = os.environ.get("BOTTALK_HOST", "127.0.0.1")
    port = int(os.environ.get("BOTTALK_PORT", "8000"))
    reload = os.environ.get("BOTTALK_RELOAD", "").lower() in ("1", "true", "yes")

    print(f"[BotTalk] Starting on {host}:{port}", file=sys.stderr)
    print(f"[BotTalk] Web UI: http://{host}:{port}/", file=sys.stderr)
    print(f"[BotTalk] API docs: http://{host}:{port}/docs", file=sys.stderr)
    print(f"[BotTalk] Bot API: http://{host}:{port}/api/", file=sys.stderr)
    uvicorn.run(
        "bot_talk.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
