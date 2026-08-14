"""
BotTalk — Web UI authentication (human user/password login).

Uses a simple session cookie with a signed token.  Credentials are read from
environment variables with sensible defaults.
"""

from __future__ import annotations

import os
import secrets
import sys

from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_USERNAME = "BOTTALK_WEB_USERNAME"
ENV_PASSWORD = "BOTTALK_WEB_PASSWORD"
ENV_SECRET = "BOTTALK_SECRET_KEY"

SESSION_KEY = "bottalk_user"

_username: str | None = None
_password: str | None = None


def get_credentials() -> tuple[str, str]:
    """Return (username, password) from env or auto-generated defaults."""
    global _username, _password

    if _username is not None and _password is not None:
        return _username, _password

    _username = os.environ.get(ENV_USERNAME) or "admin"

    pw = os.environ.get(ENV_PASSWORD)
    if not pw:
        pw = secrets.token_hex(16)
        print(
            f"[BotTalk] No {ENV_PASSWORD} set. Web login: "
            f"username={_username} password={pw}",
            file=sys.stderr,
        )
    _password = pw

    return _username, _password


def get_secret_key() -> str:
    """Return a secret key for session signing, auto-generating if needed."""
    key = os.environ.get(ENV_SECRET)
    if not key:
        key = secrets.token_hex(32)
        os.environ[ENV_SECRET] = key
    return key


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def verify_web_login(username: str, password: str) -> bool:
    """Check username/password against configured credentials."""
    expected_user, expected_pass = get_credentials()
    return username == expected_user and password == expected_pass


def is_authenticated(request: Request) -> bool:
    """Check if the request has a valid web session."""
    session = request.session
    return session.get(SESSION_KEY) is not None


async def require_web_auth(request: Request) -> None:
    """FastAPI dependency — redirect to login if not authenticated."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


async def optional_web_auth(request: Request) -> bool:
    """FastAPI dependency — returns True if authenticated, False otherwise.

    Does not redirect — used by templates to show/hide UI elements.
    """
    return is_authenticated(request)
