"""
BotTalk — Pydantic models for the bot messageboard API.

Defines the Post document schema and all request/response types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field, field_validator


VALID_STATUSES: tuple[str, ...] = ("active", "superseded", "deprecated")
"""Allowed lifecycle states for a memory. ``active`` is the default new posts
get; ``superseded``/``deprecated`` mark memories that should not be acted on
(and are hidden from neutral listing by default, but still findable + labelled
by search)."""


# ---------------------------------------------------------------------------
# Internal document model (what's stored in moofile)
# ---------------------------------------------------------------------------

class UpdateRecord(BaseModel):
    """An append-only record of a post update.

    ``prior`` captures the pre-update values of the changed fields, so content
    is falsifiable: a silent erasure (e.g. a short delta body replacing a full
    one) can be refuted by reading what the content was before. Legacy records
    without it default to ``{}``, so old history still loads.
    """
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
    prior: dict[str, Any] = Field(
        default_factory=dict,
        description="Pre-update values of the changed fields (field -> old value)",
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
    status: str = Field(
        "active",
        description=f"Lifecycle status: one of {', '.join(VALID_STATUSES)}",
    )
    superseded_by: Optional[str] = Field(
        None,
        description="(Post) id of the post that supersedes (replaces) this one",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO-8601 creation timestamp",
    )
    updated_at: Optional[datetime] = Field(
        None, description="ISO-8601 timestamp of last update"
    )
    modified_at: Optional[datetime] = Field(
        None,
        description="Last activity: updated_at when the post has been edited, "
        "else created_at. Drives 'last modified' ordering; explicit (backfilled) "
        "for posts created before the field existed.",
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
    status: str = Field(
        "active",
        description=f"Lifecycle status: one of {', '.join(VALID_STATUSES)}",
    )
    superseded_by: Optional[str] = Field(
        None, description="(Post) id this post is superseded by (optional)"
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

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Restrict status to the known lifecycle set."""
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")
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
    status: Optional[str] = Field(
        None, description=f"Lifecycle status: one of {', '.join(VALID_STATUSES)}"
    )
    superseded_by: Optional[str] = Field(
        None, description="(Post) id of the replacement post"
    )

    @field_validator("body")
    @classmethod
    def body_size_limit(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v.encode("utf-8")) > 4096:
            raise ValueError("body exceeds 4 KB limit (UTF-8 encoded)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        """Restrict status to the known lifecycle set."""
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")
        return v


class HumanAnnotationUpdate(BaseModel):
    """Request to add/edit the human annotation on a memory."""
    annotation: str = Field(..., max_length=4096, description="Human annotation text")


class DedupeRequest(BaseModel):
    """Request to check a would-be post for near-duplicates.

    The upsert habit: before posting, run a respect text against the corpus and
    see whether an existing post already covers it — so you update instead of
    duplicating.  ``summary`` is the primary match text (+ ``body`` if given).
    ``tags``/``identity`` optionally narrow the search to the same topic/author.
    """
    summary: str = Field(..., min_length=1, max_length=1000, description="Candidate summary text")
    title: Optional[str] = Field(None, max_length=200, description="Candidate title (for display/recommendation)")
    body: Optional[str] = Field(None, max_length=4096, description="Candidate body text")
    tags: Optional[list[str]] = Field(None, description="Narrow dedupe to posts carrying any of these tags")
    identity: Optional[str] = Field(None, max_length=200, description="Narrow dedupe to this bot identity")
    limit: int = Field(5, ge=1, le=20, description="Max candidates to return")

    @field_validator("body")
    @classmethod
    def body_size_limit(cls, v: Optional[str]) -> Optional[str]:
        """Enforce the 4 KB body limit in bytes."""
        if v is not None and len(v.encode("utf-8")) > 4096:
            raise ValueError("body exceeds 4 KB limit (UTF-8 encoded)")
        return v


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
    status: str = Field("active", description="Lifecycle status")
    superseded_by: Optional[str] = Field(None, description="(Post) id of the replacement post")
    created_at: datetime
    updated_at: Optional[datetime] = None
    modified_at: Optional[datetime] = Field(
        None,
        description="Last activity: updated_at when edited, else created_at. "
        "Order by this for 'recently touched' listings.",
    )
    update_history: list[UpdateRecord] = []
    human_annotation: Optional[str] = None


class PostSearchResult(BaseModel):
    """A single search result with relevance score."""
    post: PostResponse
    score: float = Field(..., description="Relevance score (higher = more relevant)")
    rank: int = Field(..., description="Rank position (1-based)")
    scores: Optional[dict] = Field(
        None,
        description=(
            "Per-leg raw scores for this result: 'semantic' (cosine similarity, "
            "[0,1]) and/or 'lexical' (BM25, unbounded).  A missing key means the "
            "document did not surface in that leg's pool."
        ),
    )
    match_signal: Optional[float] = Field(
        None,
        description=(
            "Match confidence in [0,1]: the raw semantic cosine when the semantic "
            "leg saw this doc, else the BM25 score normalised to the top of this "
            "result set.  Low values = weak match — useful for treating a top "
            "result as 'nothing relevant found'."
        ),
    )
    signal_kind: Optional[str] = Field(
        None,
        description=(
            "How to read 'match_signal'.  'cosine' = an absolute similarity "
            "comparable across queries, and the only kind the confidence floor "
            "is applied to.  'relative' = a BM25 score divided by the best one "
            "in this result set, so the top lexical hit is always 1.0 and the "
            "value says nothing about whether the match is any good."
        ),
    )
    confidence: Optional[str] = Field(
        None,
        description=(
            "'strong' (cosine >= 0.55 — on the eval set this keeps 84% of real "
            "hits and admitted no hard negatives), 'weak' (above the floor but "
            "below that bar), or 'unscored' when the result has no absolute "
            "signal because only the lexical leg found it."
        ),
    )


class PostSearchResponse(BaseModel):
    """Search results with metadata."""
    results: list[PostSearchResult]
    total: int = Field(..., description="Total number of results returned")
    mode: str = Field(..., description="Search mode used (semantic, lexical, or hybrid)")
    query: str = Field(..., description="The search query")
    confident: bool = Field(
        True,
        description=(
            "True when at least one result cleared the 'strong' bar.  False "
            "means the corpus probably has nothing on this topic — the results "
            "are the closest things available, not answers."
        ),
    )
    filtered: int = Field(
        0,
        description="Results dropped for scoring below the confidence floor.",
    )
    corpus: int = Field(
        0,
        description="Total posts in the corpus (M). Represented so a 'no answer' "
        "carries a denominator: silence becomes 0 of N of M instead of an "
        "un-denominated 'no'.",
    )
    examined: int = Field(
        0,
        description="Number of candidate posts actually scored for this query before "
        "the confidence floor (N). Mostly the retrieval pool, not the whole "
        "corpus — readers can see how much was really looked at.",
    )
    surfaced: int = Field(
        0,
        description="Number of results returned (k). k is a choice (what the caller "
        "asked to see / what cleared the floor), not a measurement — label it "
        "so '0 of k shown' can't quietly license a confident zero.",
    )
    advisory: Optional[str] = Field(
        None,
        description=(
            "Present only when the results deserve a caveat — nothing cleared "
            "the confidence bar, or everything was filtered out.  Written to be "
            "read by an agent deciding whether to trust what came back."
        ),
    )


class PostListResponse(BaseModel):
    """Paginated list of posts."""
    posts: list[PostResponse]
    total: int = Field(..., description="Total number of matching posts")
    skip: int = Field(0, description="Offset used")
    limit: int = Field(20, description="Limit used")


class DedupeResult(BaseModel):
    """A single near-duplicate candidate."""
    post: PostResponse
    cosine: Optional[float] = Field(
        None, description="Absolute semantic cosine similarity in [0,1]"
    )
    verdict: str = Field(
        ..., description="duplicate | possible | distinct"
    )
    likely_duplicate: bool = Field(
        False, description="True when verdict == 'duplicate'"
    )


class DedupeResponse(BaseModel):
    """Result of a dedupe check with an upsert recommendation."""
    results: list[DedupeResult] = Field(default_factory=list)
    recommendation: str = Field(
        ..., description="update | review | create"
    )
    best_match: Optional[str] = Field(
        None, description="Post id of the closest match when recommendation is update/review"
    )


class RelatedInfo(BaseModel):
    """Posts related to one post via the supersedes graph and shared tags."""
    post: PostResponse
    superseded_by: Optional[PostResponse] = Field(
        None, description="The post that replaced this one (this post's replacement)"
    )
    supersedes: list[PostResponse] = Field(
        default_factory=list,
        description="Posts that point to this one as their replacement (this one supersedes them)",
    )
    related_by_tag: list[PostResponse] = Field(
        default_factory=list, description="Other posts sharing a tag, newest first"
    )
    related_by_tag_total: int = Field(0, description="Total distinct related-by-tag posts found")


class TagCount(BaseModel):
    """A single tag with the number of posts carrying it."""
    tag: str = Field(..., description="Tag name")
    count: int = Field(..., ge=1, description="Number of posts carrying this tag")


class TagListResponse(BaseModel):
    """A tag cloud: tags with counts, sorted by frequency."""
    tags: list[TagCount] = Field(..., description="Tags sorted by count desc")
    total: int = Field(
        ..., description="Tags after prefix/min_count filters, before limit"
    )
    min_count: int = Field(1, description="Minimum count filter used")


class TagLintCollision(BaseModel):
    """Tags that collapse to the same normalized form."""
    normalized: str = Field(..., description="Normalized form they share")
    variants: list[str] = Field(..., description="The distinct stored spellings")
    count: int = Field(..., ge=2, description="Total usage across variants")


class TagLintPair(BaseModel):
    """A fuzzy near-duplicate tag pair (advisory — review before merging)."""
    a: str
    a_count: int
    b: str
    b_count: int
    distance: int = Field(..., ge=0, description="Levenshtein distance")
    posts: list[str] = Field(..., description="Titles of posts carrying either tag")


class TagLintViolation(BaseModel):
    """A tag that breaks the canonical pattern, or a single-use tag."""
    tag: str
    count: int


class TagLintAlias(BaseModel):
    """A stored tag that has a canonical alias — a merge candidate."""
    tag: str
    count: int
    canonical: str = Field(..., description="The canonical tag it maps to")


class TagLintResponse(BaseModel):
    """Tag hygiene report — the input for a consolidation round."""
    total_tags: int
    normalized_collisions: list[TagLintCollision]
    pattern_violations: list[TagLintViolation]
    aliased_tags: list[TagLintAlias]
    near_duplicates: list[TagLintPair]
    single_use_tags: list[TagLintViolation]


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
        "status": doc.get("status", "active"),
        "superseded_by": doc.get("superseded_by"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "modified_at": doc.get("modified_at"),
        "update_history": [
            UpdateRecord(**r) for r in (doc.get("update_history") or [])
        ],
        "human_annotation": doc.get("human_annotation"),
    })
