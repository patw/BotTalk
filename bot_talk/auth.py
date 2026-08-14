"""
BotTalk — API key authentication.

Uses a single API key from the ``BOTTALK_API_KEY`` environment variable.
If the variable is not set, a random key is generated on first start and
printed to stderr so the operator can capture it.
"""

from __future__ import annotations

import os
import secrets
import sys

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------

ENV_KEY_NAME = "BOTTALK_API_KEY"

_api_key: str | None = None


def get_api_key() -> str:
    """Get the configured API key.

    Reads from ``BOTTALK_API_KEY`` env var.  If unset, generates a random
    hex key and prints it to stderr so the operator knows what it is.
    """
    global _api_key
    if _api_key is not None:
        return _api_key

    key = os.environ.get(ENV_KEY_NAME)
    if key:
        _api_key = key
        return _api_key

    # Generate a random key
    key = f"bt_{secrets.token_hex(24)}"
    _api_key = key
    print(
        f"[BotTalk] No {ENV_KEY_NAME} set. Generated API key: {key}",
        file=sys.stderr,
    )
    return _api_key


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

_security = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> None:
    """FastAPI dependency: verify the bearer token matches the configured API key.

    Raises 401 Unauthorized if the key is missing or wrong.

    Usage::

        @app.get("/api/posts")
        async def list_posts(_, auth=Depends(verify_api_key)):
            ...
    """
    expected = get_api_key()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide it as: Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
