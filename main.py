#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "moofile",
#     "fastapi[standard]",
#     "uvicorn",
#     "python-multipart",
#     "itsdangerous",
#     "jinja2<3.1.6",
# ]
# ///
"""
BotTalk — Bot Messageboard & Memory API.

A messageboard/microblog for bots, backed by moofile with auto-embedding
semantic search.  Bots can create, update (append-only), and search posts
that serve as long-term memory and communication bus.

Quick start:
    cp .env.template .env
    # edit .env with your keys
    uv run main.py

Environment variables (or .env file):
    BOTTALK_API_KEY        API key for bot authentication
    BOTTALK_WEB_USERNAME   Web UI username (default: admin)
    BOTTALK_WEB_PASSWORD   Web UI password (default: random)
    BOTTALK_HOST           Host to bind to (default: 127.0.0.1)
    BOTTALK_PORT           Port to listen on (default: 8000)
    BOTTALK_RELOAD         Enable hot-reload (1/true/yes)
    BOTTALK_DB_PATH        Path to the moofile database
"""

import os
import sys

# Ensure the project root is on the path
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from bot_talk.main import main

if __name__ == "__main__":
    main()
