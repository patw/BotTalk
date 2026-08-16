"""
BotTalk — full API integration tests.

Tests every endpoint through the HTTP layer using FastAPI TestClient.
Covers auth, CRUD, human annotations, search, stats, and edge cases.
"""

from __future__ import annotations

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from conftest import SAMPLE_POSTS


# ======================== Auth Tests ========================


class TestAuth:
    """Authentication via Bearer token."""

    def test_health_no_auth(self, client: TestClient):
        """Health check should not require auth."""
        resp = client.get("/api/health")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["status"] == "ok"

    def test_posts_requires_auth(self, client: TestClient):
        """Listing posts without auth returns 401."""
        resp = client.get("/api/posts")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_wrong_key(self, client: TestClient):
        """A wrong API key returns 401."""
        resp = client.get("/api/posts", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_post_create_requires_auth(self, client: TestClient):
        resp = client.post("/api/posts", json={"title": "T", "summary": "S", "tags": [], "body": "B", "identity": "bot"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_put_requires_auth(self, client: TestClient):
        resp = client.put("/api/posts/abc", json={"identity": "bot", "title": "T"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_delete_requires_auth(self, client: TestClient):
        resp = client.delete("/api/posts/abc")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_search_requires_auth(self, client: TestClient):
        resp = client.get("/api/search?q=test")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_stats_requires_auth(self, client: TestClient):
        resp = client.get("/api/stats")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_annotation_get_requires_auth(self, client: TestClient):
        resp = client.get("/api/posts/abc/annotation")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_annotation_put_requires_auth(self, client: TestClient):
        resp = client.put("/api/posts/abc/annotation", json={"annotation": "note"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ======================== CRUD Tests ========================


class TestCreatePost:
    """POST /api/posts — creating posts."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def test_create_post(self, client: TestClient):
        resp = client.post(
            "/api/posts",
            json={"title": "Hello Bot", "summary": "First post", "tags": ["intro"], "body": "Hello from test bot!", "identity": "test_bot"},
            headers=self.AUTH,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["id"] is not None
        assert data["title"] == "Hello Bot"
        assert data["identity"] == "test_bot"
        assert data["summary"] == "First post"
        assert data["tags"] == ["intro"]
        assert data["body"] == "Hello from test bot!"
        assert data["human_annotation"] is None
        assert data["update_history"] == []

    def test_create_post_returns_id(self, client: TestClient):
        resp = client.post(
            "/api/posts",
            json={"title": "T", "summary": "S", "tags": [], "body": "B", "identity": "bot"},
            headers=self.AUTH,
        )
        data = resp.json()
        assert len(data["id"]) > 0  # Should be a non-empty hex string

    def test_create_post_empty_tags(self, client: TestClient):
        resp = client.post(
            "/api/posts",
            json={"title": "T", "summary": "S", "tags": [], "body": "B", "identity": "bot"},
            headers=self.AUTH,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["tags"] == []

    def test_create_post_validation_error(self, client: TestClient):
        """Missing required fields return 422."""
        resp = client.post("/api/posts", json={"title": "T"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_post_body_too_large(self, client: TestClient):
        """Body exceeding 4 KB returns 422."""
        resp = client.post(
            "/api/posts",
            json={"title": "T", "summary": "S", "tags": [], "body": "x" * 4097, "identity": "bot"},
            headers=self.AUTH,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestGetPost:
    """GET /api/posts/{id} — fetching a single post."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _create(self, client: TestClient) -> str:
        resp = client.post(
            "/api/posts",
            json={"title": "GetTest", "summary": "Testing GET", "tags": ["test"], "body": "Body text", "identity": "tester"},
            headers=self.AUTH,
        )
        return resp.json()["id"]

    def test_get_post(self, client: TestClient):
        pid = self._create(client)
        resp = client.get(f"/api/posts/{pid}", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["id"] == pid
        assert data["title"] == "GetTest"
        assert data["identity"] == "tester"

    def test_get_nonexistent(self, client: TestClient):
        resp = client.get("/api/posts/nonexistent", headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_get_post_shows_human_annotation(self, client: TestClient):
        pid = self._create(client)
        # Add annotation
        client.put(f"/api/posts/{pid}/annotation", json={"annotation": "Read this!"}, headers=self.AUTH)
        # Get and check
        resp = client.get(f"/api/posts/{pid}", headers=self.AUTH)
        assert resp.json()["human_annotation"] == "Read this!"


class TestListPosts:
    """GET /api/posts — listing posts with filters."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _seed(self, client: TestClient) -> list[str]:
        ids = []
        for p in SAMPLE_POSTS:
            resp = client.post("/api/posts", json=p, headers=self.AUTH)
            ids.append(resp.json()["id"])
        return ids

    def test_list_all(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["total"] == 4
        assert len(data["posts"]) == 4

    def test_list_empty(self, client: TestClient):
        resp = client.get("/api/posts", headers=self.AUTH)
        assert resp.json()["total"] == 0

    def test_list_pagination(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?limit=2", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 4
        assert len(data["posts"]) == 2

    def test_list_skip(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?skip=2", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 4
        assert len(data["posts"]) == 2

    def test_list_filter_identity(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?identity=pengy_bot", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 2
        for p in data["posts"]:
            assert p["identity"] == "pengy_bot"

    def test_list_filter_tags(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?tags=python", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["posts"][0]["title"] == "Python Tips"

    def test_list_filter_tags_multiple(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?tags=python,systems", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 2

    def test_list_no_match(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?identity=nobody", headers=self.AUTH)
        assert resp.json()["total"] == 0

    def test_list_filter_tags_all(self, client: TestClient):
        """tag_mode=all requires every listed tag to be present."""
        self._seed(client)
        resp = client.get("/api/posts?tags=ai,python&tag_mode=all", headers=self.AUTH)
        assert resp.json()["total"] == 0  # no post has both ai and python

        resp = client.get("/api/posts?tags=ai,ml&tag_mode=all", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["posts"][0]["title"] == "Machine Learning Basics"

    def test_list_filter_tags_any(self, client: TestClient):
        """tag_mode=any (default) matches posts with any listed tag."""
        self._seed(client)
        resp = client.get("/api/posts?tags=ai,python", headers=self.AUTH)
        assert resp.json()["total"] == 3  # ML Basics, Neural Networks, Python Tips

    def test_list_invalid_tag_mode(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts?tags=ai&tag_mode=bad", headers=self.AUTH)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_newest_first(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/posts", headers=self.AUTH)
        posts = resp.json()["posts"]
        timestamps = [p["created_at"] for p in posts]
        assert timestamps == sorted(timestamps, reverse=True)


class TestTags:
    """GET /api/tags — tag cloud with counts."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _seed(self, client: TestClient):
        for p in SAMPLE_POSTS:
            client.post("/api/posts", json=p, headers=self.AUTH)

    def test_tags_requires_auth(self, client: TestClient):
        resp = client.get("/api/tags")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_tags_empty(self, client: TestClient):
        resp = client.get("/api/tags", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["total"] == 0
        assert data["tags"] == []

    def test_tags_counts_sorted(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/tags", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 9
        counts = {t["tag"]: t["count"] for t in data["tags"]}
        assert counts["ai"] == 2
        assert counts["ml"] == 1
        ns = [t["count"] for t in data["tags"]]
        assert ns == sorted(ns, reverse=True)

    def test_tags_prefix(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/tags?prefix=neural", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["tags"][0]["tag"] == "neural-nets"

    def test_tags_min_count(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/tags?min_count=2", headers=self.AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["tags"][0]["tag"] == "ai"

    def test_tags_limit(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/tags?limit=2", headers=self.AUTH)
        data = resp.json()
        assert len(data["tags"]) == 2
        assert data["total"] == 9  # total unaffected by limit


class TestUpdatePost:
    """PUT /api/posts/{id} — updating posts."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _create(self, client: TestClient) -> str:
        resp = client.post(
            "/api/posts",
            json={"title": "Original", "summary": "Original summary", "tags": ["a"], "body": "Original body", "identity": "bot_a"},
            headers=self.AUTH,
        )
        return resp.json()["id"]

    def test_update_title(self, client: TestClient):
        pid = self._create(client)
        resp = client.put(f"/api/posts/{pid}", json={"identity": "bot_b", "title": "Updated"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["title"] == "Updated"
        assert data["summary"] == "Original summary"  # unchanged

    def test_update_appends_history(self, client: TestClient):
        pid = self._create(client)
        resp = client.put(f"/api/posts/{pid}", json={"identity": "bot_b", "title": "V2"}, headers=self.AUTH)
        data = resp.json()
        assert len(data["update_history"]) == 1
        assert data["update_history"][0]["identity"] == "bot_b"

    def test_update_multiple_times(self, client: TestClient):
        pid = self._create(client)
        client.put(f"/api/posts/{pid}", json={"identity": "b1", "title": "V2"}, headers=self.AUTH)
        client.put(f"/api/posts/{pid}", json={"identity": "b2", "body": "New body"}, headers=self.AUTH)
        resp = client.get(f"/api/posts/{pid}", headers=self.AUTH)
        assert len(resp.json()["update_history"]) == 2

    def test_update_nonexistent(self, client: TestClient):
        resp = client.put("/api/posts/badid", json={"identity": "bot", "title": "X"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_update_requires_identity(self, client: TestClient):
        pid = self._create(client)
        resp = client.put(f"/api/posts/{pid}", json={"title": "X"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_update_annotation_field(self, client: TestClient):
        pid = self._create(client)
        resp = client.put(
            f"/api/posts/{pid}",
            json={"identity": "human", "human_annotation": "A note from human"},
            headers=self.AUTH,
        )
        assert resp.json()["human_annotation"] == "A note from human"


class TestDeletePost:
    """DELETE /api/posts/{id} — deleting posts."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _create(self, client: TestClient) -> str:
        resp = client.post(
            "/api/posts",
            json={"title": "ToDelete", "summary": "Will be deleted", "tags": [], "body": "Bye!", "identity": "bot"},
            headers=self.AUTH,
        )
        return resp.json()["id"]

    def test_delete_existing(self, client: TestClient):
        pid = self._create(client)
        resp = client.delete(f"/api/posts/{pid}", headers=self.AUTH)
        assert resp.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_nonexistent(self, client: TestClient):
        resp = client.delete("/api/posts/badid", headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_actually_removes(self, client: TestClient):
        pid = self._create(client)
        client.delete(f"/api/posts/{pid}", headers=self.AUTH)
        resp = client.get(f"/api/posts/{pid}", headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_updates_list(self, client: TestClient):
        pid = self._create(client)
        client.delete(f"/api/posts/{pid}", headers=self.AUTH)
        resp = client.get("/api/posts", headers=self.AUTH)
        assert resp.json()["total"] == 0


# ======================== Human Annotation Tests ========================


class TestHumanAnnotation:
    """Human annotation endpoints — GET/PUT /api/posts/{id}/annotation."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _create(self, client: TestClient) -> str:
        resp = client.post(
            "/api/posts",
            json={"title": "Memory", "summary": "A bot memory", "tags": ["core"], "body": "Important details.", "identity": "mem_bot"},
            headers=self.AUTH,
        )
        return resp.json()["id"]

    def test_set_annotation(self, client: TestClient):
        pid = self._create(client)
        resp = client.put(f"/api/posts/{pid}/annotation", json={"annotation": "Human says: check this"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["human_annotation"] == "Human says: check this"

    def test_get_annotation(self, client: TestClient):
        pid = self._create(client)
        client.put(f"/api/posts/{pid}/annotation", json={"annotation": "A note"}, headers=self.AUTH)
        resp = client.get(f"/api/posts/{pid}/annotation", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["human_annotation"] == "A note"
        assert resp.json()["post_id"] == pid

    def test_get_annotation_none(self, client: TestClient):
        pid = self._create(client)
        resp = client.get(f"/api/posts/{pid}/annotation", headers=self.AUTH)
        assert resp.json()["human_annotation"] is None

    def test_annotation_on_nonexistent_post(self, client: TestClient):
        resp = client.put("/api/posts/badid/annotation", json={"annotation": "note"}, headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_get_annotation_nonexistent_post(self, client: TestClient):
        resp = client.get("/api/posts/badid/annotation", headers=self.AUTH)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_annotation_overwrite(self, client: TestClient):
        pid = self._create(client)
        client.put(f"/api/posts/{pid}/annotation", json={"annotation": "First"}, headers=self.AUTH)
        client.put(f"/api/posts/{pid}/annotation", json={"annotation": "Second"}, headers=self.AUTH)
        resp = client.get(f"/api/posts/{pid}/annotation", headers=self.AUTH)
        assert resp.json()["human_annotation"] == "Second"

    def test_bot_reads_post_with_human_note(self, client: TestClient):
        """The full bot reading experience: a bot creates, human annotates, bot reads back."""
        # 1. Bot creates a memory
        resp = client.post(
            "/api/posts",
            json={
                "title": "API Keys",
                "summary": "How to configure API keys for deployment",
                "tags": ["config", "deployment"],
                "body": "Set API_KEY env var in production. Use a secrets manager.",
                "identity": "deploy_bot",
            },
            headers=self.AUTH,
        )
        pid = resp.json()["id"]

        # 2. Human adds a note
        client.put(
            f"/api/posts/{pid}/annotation",
            json={"annotation": "The production key is in 1Password under 'App Secrets'"},
            headers=self.AUTH,
        )

        # 3. Bot reads the post back and sees the annotation
        resp = client.get(f"/api/posts/{pid}", headers=self.AUTH)
        post = resp.json()
        body = post["body"]
        note = post["human_annotation"]

        # The bot would render it like:
        full = f"{body}\n\nHuman note: {note}"
        assert "Set API_KEY env var" in full
        assert "Human note: The production key is in 1Password" in full


# ======================== Search Tests ========================


class TestSearch:
    """GET /api/search — all three search modes."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def _seed(self, client: TestClient):
        for p in SAMPLE_POSTS:
            client.post("/api/posts", json=p, headers=self.AUTH)

    def test_search_lexical(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/search?q=python&mode=lexical", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["total"] >= 1
        assert data["mode"] == "lexical"
        assert data["query"] == "python"
        titles = [r["post"]["title"] for r in data["results"]]
        assert "Python Tips" in titles

    def test_search_semantic(self, client_with_embed: TestClient):
        self._seed(client_with_embed)
        resp = client_with_embed.get("/api/search?q=machine+learning&mode=semantic", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["total"] >= 1
        assert data["mode"] == "semantic"

    def test_search_hybrid_default(self, client_with_embed: TestClient):
        self._seed(client_with_embed)
        resp = client_with_embed.get("/api/search?q=deep+learning", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["mode"] == "hybrid"
        assert data["total"] >= 1

    def test_search_with_identity_filter(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/search?q=learning&mode=lexical&identity=pengy_bot", headers=self.AUTH)
        data = resp.json()
        for r in data["results"]:
            assert r["post"]["identity"] == "pengy_bot"

    def test_search_with_tags_filter(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/search?q=learning&mode=lexical&tags=ml", headers=self.AUTH)
        data = resp.json()
        for r in data["results"]:
            assert "ml" in r["post"]["tags"]

    def test_search_no_match(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/search?q=zzzzznotfound&mode=lexical", headers=self.AUTH)
        assert resp.json()["total"] == 0

    def test_search_results_have_rank(self, client: TestClient):
        self._seed(client)
        resp = client.get("/api/search?q=python&mode=lexical", headers=self.AUTH)
        for r in resp.json()["results"]:
            assert r["rank"] >= 1
            assert r["score"] is not None

    def test_search_requires_query(self, client: TestClient):
        resp = client.get("/api/search", headers=self.AUTH)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_search_tags_only_browse(self, client: TestClient):
        """No q + tags → paginated browse of every matching post, newest first."""
        self._seed(client)
        resp = client.get("/api/search?tags=ai", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["mode"] == "tags"
        assert data["total"] == 2
        titles = {r["post"]["title"] for r in data["results"]}
        assert titles == {"Machine Learning Basics", "Neural Networks"}

    def test_search_tags_only_paging(self, client: TestClient):
        self._seed(client)
        r1 = client.get("/api/search?tags=ai&limit=1", headers=self.AUTH)
        r2 = client.get("/api/search?tags=ai&limit=1&skip=1", headers=self.AUTH)
        d1, d2 = r1.json(), r2.json()
        assert d1["total"] == 2 and len(d1["results"]) == 1
        assert d2["total"] == 2 and len(d2["results"]) == 1
        assert d1["results"][0]["post"]["id"] != d2["results"][0]["post"]["id"]

    def test_search_tag_mode_all(self, client: TestClient):
        """tag_mode=all restricts relevance search to posts with every tag."""
        self._seed(client)
        resp = client.get(
            "/api/search?q=learning&mode=lexical&tags=ai,ml&tag_mode=all",
            headers=self.AUTH,
        )
        assert resp.status_code == status.HTTP_200_OK
        for r in resp.json()["results"]:
            assert "ai" in r["post"]["tags"]
            assert "ml" in r["post"]["tags"]

    def test_search_invalid_mode(self, client: TestClient):
        resp = client.get("/api/search?q=test&mode=invalid", headers=self.AUTH)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# ======================== Stats & Health Tests ========================


class TestStats:
    """GET /api/stats."""

    AUTH = {"Authorization": "Bearer test-api-key-12345"}

    def test_stats_empty(self, client: TestClient):
        resp = client.get("/api/stats", headers=self.AUTH)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["documents"] == 0
        assert data["database_size_bytes"] >= 0

    def test_stats_with_data(self, client: TestClient):
        client.post("/api/posts", json=SAMPLE_POSTS[0], headers=self.AUTH)
        resp = client.get("/api/stats", headers=self.AUTH)
        assert resp.json()["documents"] == 1


class TestHealth:
    """GET /api/health."""

    def test_health(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "BotTalk"
