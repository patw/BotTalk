"""
BotTalk — Database layer wrapping the moofile collection.

Provides all CRUD and search operations on top of the moofile BSON store.
The collection uses:
  - Regular indexes on ``identity`` and ``tags`` for fast filtering.
  - Text indexes (BM25) on ``title``, ``summary``, ``tags``, ``body``.
  - Vector index on ``summary_embedding`` with auto-embedding from the
    ``summary`` field via the local voyage-4-nano ONNX model (moofile >= 1.2.0).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from moofile import Collection, DocumentNotFoundError, MooFileError

from .models import PostUpdate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default database path (can be overridden via environment variable)
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bottalk.bson",
)

# Auto-embedding model config — voyage-4-nano (moofile >= 1.2.0), the default
# ONNX model bundled with moofile. 256 dims is deliberate MRL truncation of the
# model's 2048-dim output; int8 quantization keeps retrieval quality ~1.0000
# cosine vs f32 while cutting memory 4x. Model auto-downloaded from HF
# (onnx-community/voyage-4-nano-ONNX, ~422 MB) to ~/.cache/moofile/models/ on first use.
AUTO_EMBED_CONFIG = {
    "summary": {
        # "model" omitted -> moofile's built-in voyage-4-nano default
        "target": "summary_embedding",
        "dims": 256,
        "precision": "int8",
        "normalize": True,
        "max_length": 1024,
        # voyage-4-nano is asymmetric: queries carry an instruction prefix, docs do not.
        "query_prefix": "Represent the query for retrieving supporting documents: ",
        "doc_prefix": "",
    }
}

# Max results for any search
MAX_SEARCH_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 20


# ---------------------------------------------------------------------------
# Database wrapper
# ---------------------------------------------------------------------------

class BotTalkDB:
    """Thin wrapper around a moofile Collection for BotTalk operations."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, auto_embed: dict | None = None):
        self._path = db_path
        self._auto_embed = auto_embed if auto_embed is not None else AUTO_EMBED_CONFIG
        self._db: Collection | None = None

    def open(self) -> Collection:
        """Open (or reopen) the database collection.

        Returns the Collection for direct moofile access if needed.
        """
        if self._db is not None:
            try:
                # Quick health check
                self._db.stats()
                return self._db
            except Exception:
                self._db.close()
                self._db = None

        self._db = Collection(
            self._path,
            indexes=["identity"],
            text_indexes=["title", "summary", "tags", "body"],
            vector_indexes={"summary_embedding": 256},
            auto_embed=self._auto_embed,
        )
        return self._db

    def reembed(self, source_field: str = "summary") -> int:
        """Re-embed every document carrying ``source_field`` at the new width.

        Thin wrapper over moofile's ``reembed()`` — the recovery path after the
        embedding model or dims change.  Rewrites every stored vector at the
        configured width, retargets the vector index and its ``.meta`` entry,
        and clears the disabled-vector-index flag raised at open on a width
        mismatch.  Returns the number of documents rewritten.

        Not implicit on open() (it is a whole-collection write), so call it
        explicitly after a model/dims migration.
        """
        return self.db.reembed(source_field)

    def close(self) -> None:
        """Close the database."""
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    @property
    def db(self) -> Collection:
        """Get the open database, opening it if necessary."""
        if self._db is None:
            return self.open()
        return self._db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_post(
        self,
        title: str,
        summary: str,
        tags: list[str],
        body: str,
        identity: str,
    ) -> dict:
        """Insert a new post document.

        Returns the stored document (with ``_id`` and auto-embedding populated).
        """
        doc = {
            "title": title,
            "summary": summary,
            "tags": tags,
            "body": body,
            "identity": identity,
            "created_at": datetime.now(timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
        }
        return self.db.insert(doc)

    def get_post(self, post_id: str) -> dict | None:
        """Fetch a single post by its ``_id``."""
        return self.db.find_one({"_id": post_id})

    def list_posts(
        self,
        skip: int = 0,
        limit: int = 20,
        identity: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """List posts with optional filtering, sorted by created_at desc.

        Returns (documents, total_count).
        """
        # Build filter
        filter_dict: dict = {}
        if identity:
            filter_dict["identity"] = identity
        if tags:
            filter_dict["tags"] = {"$elemMatch": {"$in": tags}}

        # Count matching documents
        total = self.db.count(filter_dict) if filter_dict else self.db.count()

        # Fetch with sort + pagination
        results = (
            self.db.find(filter_dict)
            .sort("created_at", descending=True)
            .skip(skip)
            .limit(limit)
            .to_list()
        )

        return results, total

    def update_post(self, post_id: str, update: PostUpdate) -> dict | None:
        """Update a post, appending an update record to history.

        Returns the updated document, or ``None`` if not found.
        """
        doc = self.get_post(post_id)
        if doc is None:
            return None

        now = datetime.now(timezone.utc)

        # Build the set of fields to change
        set_fields: dict = {}
        changes_parts: list[str] = []

        if update.title is not None and update.title != doc.get("title"):
            set_fields["title"] = update.title
            changes_parts.append("title")

        if update.summary is not None and update.summary != doc.get("summary"):
            set_fields["summary"] = update.summary
            changes_parts.append("summary")

        if update.tags is not None and update.tags != doc.get("tags"):
            set_fields["tags"] = update.tags
            changes_parts.append("tags")

        if update.body is not None and update.body != doc.get("body"):
            set_fields["body"] = update.body
            changes_parts.append("body")

        if update.human_annotation is not None:
            set_fields["human_annotation"] = update.human_annotation
            changes_parts.append("human_annotation")

        if not set_fields:
            return doc  # No changes

        # Set updated_at
        set_fields["updated_at"] = now

        # Build the update record
        update_record = {
            "identity": update.identity,
            "timestamp": now,
            "changes": ", ".join(changes_parts),
        }

        # Append to update_history
        history = list(doc.get("update_history") or [])
        history.append(update_record)
        set_fields["update_history"] = history

        self.db.update_one({"_id": post_id}, set=set_fields)

        # Re-fetch to get the auto-embedded summary_embedding if summary changed
        return self.get_post(post_id)

    def delete_post(self, post_id: str) -> bool:
        """Delete a post by ID. Returns True if deleted."""
        return self.db.delete_one({"_id": post_id})

    def set_human_annotation(self, post_id: str, annotation: str) -> dict | None:
        """Set or update the human annotation on a post."""
        doc = self.get_post(post_id)
        if doc is None:
            return None

        self.db.update_one(
            {"_id": post_id},
            set={"human_annotation": annotation},
        )
        return self.get_post(post_id)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_semantic(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
    ) -> list[tuple[dict, float]]:
        """Semantic (vector) search on the summary field.

        The query text is embedded automatically using the configured GGUF model.
        Returns ``[(doc, score), ...]`` sorted by relevance (descending).
        """
        pre_filter = self._build_search_filter(identity, tags)
        return (
            self.db.find(pre_filter)
            .semantic("summary", query, limit=limit)
            .to_list()
        )

    def search_lexical(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
    ) -> list[tuple[dict, float]]:
        """Lexical (BM25) search across indexed text fields.

        Searches ``title``, ``summary``, ``tags`` and ``body``.
        Returns ``[(doc, score), ...]`` sorted by relevance (descending).
        Results are combined and deduplicated across fields.
        """
        pre_filter = self._build_search_filter(identity, tags)

        # Search across all text-indexed fields
        all_results: dict[str, tuple[dict, float]] = {}

        for field in ("title", "summary", "tags", "body"):
            try:
                results = (
                    self.db.find(pre_filter)
                    .text_search(field, query, limit=limit)
                    .to_list()
                )
                for doc, score in results:
                    doc_id = doc["_id"]
                    # Boost title matches
                    boost = 1.5 if field == "title" else 1.0
                    current_score = all_results.get(doc_id, (None, -9999))[1]
                    if score * boost > current_score:
                        all_results[doc_id] = (doc, score * boost)
            except Exception:
                continue  # Skip fields that might not support search

        # Sort by score descending
        sorted_results = sorted(
            all_results.values(), key=lambda x: x[1], reverse=True
        )
        return sorted_results[:limit]

    def search_hybrid(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
    ) -> list[tuple[dict, float]]:
        """Hybrid search: BM25 + semantic vector search fused via RRF.

        The semantic leg uses auto-embedding on the query text.
        Both result sets are combined using Reciprocal Rank Fusion.
        """
        pre_filter = self._build_search_filter(identity, tags)

        # Get both result sets
        semantic_results = (
            self.db.find(pre_filter)
            .semantic("summary", query, limit=limit * 3)
            .to_list()
        )

        lexical_results: dict[str, tuple[dict, float]] = {}
        for field in ("title", "summary", "tags", "body"):
            try:
                results = (
                    self.db.find(pre_filter)
                    .text_search(field, query, limit=limit * 3)
                    .to_list()
                )
                for doc, score in results:
                    doc_id = doc["_id"]
                    boost = 1.5 if field == "title" else 1.0
                    current = lexical_results.get(doc_id, (None, -9999))[1]
                    if score * boost > current:
                        lexical_results[doc_id] = (doc, score * boost)
            except Exception:
                continue

        # Reciprocal Rank Fusion
        k = 60  # Canonical RRF constant

        # Build rank positions
        rrf_scores: dict[str, float] = {}

        for rank, (doc, _) in enumerate(semantic_results):
            doc_id = doc["_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)

        for rank, (doc, _) in enumerate(
            sorted(lexical_results.values(), key=lambda x: x[1], reverse=True)
        ):
            doc_id = doc["_id"]
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)

        # Build final results
        doc_map: dict[str, dict] = {}
        for doc, _ in semantic_results:
            doc_map[doc["_id"]] = doc
        for doc_id, (doc, _) in lexical_results.items():
            doc_map[doc_id] = doc

        fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        return [(doc_map[did], score) for did, score in fused[:limit]]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Get database statistics."""
        return self.db.stats()

    def compact(self) -> None:
        """Rewrite the database, reclaiming dead append-only records."""
        self.db.compact()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_search_filter(
        identity: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Build a moofile filter dict for search pre-filtering."""
        filter_dict: dict = {}
        if identity:
            filter_dict["identity"] = identity
        if tags:
            filter_dict["tags"] = {"$elemMatch": {"$in": tags}}
        return filter_dict


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_db_instance: BotTalkDB | None = None


def get_db(auto_embed: dict | None = None) -> BotTalkDB:
    """Get or create the global BotTalkDB singleton."""
    global _db_instance
    if _db_instance is None:
        db_path = os.environ.get("BOTTALK_DB_PATH", DEFAULT_DB_PATH)
        _db_instance = BotTalkDB(db_path=db_path, auto_embed=auto_embed)
        _db_instance.open()
    return _db_instance


def close_db() -> None:
    """Close and reset the global database singleton."""
    global _db_instance
    if _db_instance is not None:
        _db_instance.close()
        _db_instance = None
