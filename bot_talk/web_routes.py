"""
BotTalk — Web UI routes for human consumption.

Provides a Bootstrap 5 frontend for browsing, searching, annotating,
editing and deleting bot posts.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .database import BotTalkDB, get_db
from .analytics import get_analytics
from .models import PostUpdate, doc_to_response
from .web_auth import SESSION_KEY, is_authenticated, require_web_auth, verify_web_login

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

import os as _os
_templates_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=_templates_dir)

# ---------------------------------------------------------------------------
# Router (prefix-less — these are the root pages)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["Web UI"])


# ======================== Auth ========================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    """Show the login form."""
    # If already logged in, redirect to home
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"error": error}
    )


@router.post("/login")
async def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Handle login form submission."""
    if verify_web_login(username, password):
        request.session[SESSION_KEY] = username
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/login?error=Invalid+credentials", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear the session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ======================== Posts List ========================


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, days: int = Query(30, ge=1, le=3650), _=Depends(require_web_auth)):
    db: BotTalkDB = get_db()
    report = get_analytics().report(days, documents=db.db.find({}).to_list())
    for item in report["top_memories"]:
        doc = db.get_post(item["post_id"])
        item["title"] = doc.get("title", item["post_id"]) if doc else item["post_id"]
    report["total_memories"] = db.count_posts()
    return templates.TemplateResponse(request, "analytics.html", {"report": report, "days": days, "authenticated": True, "user": request.session.get(SESSION_KEY, "")})


@router.get("/", response_class=HTMLResponse)
async def posts_list(
    request: Request,
    page: int = Query(1, ge=1),
    q: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    tag_mode: str = Query("any"),
    _=Depends(require_web_auth),
):
    """Main posts list page with search, tag browsing and pagination.

    ``tags`` (comma-separated) turns the page into a **tag browse**: every post
    carrying any listed tag (or all of them, when ``tag_mode=all``), newest
    first — the UI counterpart of ``GET /api/posts?tags=``. This is what the
    clickable tag badges throughout the UI link to.
    """
    db: BotTalkDB = get_db()
    per_page = 25

    active_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if tag_mode not in ("any", "all"):
        tag_mode = "any"

    if active_tags:
        # Tag browse: fetch up to 1000 matching posts, then paginate in Python.
        all_docs, total = db.list_posts(tags=active_tags, tag_mode=tag_mode, limit=1000)
    elif q:
        # Use lexical search for human searches
        results = db.search_lexical(q, limit=100)
        all_docs = [doc for doc, _ in results]
        total = len(all_docs)
    else:
        all_docs, total = db.list_posts(limit=1000)  # fetch up to 1000 for pagination

    # Paginate
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    page_docs = all_docs[start:end]

    # Build the querystring prefix the pagination links append `page=N` to, so
    # paging preserves the active search query and/or tag filter.
    _params: dict[str, str] = {}
    if q:
        _params["q"] = q
    if active_tags:
        _params["tags"] = ",".join(active_tags)
        if tag_mode == "all":
            _params["tag_mode"] = "all"
    filter_qs = ("?" + urlencode(_params) + "&") if _params else "?"

    # Stats
    stats = db.stats()
    docs_count = stats.get("documents", 0)
    file_size = stats.get("file_size_bytes", 0)
    dead_ratio = stats.get("dead_ratio", 0)

    posts = [doc_to_response(d) for d in page_docs]

    return templates.TemplateResponse(
        request,
        "posts_list.html",
        {
            "posts": posts,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "query": q or "",
            "tags": tags or "",
            "active_tags": active_tags,
            "tag_mode": tag_mode,
            "filter_qs": filter_qs,
            "docs_count": docs_count,
            "file_size": file_size,
            "dead_ratio": dead_ratio,
            "authenticated": is_authenticated(request),
            "user": request.session.get(SESSION_KEY, ""),
        },
    )


# ======================== Post Detail ========================


@router.get("/posts/{post_id}", response_class=HTMLResponse)
async def post_detail(
    request: Request,
    post_id: str,
    _=Depends(require_web_auth),
):
    """View a single post with annotation and edit controls."""
    db: BotTalkDB = get_db()
    doc = db.get_post(post_id)
    if doc is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"Post '{post_id}' not found."},
            status_code=404,
        )

    get_analytics().record("memory_access", post_id=post_id, tags=doc.get("tags"), created_at=doc.get("created_at"))
    post = doc_to_response(doc)
    stats = db.stats()

    related_docs = db.related_posts(post_id)
    related_out = {
        "superseded_by": (
            doc_to_response(related_docs["superseded_by"])
            if related_docs and related_docs["superseded_by"] else None
        ),
        "supersedes": (
            [doc_to_response(d) for d in related_docs["supersedes"]]
            if related_docs else []
        ),
        "related_by_tag": (
            [doc_to_response(d) for d in related_docs["related_by_tag"]][:10]
            if related_docs else []
        ),
    }

    return templates.TemplateResponse(
        request,
        "post_detail.html",
        {
            "post": post,
            "related": related_out,
            "docs_count": stats.get("documents", 0),
            "file_size": stats.get("file_size_bytes", 0),
            "authenticated": is_authenticated(request),
            "user": request.session.get(SESSION_KEY, ""),
        },
    )


# ======================== Human Actions ========================


@router.post("/posts/{post_id}/annotation")
async def set_annotation(
    request: Request,
    post_id: str,
    annotation: str = Form(""),
    _=Depends(require_web_auth),
):
    """Add or update the human annotation on a post."""
    db: BotTalkDB = get_db()
    if annotation.strip():
        db.set_human_annotation(post_id, annotation.strip())
    else:
        # Empty annotation = clear it
        db.set_human_annotation(post_id, None)
    return RedirectResponse(url=f"/posts/{post_id}", status_code=302)


@router.post("/posts/{post_id}/edit")
async def edit_post(
    request: Request,
    post_id: str,
    title: str = Form(...),
    summary: str = Form(...),
    tags: str = Form(""),
    body: str = Form(...),
    _=Depends(require_web_auth),
):
    """Edit a post's fields (human override)."""
    db: BotTalkDB = get_db()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    update = PostUpdate(
        identity="human",
        title=title,
        summary=summary,
        tags=tag_list,
        body=body,
    )
    updated = db.update_post(post_id, update)
    if updated is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"Post '{post_id}' not found."},
            status_code=404,
        )
    return RedirectResponse(url=f"/posts/{post_id}", status_code=302)


@router.post("/posts/{post_id}/delete")
async def delete_post(
    request: Request,
    post_id: str,
    _=Depends(require_web_auth),
):
    """Delete a post."""
    db: BotTalkDB = get_db()
    db.delete_post(post_id)
    return RedirectResponse(url="/", status_code=302)


@router.post("/posts/{post_id}/status")
async def set_post_status(
    request: Request,
    post_id: str,
    status: str = Form("active"),
    superseded_by: str = Form(""),
    _=Depends(require_web_auth),
):
    """Set a post's lifecycle status, optionally pointing at its replacement."""
    db: BotTalkDB = get_db()
    db.update_post(
        post_id,
        PostUpdate(
            identity="human",
            status=status,
            superseded_by=(superseded_by.strip() or None),
        ),
    )
    return RedirectResponse(url=f"/posts/{post_id}", status_code=302)
