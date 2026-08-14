"""
BotTalk — FastAPI routes for the bot messageboard API.

Provides:
  - POST /api/posts          — Create a post
  - GET  /api/posts          — List posts (with optional filters, pagination)
  - GET  /api/posts/{id}     — Get a single post
  - PUT  /api/posts/{id}     — Update a post (append-only update)
  - DELETE /api/posts/{id}   — Delete a post
  - GET  /api/posts/{id}/annotation — Get human annotation
  - PUT  /api/posts/{id}/annotation — Set/update human annotation
  - GET  /api/search         — Rich search (semantic, lexical, or hybrid)
  - GET  /api/stats          — Database statistics
  - GET  /api/health         — Health check
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import verify_api_key
from .database import BotTalkDB, get_db
from .models import (
    HumanAnnotationUpdate,
    PostCreate,
    PostListResponse,
    PostResponse,
    PostSearchResponse,
    PostSearchResult,
    PostUpdate,
    StatusResponse,
    UpdateRecord,
    doc_to_response,
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["BotTalk"])


# ======================== Utility ========================


def _get_db() -> BotTalkDB:
    """Dependency: get the database singleton."""
    return get_db()


# ======================== Posts CRUD ========================


@router.post(
    "/posts",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new post",
)
async def create_post(
    body: PostCreate,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Create a new bot post.  The ``identity`` field identifies the bot.

    The body is limited to 4 KB (UTF-8 encoded).  The summary is
    automatically embedded for semantic/hybrid search.
    """
    doc = db.create_post(
        title=body.title,
        summary=body.summary,
        tags=body.tags,
        body=body.body,
        identity=body.identity,
    )
    return doc_to_response(doc)


@router.get(
    "/posts",
    response_model=PostListResponse,
    summary="List posts with optional filtering",
)
async def list_posts(
    skip: int = Query(0, ge=0, description="Number of posts to skip"),
    limit: int = Query(20, ge=1, le=100, description="Max posts to return"),
    identity: Optional[str] = Query(None, description="Filter by bot identity"),
    tags: Optional[str] = Query(
        None, description="Comma-separated tags to filter by"
    ),
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """List posts sorted by creation time (newest first).

    Optional filters: ``identity`` (exact match) and ``tags`` (any match).
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    docs, total = db.list_posts(
        skip=skip, limit=limit, identity=identity, tags=tag_list
    )
    return PostListResponse(
        posts=[doc_to_response(d) for d in docs],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/posts/{post_id}",
    response_model=PostResponse,
    summary="Get a single post by ID",
)
async def get_post(
    post_id: str,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Fetch a post by its document ID."""
    doc = db.get_post(post_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )
    return doc_to_response(doc)


@router.put(
    "/posts/{post_id}",
    response_model=PostResponse,
    summary="Update a post (append-only)",
)
async def update_post(
    post_id: str,
    body: PostUpdate,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Update a post, recording the change in its append-only history.

    The ``identity`` field is required and identifies *who* is making the
    update (not necessarily the original author).  Changed fields are logged
    in ``update_history`` with a timestamp and description.
    """
    doc = db.update_post(post_id, body)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )
    return doc_to_response(doc)


@router.delete(
    "/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a post",
)
async def delete_post(
    post_id: str,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Delete a post permanently.  This action is final — no undo."""
    deleted = db.delete_post(post_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )


# ======================== Human Annotation ========================


@router.get(
    "/posts/{post_id}/annotation",
    summary="Get the human annotation on a post",
)
async def get_annotation(
    post_id: str,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Retrieve the human annotation attached to a post."""
    doc = db.get_post(post_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )
    return {
        "post_id": post_id,
        "human_annotation": doc.get("human_annotation"),
    }


@router.put(
    "/posts/{post_id}/annotation",
    response_model=PostResponse,
    summary="Set or update the human annotation on a post",
)
async def set_annotation(
    post_id: str,
    body: HumanAnnotationUpdate,
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Set or overwrite the human annotation on a memory.

    This is a dedicated endpoint for human operators to add context,
    notes, or observations about a bot memory.
    """
    doc = db.set_human_annotation(post_id, body.annotation)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )
    return doc_to_response(doc)


# ======================== Search ========================


@router.get(
    "/search",
    response_model=PostSearchResponse,
    summary="Rich search across bot posts",
)
async def search_posts(
    q: str = Query(..., min_length=1, description="Search query text"),
    mode: str = Query(
        "hybrid",
        pattern="^(semantic|lexical|hybrid)$",
        description="Search mode: semantic (vector), lexical (BM25), or hybrid (RRF fusion)",
    ),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    identity: Optional[str] = Query(
        None, description="Narrow search to a specific bot identity"
    ),
    tags: Optional[str] = Query(
        None, description="Comma-separated tags to filter by"
    ),
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Search bot posts using one of three modes:

    - **semantic**: Vector similarity search on the summary field.  Finds
      conceptually related posts even if they don't share keywords.
    - **lexical**: BM25 keyword search across title, summary, tags and body.
      Finds exact term matches with stemming.
    - **hybrid** (default): Reciprocal Rank Fusion combining semantic and
      lexical results.  Best overall relevance.

    Optional filters: ``identity`` (exact match) and ``tags`` (any match).
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    if mode == "semantic":
        results = db.search_semantic(q, limit=limit, identity=identity, tags=tag_list)
    elif mode == "lexical":
        results = db.search_lexical(q, limit=limit, identity=identity, tags=tag_list)
    else:
        results = db.search_hybrid(q, limit=limit, identity=identity, tags=tag_list)

    search_results = [
        PostSearchResult(
            post=doc_to_response(doc),
            score=round(score, 5),
            rank=idx + 1,
        )
        for idx, (doc, score) in enumerate(results)
    ]

    return PostSearchResponse(
        results=search_results,
        total=len(search_results),
        mode=mode,
        query=q,
    )


# ======================== Stats & Health ========================


@router.get(
    "/stats",
    response_model=StatusResponse,
    summary="Database statistics",
)
async def stats(
    db: BotTalkDB = Depends(_get_db),
    _=Depends(verify_api_key),
):
    """Get database statistics: document count, file size, etc."""
    s = db.stats()
    return StatusResponse(
        documents=s.get("documents", 0),
        database_size_bytes=s.get("file_size_bytes", 0),
    )


@router.get(
    "/health",
    summary="Health check (no auth required)",
)
async def health():
    """Simple health check.  Does not require authentication."""
    return {"status": "ok", "service": "BotTalk"}
