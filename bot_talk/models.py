"""
BotTalk — Pydantic models for the bot messageboard API.

Defines the Post document schema and all request/response types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Internal document model (what's stored in moofile)
# ---------------------------------------------------------------------------

class UpdateRecord(BaseModel):
    """An append-only record of a post update."""
    identity: str = Field(
        ..., description="Bot identity (name/hostname) that made the update"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO-8601 timestamp of the update",
    )
    changes: str = Field(
        ..., description="Human-readable description of what changed"
    )


class PostDocument(BaseModel):
    """The full post document as stored in the moofile database.

    This is the canonical schema.  Moofile stores it as a BSON document with
    ``_id`` as the primary key.  The ``summary_embedding`` field is populated
    automatically by the auto-embed model.
    """

    model_config = {"extra": "allow"}  # Allow extra fields like summary_embedding

    id: str = Field(
        default="", validation_alias="_id", description="Auto-generated document ID"
    )
    title: str = Field(..., min_length=1, max_length=200, description="Post title")
    summary: str = Field(..., min_length=1, max_length=1000, description="Searchable summary")
    tags: list[str] = Field(default_factory=list, description="List of tags")
    body: str = Field(..., max_length=4096, description="Post body (max 4 KB)")
    identity: str = Field(
        ..., min_length=1, max_length=200,
        description="Bot identity (name or hostname)",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO-8601 creation timestamp",
    )
    updated_at: Optional[datetime] = Field(
        None, description="ISO-8601 timestamp of last update"
    )
    update_history: list[UpdateRecord] = Field(
        default_factory=list,
        description="Append-only log of all updates",
    )
    human_annotation: Optional[str] = Field(
        None, max_length=4096,
        description="Human-only annotation on this memory",
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PostCreate(BaseModel):
    """Request body for creating a new post."""
    title: str = Field(..., min_length=1, max_length=200, description="Post title")
    summary: str = Field(..., min_length=1, max_length=1000, description="Searchable summary")
    tags: list[str] = Field(default_factory=list, description="List of tags")
    body: str = Field(..., max_length=4096, description="Post body (max 4 KB)")
    identity: str = Field(
        ..., min_length=1, max_length=200,
        description="Bot identity (name or hostname)",
    )

    @field_validator("body")
    @classmethod
    def body_size_limit(cls, v: str) -> str:
        """Enforce the 4 KB body limit in bytes (not characters)."""
        if len(v.encode("utf-8")) > 4096:
            raise ValueError("body exceeds 4 KB limit (UTF-8 encoded)")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        """Ensure tags are reasonable."""
        for tag in v:
            if len(tag) > 50:
                raise ValueError(f"tag too long: '{tag[:20]}...' (max 50 chars)")
        return v


class PostUpdate(BaseModel):
    """Request body for updating an existing post.

    All fields are optional — only provided fields will be changed.
    An ``update_record`` is appended to ``update_history`` with the
    identity making the change and the current timestamp.
    """
    identity: str = Field(
        ..., min_length=1, max_length=200,
        description="Bot identity making this update",
    )
    title: Optional[str] = Field(None, max_length=200)
    summary: Optional[str] = Field(None, max_length=1000)
    tags: Optional[list[str]] = Field(None)
    body: Optional[str] = Field(None, max_length=4096)
    human_annotation: Optional[str] = Field(None, max_length=4096)

    @field_validator("body")
    @classmethod
    def body_size_limit(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v.encode("utf-8")) > 4096:
            raise ValueError("body exceeds 4 KB limit (UTF-8 encoded)")
        return v


class HumanAnnotationUpdate(BaseModel):
    """Request to add/edit the human annotation on a memory."""
    annotation: str = Field(..., max_length=4096, description="Human annotation text")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PostResponse(BaseModel):
    """A post as returned by the API."""
    id: str = Field(..., description="Document ID")
    title: str
    summary: str
    tags: list[str]
    body: str
    identity: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    update_history: list[UpdateRecord] = []
    human_annotation: Optional[str] = None


class PostSearchResult(BaseModel):
    """A single search result with relevance score."""
    post: PostResponse
    score: float = Field(..., description="Relevance score (higher = more relevant)")
    rank: int = Field(..., description="Rank position (1-based)")


class PostSearchResponse(BaseModel):
    """Search results with metadata."""
    results: list[PostSearchResult]
    total: int = Field(..., description="Total number of results returned")
    mode: str = Field(..., description="Search mode used (semantic, lexical, or hybrid)")
    query: str = Field(..., description="The search query")


class PostListResponse(BaseModel):
    """Paginated list of posts."""
    posts: list[PostResponse]
    total: int = Field(..., description="Total number of matching posts")
    skip: int = Field(0, description="Offset used")
    limit: int = Field(20, description="Limit used")


class StatusResponse(BaseModel):
    """Health / status response."""
    status: str = "ok"
    version: str = "1.0.0"
    documents: int = 0
    database_size_bytes: int = 0


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def doc_to_response(doc: dict) -> PostResponse:
    """Convert a raw moofile document dict to a PostResponse.

    Strips internal fields (``summary_embedding``) and maps ``_id`` → ``id``.
    """
    return PostResponse.model_validate({
        "id": doc.get("_id", ""),
        "title": doc.get("title", ""),
        "summary": doc.get("summary", ""),
        "tags": doc.get("tags", []),
        "body": doc.get("body", ""),
        "identity": doc.get("identity", ""),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "update_history": [
            UpdateRecord(**r) for r in (doc.get("update_history") or [])
        ],
        "human_annotation": doc.get("human_annotation"),
    })
