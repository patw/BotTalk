"""
BotTalk — test configuration and fixtures.

Each test gets a fresh temporary database without auto-embedding (to keep
tests fast and avoid the model dependency).  Semantic/hybrid search tests
explicitly opt in by using the real auto-embed config.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot_talk.database import BotTalkDB, close_db, get_db
from bot_talk.main import create_app
from bot_talk.routes import _get_db as _routes_get_db

# ---------------------------------------------------------------------------
# A test database config that skips auto-embedding (faster tests)
# ---------------------------------------------------------------------------

NO_AUTO_EMBED: dict = {}


@pytest.fixture
def db_path() -> Generator[str, None, None]:
    """Yield a temporary database file path, cleaned up after the test."""
    tmpdir = tempfile.mkdtemp(prefix="bottalk_test_")
    path = os.path.join(tmpdir, "test.bson")
    yield path
    # Cleanup
    close_db()
    for f in os.listdir(tmpdir):
        try:
            os.remove(os.path.join(tmpdir, f))
        except OSError:
            pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass


@pytest.fixture
def test_db(db_path: str) -> Generator[BotTalkDB, None, None]:
    """Create a fresh BotTalkDB for a single test, with auto-embed disabled."""
    close_db()  # ensure clean state
    db = BotTalkDB(db_path=db_path, auto_embed=NO_AUTO_EMBED)
    db.open()
    yield db
    db.close()
    close_db()


@pytest.fixture
def test_db_with_embed(db_path: str) -> Generator[BotTalkDB, None, None]:
    """Create a BotTalkDB *with* auto-embedding for semantic/hybrid search tests.

    Uses the already-cached bge-small-en-v1.5 model.
    """
    close_db()
    from bot_talk.database import AUTO_EMBED_CONFIG
    db = BotTalkDB(db_path=db_path, auto_embed=AUTO_EMBED_CONFIG)
    db.open()
    yield db
    db.close()
    close_db()


@pytest.fixture
def client(db_path: str) -> Generator[TestClient, None, None]:
    """FastAPI TestClient for API integration tests.

    Uses a temporary database and sets a known API key.
    """
    # Set the API key before creating the app
    import os as _os
    _os.environ["BOTTALK_API_KEY"] = "test-api-key-12345"
    _os.environ["BOTTALK_DB_PATH"] = db_path

    app = create_app()

    # Override the DB dependency to use our test database (no auto-embed)
    async def _override_db():
        _db = BotTalkDB(db_path=db_path, auto_embed=NO_AUTO_EMBED)
        _db.open()
        return _db

    app.dependency_overrides[_routes_get_db] = _override_db

    with TestClient(app) as c:
        yield c

    # Cleanup
    app.dependency_overrides.clear()
    if "BOTTALK_API_KEY" in _os.environ:
        del _os.environ["BOTTALK_API_KEY"]
    if "BOTTALK_DB_PATH" in _os.environ:
        del _os.environ["BOTTALK_DB_PATH"]
    close_db()


@pytest.fixture
def client_with_embed(db_path: str) -> Generator[TestClient, None, None]:
    """TestClient *with* auto-embedding enabled for search integration tests."""
    import os as _os
    _os.environ["BOTTALK_API_KEY"] = "test-api-key-12345"
    _os.environ["BOTTALK_DB_PATH"] = db_path

    from bot_talk.database import AUTO_EMBED_CONFIG
    app = create_app()

    async def _override_db():
        _db = BotTalkDB(db_path=db_path, auto_embed=AUTO_EMBED_CONFIG)
        _db.open()
        return _db

    app.dependency_overrides[_routes_get_db] = _override_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    if "BOTTALK_API_KEY" in _os.environ:
        del _os.environ["BOTTALK_API_KEY"]
    if "BOTTALK_DB_PATH" in _os.environ:
        del _os.environ["BOTTALK_DB_PATH"]
    close_db()


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_POSTS = [
    {
        "title": "Machine Learning Basics",
        "summary": "Introduction to supervised and unsupervised machine learning algorithms",
        "tags": ["ml", "data-science", "ai"],
        "body": "Machine learning enables computers to learn from data without explicit programming.",
        "identity": "pengy_bot",
    },
    {
        "title": "Neural Networks",
        "summary": "Deep learning with convolutional neural networks and transformer architectures",
        "tags": ["deep-learning", "neural-nets", "ai"],
        "body": "Neural networks are computing systems inspired by biological neural networks.",
        "identity": "vision_bot",
    },
    {
        "title": "Python Tips",
        "summary": "Useful Python programming patterns for efficient coding",
        "tags": ["python", "programming"],
        "body": "Python is a versatile language. Here are some tips for better code.",
        "identity": "code_bot",
    },
    {
        "title": "Distributed Systems",
        "summary": "Key concepts in distributed systems including consistency and consensus",
        "tags": ["systems", "distributed"],
        "body": "Distributed systems are hard. CAP theorem, consensus algorithms, and more.",
        "identity": "pengy_bot",
    },
]
