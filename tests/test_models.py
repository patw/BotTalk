"""
Tests for BotTalk Pydantic models — validation, serialization, edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from bot_talk.models import (
    HumanAnnotationUpdate,
    PostCreate,
    PostDocument,
    PostResponse,
    PostSearchResponse,
    PostSearchResult,
    PostUpdate,
    UpdateRecord,
    doc_to_response,
)


class TestPostCreate:
    """Validation of the PostCreate request model."""

    def test_valid_post(self):
        """A well-formed post passes validation."""
        p = PostCreate(
            title="Test Post",
            summary="A brief summary",
            tags=["test", "example"],
            body="Hello, world!",
            identity="test_bot",
        )
        assert p.title == "Test Post"
        assert p.summary == "A brief summary"
        assert p.tags == ["test", "example"]
        assert p.body == "Hello, world!"
        assert p.identity == "test_bot"

    def test_minimal_post(self):
        """Empty tags list is allowed."""
        p = PostCreate(
            title="Minimal",
            summary="Just a test",
            tags=[],
            body="Body",
            identity="bot",
        )
        assert p.tags == []

    def test_missing_required_fields(self):
        """Omitting required fields raises ValidationError."""
        with pytest.raises(ValidationError):
            PostCreate()

    def test_empty_title_rejected(self):
        """Title must be at least 1 character."""
        with pytest.raises(ValidationError):
            PostCreate(title="", summary="S", tags=[], body="B", identity="bot")

    def test_empty_summary_rejected(self):
        """Summary must be at least 1 character."""
        with pytest.raises(ValidationError):
            PostCreate(title="T", summary="", tags=[], body="B", identity="bot")

    def test_empty_identity_rejected(self):
        """Identity must be at least 1 character."""
        with pytest.raises(ValidationError):
            PostCreate(title="T", summary="S", tags=[], body="B", identity="")

    def test_body_4kb_limit_bytes(self):
        """Body is limited to 4 KB (UTF-8 encoded)."""
        # 4095 ASCII bytes = 4095 chars → should be OK
        ok_body = "x" * 4095
        p = PostCreate(title="T", summary="S", tags=[], body=ok_body, identity="bot")
        assert len(p.body) == 4095

        # 4097 ASCII bytes → rejected by max_length=4096 first
        with pytest.raises(ValidationError):
            PostCreate(title="T", summary="S", tags=[], body="x" * 4097, identity="bot")

    def test_body_4kb_limit_unicode(self):
        """The 4 KB limit is on UTF-8 encoded bytes, not characters."""
        # Each '✓' is 3 bytes in UTF-8 → 1365 chars = 4095 bytes (OK)
        ok_unicode = "✓" * 1365
        p = PostCreate(title="T", summary="S", tags=[], body=ok_unicode, identity="bot")
        assert len(p.body.encode("utf-8")) <= 4096

        # 1366 chars = 4098 bytes → rejected
        with pytest.raises(ValidationError, match="4 KB"):
            PostCreate(title="T", summary="S", tags=[], body="✓" * 1366, identity="bot")

    def test_title_max_length(self):
        """Title is capped at 200 characters."""
        long_title = "a" * 201
        with pytest.raises(ValidationError):
            PostCreate(title=long_title, summary="S", tags=[], body="B", identity="bot")

    def test_summary_max_length(self):
        """Summary is capped at 1000 characters."""
        long_summary = "a" * 1001
        with pytest.raises(ValidationError):
            PostCreate(title="T", summary=long_summary, tags=[], body="B", identity="bot")

    def test_tag_max_length(self):
        """Individual tags are capped at 50 characters."""
        with pytest.raises(ValidationError, match="tag too long"):
            PostCreate(
                title="T", summary="S", tags=["x" * 51], body="B", identity="bot"
            )


class TestPostUpdate:
    """Validation of the PostUpdate request model."""

    def test_valid_update(self):
        """A partial update with only one field is valid."""
        u = PostUpdate(identity="updater_bot", title="New Title")
        assert u.title == "New Title"
        assert u.summary is None
        assert u.tags is None
        assert u.body is None
        assert u.human_annotation is None

    def test_full_update(self):
        """All fields can be provided at once."""
        u = PostUpdate(
            identity="updater_bot",
            title="New Title",
            summary="New summary",
            tags=["new-tag"],
            body="New body content",
            human_annotation="A note",
        )
        assert u.title == "New Title"
        assert u.human_annotation == "A note"

    def test_identity_required(self):
        """Identity is always required on updates."""
        with pytest.raises(ValidationError):
            PostUpdate()

    def test_body_still_limited(self):
        """Body limit still applies on update."""
        with pytest.raises(ValidationError):
            PostUpdate(identity="bot", body="x" * 4097)


class TestHumanAnnotationUpdate:
    """Validation of the human annotation model."""

    def test_valid_annotation(self):
        h = HumanAnnotationUpdate(annotation="A human note")
        assert h.annotation == "A human note"

    def test_annotation_required(self):
        with pytest.raises(ValidationError):
            HumanAnnotationUpdate()

    def test_annotation_max_length(self):
        with pytest.raises(ValidationError):
            HumanAnnotationUpdate(annotation="x" * 4097)


class TestUpdateRecord:
    """The append-only update history record."""

    def test_valid_record(self):
        r = UpdateRecord(identity="bot_a", timestamp=datetime.now(timezone.utc), changes="title")
        assert r.identity == "bot_a"
        assert r.changes == "title"

    def test_default_timestamp(self):
        r = UpdateRecord(identity="bot", changes="body")
        assert r.timestamp is not None
        assert r.timestamp.tzinfo is not None


class TestPostResponse:
    """Serialization of the PostResponse model."""

    def test_from_doc(self):
        """doc_to_response converts a moofile dict to a PostResponse."""
        doc = {
            "_id": "abc123def456",
            "title": "Hello",
            "summary": "Summary text",
            "tags": ["a", "b"],
            "body": "Body text here",
            "identity": "test_bot",
            "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
        }
        resp = doc_to_response(doc)
        assert resp.id == "abc123def456"
        assert resp.title == "Hello"
        assert resp.tags == ["a", "b"]
        assert resp.updated_at is None
        assert resp.human_annotation is None

    def test_from_doc_with_history(self):
        """update_history converts to UpdateRecord objects."""
        ts = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        doc = {
            "_id": "x1",
            "title": "T",
            "summary": "S",
            "tags": [],
            "body": "B",
            "identity": "bot",
            "created_at": ts,
            "updated_at": ts,
            "update_history": [
                {"identity": "bot2", "timestamp": ts, "changes": "title, body"}
            ],
            "human_annotation": "human says hi",
        }
        resp = doc_to_response(doc)
        assert len(resp.update_history) == 1
        assert resp.update_history[0].identity == "bot2"
        assert resp.update_history[0].changes == "title, body"
        assert resp.human_annotation == "human says hi"

    def test_serialization_includes_id(self):
        """JSON serialization uses 'id' (not '_id') as the key."""
        doc = {
            "_id": "abc",
            "title": "T",
            "summary": "S",
            "tags": [],
            "body": "B",
            "identity": "bot",
            "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
        }
        resp = doc_to_response(doc)
        dumped = resp.model_dump()
        assert "id" in dumped
        assert "_id" not in dumped
        assert dumped["id"] == "abc"

    def test_strips_embedding_field(self):
        """The raw summary_embedding field is not in the response."""
        doc = {
            "_id": "abc",
            "title": "T",
            "summary": "S",
            "tags": [],
            "body": "B",
            "identity": "bot",
            "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
            "summary_embedding": [0.1, 0.2, 0.3],
        }
        resp = doc_to_response(doc)
        dumped = resp.model_dump()
        assert "summary_embedding" not in dumped


class TestPostSearchResult:
    """Search result model."""

    def test_valid_result(self):
        doc = {
            "_id": "abc",
            "title": "T",
            "summary": "S",
            "tags": [],
            "body": "B",
            "identity": "bot",
            "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
        }
        result = PostSearchResult(post=doc_to_response(doc), score=0.95, rank=1)
        assert result.score == 0.95
        assert result.rank == 1
        assert result.post.id == "abc"


class TestPostDocument:
    """The internal PostDocument schema (used for type clarity, not stored directly)."""

    def test_valid_document(self):
        doc = PostDocument(
            title="Test",
            summary="Summary",
            tags=["ml"],
            body="Body",
            identity="bot",
        )
        assert doc.title == "Test"
        assert doc.human_annotation is None

    def test_with_annotation(self):
        doc = PostDocument(
            title="T",
            summary="S",
            tags=[],
            body="B",
            identity="bot",
            human_annotation="A human note",
        )
        assert doc.human_annotation == "A human note"
