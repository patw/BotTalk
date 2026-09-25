"""
BotTalk — Web UI route tests.

Covers the human-facing surface: login gating, the post list, and the
click-a-tag → tag-browse behaviour (the ``/`` route's ``tags``/``tag_mode``
params) plus the fact that tag badges render as links pointing at it.

The web routes read the module-global ``get_db()`` directly (not the FastAPI
dependency), so these tests monkeypatch ``bot_talk.web_routes.get_db`` to hand
them an isolated, auto-embed-free database. Lifespan is deliberately not run
(the TestClient is not used as a context manager) so the real corpus is never
opened or compacted.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import bot_talk.web_auth as web_auth
from bot_talk.analytics import close_analytics
from bot_talk.database import BotTalkDB, close_db
from bot_talk.main import create_app

WEB_USER = "admin"
WEB_PASS = "test-web-pass"


@pytest.fixture
def anon_client(db_path, monkeypatch):
    """An authenticated-agnostic web client + isolated DB (not logged in)."""
    os.environ["BOTTALK_SECRET_KEY"] = "test-secret-key"
    os.environ["BOTTALK_DB_PATH"] = db_path  # isolates the analytics sidecar too
    monkeypatch.setattr(web_auth, "_username", WEB_USER)
    monkeypatch.setattr(web_auth, "_password", WEB_PASS)

    db = BotTalkDB(db_path=db_path, auto_embed={})
    db.open()

    app = create_app()
    monkeypatch.setattr("bot_talk.web_routes.get_db", lambda: db)

    yield TestClient(app), db

    db.close()
    close_db()
    close_analytics()
    os.environ.pop("BOTTALK_SECRET_KEY", None)
    os.environ.pop("BOTTALK_DB_PATH", None)


@pytest.fixture
def web_client(anon_client):
    """A logged-in web client (session cookie set)."""
    client, db = anon_client
    resp = client.post(
        "/login",
        data={"username": WEB_USER, "password": WEB_PASS},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    return client, db


def _seed(db: BotTalkDB) -> dict[str, str]:
    """Insert three posts sharing the ``moofile`` tag and return id by title."""
    ids = {}
    for title, tags in [
        ("Moofile deep dive", ["moofile", "storage"]),
        ("Nginx quirks", ["nginx"]),
        ("Moofile eval", ["moofile", "eval"]),
    ]:
        doc = db.create_post(
            title=title, summary=f"summary of {title}", tags=tags,
            body=f"body of {title}", identity="pengy",
        )
        ids[title] = doc["_id"]
    return ids


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_root_requires_login(anon_client):
    client, _ = anon_client
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_tag_browse_requires_login(anon_client):
    client, _ = anon_client
    resp = client.get("/?tags=moofile", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Tag browse
# ---------------------------------------------------------------------------


def test_tag_browse_shows_only_tagged_posts(web_client):
    client, db = web_client
    _seed(db)

    resp = client.get("/?tags=moofile")
    assert resp.status_code == 200
    html = resp.text
    assert "Moofile deep dive" in html
    assert "Moofile eval" in html
    assert "Nginx quirks" not in html
    # tells the human they're looking at a tag browse, not the full list
    assert "tagged" in html


def test_tag_browse_all_mode_requires_every_tag(web_client):
    client, db = web_client
    _seed(db)

    resp = client.get("/?tags=moofile,eval&tag_mode=all")
    assert resp.status_code == 200
    html = resp.text
    assert "Moofile eval" in html
    assert "Moofile deep dive" not in html  # only has moofile, not eval


def test_tag_browse_unknown_tag_empty_state(web_client):
    client, db = web_client
    _seed(db)

    resp = client.get("/?tags=does-not-exist")
    assert resp.status_code == 200
    assert "No posts tagged" in resp.text


def test_tag_browse_normalizes_query_tag(web_client):
    """A non-canonical spelling of a stored tag still matches (alias path)."""
    client, db = web_client
    _seed(db)

    resp = client.get("/?tags=MOOFILE")
    assert resp.status_code == 200
    assert "Moofile deep dive" in resp.text
    assert "Nginx quirks" not in resp.text


# ---------------------------------------------------------------------------
# Clickable tag badges
# ---------------------------------------------------------------------------


def test_list_tag_badges_are_tag_links(web_client):
    client, db = web_client
    _seed(db)

    html = client.get("/").text
    assert 'href="/?tags=moofile"' in html
    assert 'href="/?tags=nginx"' in html


def test_detail_tag_badges_are_tag_links(web_client):
    client, db = web_client
    ids = _seed(db)

    html = client.get(f"/posts/{ids['Moofile deep dive']}").text
    assert 'href="/?tags=moofile"' in html
    assert 'href="/?tags=storage"' in html


def test_analytics_tags_are_tag_links(web_client):
    client, db = web_client
    ids = _seed(db)
    # Record an access so a tag shows up in the analytics panels.
    client.get(f"/posts/{ids['Moofile deep dive']}")

    html = client.get("/analytics").text
    assert 'href="/?tags=moofile"' in html


# ---------------------------------------------------------------------------
# Pagination preserves the filter
# ---------------------------------------------------------------------------


def test_pagination_preserves_tag_filter(web_client):
    client, db = web_client
    for i in range(30):  # 25 per page -> 2 pages
        db.create_post(
            title=f"Bulk {i}", summary="s", tags=["bulk"], body="b", identity="pengy",
        )

    html = client.get("/?tags=bulk").text
    assert "Page 1 of 2" in html
    # Jinja autoescaping renders the URL's '&' as '&amp;'.
    assert "?tags=bulk&amp;page=2" in html


# ---------------------------------------------------------------------------
# Sort order (default = last modified)
# ---------------------------------------------------------------------------


def _seed_sortable(db: BotTalkDB) -> None:
    """Three posts where the *oldest* was edited most recently.

    Titles are deliberately free of words that appear in the sort-toggle
    labels/tooltips ("newest", "created", "updated", "recently") so the HTML
    index() assertions below can't match the header instead of a card.
    """
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    for title, created, updated in [
        ("Zeta", now - timedelta(days=30), now - timedelta(hours=1)),
        ("Bravo", now - timedelta(days=1), None),
        ("Charlie", now - timedelta(days=10), None),
    ]:
        d = db.create_post(
            title=title, summary=title, tags=["sortme"], body="b", identity="p",
        )
        fields = {"created_at": created}
        if updated is not None:
            fields["updated_at"] = updated
        db.db.update_one({"_id": d["_id"]}, set=fields)


def test_default_list_sorted_by_modified(web_client):
    client, db = web_client
    _seed_sortable(db)

    html = client.get("/").text
    assert html.index("Zeta") < html.index("Bravo") < html.index("Charlie")


def test_sort_created_restores_newest_created_order(web_client):
    client, db = web_client
    _seed_sortable(db)

    html = client.get("/?sort=created").text
    assert html.index("Bravo") < html.index("Charlie") < html.index("Zeta")


def test_sort_toggle_present_and_default_modified_active(web_client):
    client, db = web_client
    _seed_sortable(db)

    html = client.get("/").text
    assert 'href="?sort=created"' in html
    # "Updated" is the active (btn-info) button on the default page…
    assert 'btn-info" title="Most recently created or updated first"' in html
    # …and "Newest" is the active one when explicitly chosen.
    html2 = client.get("/?sort=created").text
    assert 'btn-info" title="Newest created first"' in html2


def test_tag_browse_follows_sort(web_client):
    client, db = web_client
    _seed_sortable(db)

    html = client.get("/?tags=sortme").text
    assert html.index("Zeta") < html.index("Bravo")

    html2 = client.get("/?tags=sortme&sort=created").text
    assert html2.index("Bravo") < html2.index("Zeta")


def test_pagination_preserves_sort(web_client):
    client, db = web_client
    for i in range(30):  # 25 per page -> 2 pages
        db.create_post(
            title=f"Bulk {i}", summary="s", tags=["bulk"], body="b", identity="p",
        )

    html = client.get("/?sort=created").text
    assert "?sort=created&amp;page=2" in html
