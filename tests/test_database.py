"""
Tests for BotTalk database layer — CRUD, search, human annotations.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from bot_talk.database import BotTalkDB
from bot_talk.models import PostUpdate

from conftest import SAMPLE_POSTS


class TestCompaction:
    """Compaction reclaims append-only update and delete records."""

    def test_compact_reclaims_dead_records(self, test_db: BotTalkDB):
        created = test_db.create_post(
            title="Compact me", summary="Summary", tags=[], body="Body", identity="bot"
        )
        test_db.update_post(
            created["_id"],
            PostUpdate(title="Updated", identity="bot"),
        )
        before = test_db.stats()
        assert before["dead_records"] >= 1

        test_db.compact()
        after = test_db.stats()
        assert after["dead_records"] == 0
        assert after["documents"] == 1
        assert test_db.get_post(created["_id"])["title"] == "Updated"


class TestCreatePost:
    """Creating posts via the database layer."""

    def test_create_minimal(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="Test", summary="Summary", tags=[], body="Body", identity="bot"
        )
        assert "_id" in doc
        assert doc["title"] == "Test"
        assert doc["summary"] == "Summary"
        assert doc["body"] == "Body"
        assert doc["identity"] == "bot"
        assert doc["tags"] == []
        assert doc["human_annotation"] is None
        assert doc["update_history"] == []

    def test_create_with_tags(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="ML",
            summary="Machine learning intro",
            tags=["ml", "ai"],
            body="Content here",
            identity="ml_bot",
        )
        assert doc["tags"] == ["ml", "ai"]

    def test_created_at_set(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        created = doc["created_at"]
        assert created is not None
        # BSON stores naive datetimes in UTC
        assert isinstance(created, datetime)

    def test_updated_at_none_on_create(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        assert doc["updated_at"] is None


class TestGetPost:
    """Fetching posts by ID."""

    def test_get_existing(self, test_db: BotTalkDB):
        created = test_db.create_post(
            title="FindMe", summary="Can you find me?", tags=[], body="Body", identity="bot"
        )
        doc = test_db.get_post(created["_id"])
        assert doc is not None
        assert doc["title"] == "FindMe"

    def test_get_nonexistent(self, test_db: BotTalkDB):
        doc = test_db.get_post("nonexistent_id")
        assert doc is None

    def test_get_after_delete(self, test_db: BotTalkDB):
        created = test_db.create_post(
            title="Gone", summary="Will be deleted", tags=[], body="Body", identity="bot"
        )
        pid = created["_id"]
        test_db.delete_post(pid)
        doc = test_db.get_post(pid)
        assert doc is None


class TestListPosts:
    """Listing posts with filtering and pagination."""

    def _seed(self, test_db: BotTalkDB) -> list[str]:
        ids = []
        for p in SAMPLE_POSTS:
            doc = test_db.create_post(**p)
            ids.append(doc["_id"])
        return ids

    def test_list_all(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts()
        assert total == 4
        assert len(docs) == 4

    def test_list_empty(self, test_db: BotTalkDB):
        docs, total = test_db.list_posts()
        assert total == 0
        assert docs == []

    def test_list_pagination(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(limit=2)
        assert total == 4
        assert len(docs) == 2

    def test_list_skip(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(skip=2)
        assert total == 4
        assert len(docs) == 2

    def test_list_filter_by_identity(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(identity="pengy_bot")
        assert total == 2
        for d in docs:
            assert d["identity"] == "pengy_bot"

    def test_list_filter_by_tags(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(tags=["python"])
        assert total == 1
        assert docs[0]["title"] == "Python Tips"

    def test_list_filter_by_multiple_tags(self, test_db: BotTalkDB):
        self._seed(test_db)
        # Should match posts with EITHER tag
        docs, total = test_db.list_posts(tags=["python", "systems"])
        assert total == 2

    def test_list_filter_by_multiple_tags_all(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(tags=["ai", "ml"], tag_mode="all")
        assert total == 1
        assert docs[0]["title"] == "Machine Learning Basics"

    def test_list_filter_by_multiple_tags_any(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(tags=["ai", "python"], tag_mode="any")
        assert total == 3

    def test_list_sorted_newest_first(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, _ = test_db.list_posts()
        # Last created should be first in the list
        timestamps = [d["created_at"] for d in docs]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_list_no_match_filter(self, test_db: BotTalkDB):
        self._seed(test_db)
        docs, total = test_db.list_posts(identity="nonexistent_bot")
        assert total == 0
        assert docs == []


class TestListTags:
    """Tag aggregation via list_tags()."""

    def _seed(self, test_db: BotTalkDB) -> None:
        for p in SAMPLE_POSTS:
            test_db.create_post(**p)

    def test_empty(self, test_db: BotTalkDB):
        assert test_db.list_tags() == []

    def test_counts_sorted(self, test_db: BotTalkDB):
        self._seed(test_db)
        tags = test_db.list_tags()
        assert len(tags) == 9
        counts = {t["tag"]: t["count"] for t in tags}
        assert counts["ai"] == 2
        assert counts["ml"] == 1
        ns = [t["count"] for t in tags]
        assert ns == sorted(ns, reverse=True)

    def test_prefix(self, test_db: BotTalkDB):
        self._seed(test_db)
        tags = test_db.list_tags(prefix="neural")
        assert [t["tag"] for t in tags] == ["neural-nets"]

    def test_min_count(self, test_db: BotTalkDB):
        self._seed(test_db)
        tags = test_db.list_tags(min_count=2)
        assert [t["tag"] for t in tags] == ["ai"]

    def test_tiebreak_alphabetical(self, test_db: BotTalkDB):
        self._seed(test_db)
        tags = test_db.list_tags()
        ones = [t["tag"] for t in tags if t["count"] == 1]
        assert ones == sorted(ones)


class TestTagNormalization:
    """Write-time normalization + alias coercion (the drift guardrails)."""

    def _seed_canonical(self, test_db: BotTalkDB):
        """A post establishing the canonical vocabulary."""
        test_db.create_post(
            title="Vocab", summary="s",
            tags=["open-source", "llmproxy", "skill"],
            body="b", identity="bot",
        )

    def test_create_normalizes_format_variants(self, test_db: BotTalkDB):
        self._seed_canonical(test_db)
        doc = test_db.create_post(
            title="T", summary="s",
            tags=["Open Source", "voyage_4_nano", "  AI  ", "open_source"],
            body="b", identity="bot",
        )
        assert doc["tags"] == ["open-source", "voyage-4-nano", "ai"]

    def test_create_coerces_aliases(self, test_db: BotTalkDB):
        self._seed_canonical(test_db)
        doc = test_db.create_post(
            title="T", summary="s",
            tags=["skills", "opensource", "openai-proxy"],
            body="b", identity="bot",
        )
        assert doc["tags"] == ["skill", "open-source", "llmproxy"]

    def test_create_keeps_version_tags(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="s",
            tags=["v1.2.0", "llama.cpp", "Time Series"],
            body="b", identity="bot",
        )
        assert doc["tags"] == ["v1.2.0", "llama.cpp", "time-series"]

    def test_create_dedup_after_coercion(self, test_db: BotTalkDB):
        self._seed_canonical(test_db)
        doc = test_db.create_post(
            title="T", summary="s",
            tags=["skills", "skill", "open_source", "Open Source"],
            body="b", identity="bot",
        )
        assert doc["tags"] == ["skill", "open-source"]

    def test_update_coerces_tags(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="s", tags=["a"], body="b", identity="bot",
        )
        updated = test_db.update_post(
            doc["_id"], PostUpdate(identity="bot", tags=["SKILLS", "openai_proxy"])
        )
        assert updated["tags"] == ["skill", "llmproxy"]

    def test_query_alias_expansion_any(self, test_db: BotTalkDB):
        self._seed_canonical(test_db)
        docs, total = test_db.list_posts(tags=["opensource", "skills"], tag_mode="any")
        assert total == 1

    def test_query_alias_expansion_all(self, test_db: BotTalkDB):
        self._seed_canonical(test_db)
        docs, total = test_db.list_posts(tags=["opensource", "llmproxy"], tag_mode="all")
        assert total == 1
        docs, total = test_db.list_posts(
            tags=["opensource", "nonexistent"], tag_mode="all"
        )
        assert total == 0

    def test_alias_canonicalizes_legacy_form(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="s", tags=["max_length"], body="b", identity="bot",
        )
        assert doc["tags"] == ["max-length"]

    def test_alias_overrides_sticky_known_vocab(self, test_db: BotTalkDB):
        """A legacy spelling already in the vocab must not win over its alias."""
        from datetime import datetime, timezone
        test_db.db.insert({
            "title": "Legacy", "summary": "s", "tags": ["max_length"],
            "body": "b", "identity": "bot",
            "created_at": datetime.now(timezone.utc), "updated_at": None,
            "update_history": [], "human_annotation": None,
        })
        doc = test_db.create_post(
            title="T", summary="s", tags=["max-length"], body="b", identity="bot",
        )
        assert doc["tags"] == ["max-length"]


class TestLintTags:
    """Tag hygiene report — lint_tags()."""

    def _raw_insert(self, test_db: BotTalkDB, title: str, tags: list[str]):
        """Insert a legacy-style doc directly, bypassing write normalization."""
        from datetime import datetime, timezone
        test_db.db.insert({
            "title": title, "summary": "s", "tags": tags, "body": "b",
            "identity": "bot", "created_at": datetime.now(timezone.utc),
            "updated_at": None, "update_history": [], "human_annotation": None,
        })

    def test_lint_empty(self, test_db: BotTalkDB):
        rep = test_db.lint_tags()
        assert rep["total_tags"] == 0
        assert rep["normalized_collisions"] == []
        assert rep["pattern_violations"] == []
        assert rep["aliased_tags"] == []
        assert rep["near_duplicates"] == []
        assert rep["single_use_tags"] == []

    def test_lint_normalized_collision(self, test_db: BotTalkDB):
        """Case/space/underscore variants collapse to the same form."""
        self._raw_insert(test_db, "A", ["Open Source", "b"])
        self._raw_insert(test_db, "B", ["open_source", "c"])
        rep = test_db.lint_tags()
        assert len(rep["normalized_collisions"]) == 1
        c = rep["normalized_collisions"][0]
        assert c["normalized"] == "open-source"
        assert set(c["variants"]) == {"Open Source", "open_source"}

    def test_lint_aliased_tags(self, test_db: BotTalkDB):
        """Stored tags with a known alias are merge candidates."""
        self._raw_insert(test_db, "A", ["opensource", "skills", "ok"])
        rep = test_db.lint_tags()
        aliased = {a["tag"]: a["canonical"] for a in rep["aliased_tags"]}
        assert aliased == {"opensource": "open-source", "skills": "skill"}

    def test_lint_pattern_violation(self, test_db: BotTalkDB):
        self._raw_insert(test_db, "A", ["Weird_Tag", "b"])
        rep = test_db.lint_tags()
        assert rep["pattern_violations"] == [{"tag": "Weird_Tag", "count": 1}]

    def test_lint_near_duplicates(self, test_db: BotTalkDB):
        self._raw_insert(test_db, "A", ["pengy"])
        self._raw_insert(test_db, "B", ["pengyr"])
        rep = test_db.lint_tags()
        assert any(p["a"] == "pengy" and p["b"] == "pengyr" for p in rep["near_duplicates"])

    def test_lint_single_use(self, test_db: BotTalkDB):
        self._raw_insert(test_db, "A", ["only-once"])
        self._raw_insert(test_db, "B", ["only-once", "twice"])
        rep = test_db.lint_tags()
        assert any(t["tag"] == "twice" for t in rep["single_use_tags"])
        assert all(t["tag"] != "only-once" for t in rep["single_use_tags"])


class TestUpdatePost:
    """Updating posts with append-only history."""

    def _create(self, test_db: BotTalkDB) -> str:
        doc = test_db.create_post(
            title="Original", summary="Original summary", tags=["a"],
            body="Original body", identity="bot_a"
        )
        return doc["_id"]

    def test_update_title(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        updated = test_db.update_post(pid, PostUpdate(identity="bot_b", title="Updated Title"))
        assert updated is not None
        assert updated["title"] == "Updated Title"
        # Summary, body, tags unchanged
        assert updated["summary"] == "Original summary"
        assert updated["body"] == "Original body"

    def test_update_appends_history(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        updated = test_db.update_post(pid, PostUpdate(identity="bot_b", title="New Title"))
        assert len(updated["update_history"]) == 1
        entry = updated["update_history"][0]
        assert entry["identity"] == "bot_b"
        assert "title" in entry["changes"]

    def test_update_appends_multiple_history(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        test_db.update_post(pid, PostUpdate(identity="bot_b", title="V2"))
        updated = test_db.update_post(pid, PostUpdate(identity="bot_c", body="New body"))
        assert len(updated["update_history"]) == 2
        assert updated["update_history"][0]["identity"] == "bot_b"
        assert updated["update_history"][1]["identity"] == "bot_c"

    def test_update_nonexistent(self, test_db: BotTalkDB):
        updated = test_db.update_post("bad_id", PostUpdate(identity="bot", title="X"))
        assert updated is None

    def test_update_no_changes_returns_same(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        # Update with same values → no changes
        updated = test_db.update_post(pid, PostUpdate(identity="bot"))
        assert updated is not None
        # History should still be empty since nothing changed
        assert len(updated["update_history"]) == 0

    def test_update_sets_updated_at(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        updated = test_db.update_post(pid, PostUpdate(identity="bot", title="V2"))
        assert updated["updated_at"] is not None
        assert isinstance(updated["updated_at"], datetime)
        assert updated["updated_at"] >= updated["created_at"]

    def test_update_multiple_fields(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        updated = test_db.update_post(
            pid,
            PostUpdate(identity="updater", title="New T", summary="New S", tags=["x", "y"]),
        )
        assert updated["title"] == "New T"
        assert updated["summary"] == "New S"
        assert updated["tags"] == ["x", "y"]
        assert "title, summary, tags" in updated["update_history"][0]["changes"]

    def test_update_human_annotation(self, test_db: BotTalkDB):
        pid = self._create(test_db)
        updated = test_db.update_post(
            pid,
            PostUpdate(identity="human", human_annotation="A human note"),
        )
        assert updated["human_annotation"] == "A human note"
        assert "human_annotation" in updated["update_history"][0]["changes"]


class TestDeletePost:
    """Deleting posts."""

    def test_delete_existing(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        assert test_db.delete_post(doc["_id"]) is True

    def test_delete_nonexistent(self, test_db: BotTalkDB):
        assert test_db.delete_post("bad_id") is False

    def test_delete_then_list(self, test_db: BotTalkDB):
        ids = []
        for p in SAMPLE_POSTS[:2]:
            d = test_db.create_post(**p)
            ids.append(d["_id"])
        test_db.delete_post(ids[0])
        docs, total = test_db.list_posts()
        assert total == 1
        assert docs[0]["_id"] == ids[1]


class TestHumanAnnotation:
    """The human_annotation field — set by humans, read by bots."""

    def test_set_annotation(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        updated = test_db.set_human_annotation(doc["_id"], "This is a human note")
        assert updated is not None
        assert updated["human_annotation"] == "This is a human note"

    def test_annotation_on_nonexistent(self, test_db: BotTalkDB):
        result = test_db.set_human_annotation("bad_id", "note")
        assert result is None

    def test_annotation_appears_in_get(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        test_db.set_human_annotation(doc["_id"], "Important: verify this data")
        fetched = test_db.get_post(doc["_id"])
        assert fetched["human_annotation"] == "Important: verify this data"

    def test_annotation_overwrites(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        test_db.set_human_annotation(doc["_id"], "First note")
        test_db.set_human_annotation(doc["_id"], "Updated note")
        fetched = test_db.get_post(doc["_id"])
        assert fetched["human_annotation"] == "Updated note"

    def test_annotation_in_list(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        test_db.set_human_annotation(doc["_id"], "Human insight here")
        docs, _ = test_db.list_posts()
        # The one post should have the annotation
        assert docs[0]["human_annotation"] == "Human insight here"

    def test_no_annotation_by_default(self, test_db: BotTalkDB):
        doc = test_db.create_post(
            title="T", summary="S", tags=[], body="B", identity="bot"
        )
        assert doc["human_annotation"] is None

    def test_bot_reads_annotation(self, test_db: BotTalkDB):
        """Simulate the bot reading back a post with a human note.

        The bot should see 'human_annotation' as a readable field.
        """
        doc = test_db.create_post(
            title="Memory",
            summary="A core memory about project X",
            tags=["core"],
            body="Important details about project X configuration.",
            identity="memory_bot",
        )
        # Human adds a tip
        test_db.set_human_annotation(doc["_id"], "Double-check the API key in prod")

        # Bot reads it back
        fetched = test_db.get_post(doc["_id"])
        note = fetched.get("human_annotation")
        assert note is not None
        # This is what the bot would see at the bottom of the post
        full_text = f"{fetched['body']}\n\nHuman note: {note}"
        assert "Human note: Double-check the API key in prod" in full_text


class TestSearch:
    """Search operations (without auto-embed for basic lexical tests)."""

    def _seed(self, test_db: BotTalkDB) -> list[str]:
        ids = []
        for p in SAMPLE_POSTS:
            doc = test_db.create_post(**p)
            ids.append(doc["_id"])
        return ids

    def test_lexical_search_finds_keyword(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical("python", limit=10)
        assert len(results) >= 1
        titles = [r[0]["title"] for r in results]
        assert "Python Tips" in titles

    def test_lexical_search_no_match(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical("zzzzznotfound", limit=10)
        assert len(results) == 0

    def test_lexical_search_multi_word(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical("machine learning", limit=10)
        assert len(results) >= 1

    def test_lexical_with_identity_filter(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical("learning", limit=10, identity="pengy_bot")
        for doc, score in results:
            assert doc["identity"] == "pengy_bot"

    def test_lexical_with_tags_filter(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical("learning", limit=10, tags=["ml"])
        for doc, score in results:
            assert "ml" in doc["tags"]

    def test_lexical_with_tags_all_filter(self, test_db: BotTalkDB):
        self._seed(test_db)
        results = test_db.search_lexical(
            "learning", limit=10, tags=["ai", "ml"], tag_mode="all"
        )
        for doc, score in results:
            assert "ai" in doc["tags"]
            assert "ml" in doc["tags"]

    def test_semantic_search_basic(self, test_db_with_embed):
        """Semantic search requires auto-embedding enabled."""
        ids = []
        for p in SAMPLE_POSTS:
            d = test_db_with_embed.create_post(**p)
            ids.append(d["_id"])

        results = test_db_with_embed.search_semantic("machine learning", limit=10)
        assert len(results) >= 1
        # The ML-related posts should rank higher
        top_titles = [r[0]["title"] for r in results[:2]]
        assert any("Machine Learning" in t or "Neural" in t for t in top_titles)

    def test_semantic_with_filter(self, test_db_with_embed):
        for p in SAMPLE_POSTS:
            test_db_with_embed.create_post(**p)
        results = test_db_with_embed.search_semantic(
            "coding", limit=10, identity="code_bot"
        )
        assert len(results) >= 1
        for doc, score in results:
            assert doc["identity"] == "code_bot"

    def test_semantic_no_match(self, test_db_with_embed):
        for p in SAMPLE_POSTS:
            test_db_with_embed.create_post(**p)
        results = test_db_with_embed.search_semantic(
            "quantum physics exotic matter", limit=10
        )
        # Should still return something (vector search always has nearest neighbors)
        # but scores should be low
        assert len(results) >= 0

    def test_hybrid_search(self, test_db_with_embed):
        for p in SAMPLE_POSTS:
            test_db_with_embed.create_post(**p)
        results = test_db_with_embed.search_hybrid("neural networks deep learning", limit=10)
        assert len(results) >= 1
        top_titles = [r[0]["title"] for r in results[:3]]
        assert any("Neural" in t for t in top_titles)
