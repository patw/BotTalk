"""
BotTalk — Database layer wrapping the moofile collection.

Provides all CRUD and search operations on top of the moofile BSON store.
The collection uses:
  - Regular indexes on ``identity`` and ``tags`` for fast filtering.
  - Text indexes (BM25) on ``title``, ``summary``, ``tags``, ``body``.
  - Vector indexes on ``summary_embedding`` (from ``summary``) and
    ``search_embedding`` (from ``search_text`` = summary+body) with
    auto-embedding via the local voyage-4-nano ONNX model (moofile >= 1.2.0).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
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
# ONNX model bundled with moofile. 512 dims is MRL truncation of the model's
# 2048-dim output — the recommended quality/size starting point (A/B on the live
# corpus: hybrid NDCG@5 .878 at 512 vs .788 at 256, identical recall). int8
# quantization keeps retrieval quality ~1.0000 cosine vs f32 while cutting memory
# 4x. Model auto-downloaded from HF (onnx-community/voyage-4-nano-ONNX, ~422 MB)
# to ~/.cache/moofile/models/ on first use.
#
# TWO embedding fields:
#   summary_embedding — from ``summary`` (kept for display/back-compat).
#   search_embedding  — from ``search_text`` = summary + body.  Semantic search
#                       uses THIS so body-only knowledge is retrievable (eval
#                       RUN 4 showed body-only facts were invisible to semantic
#                       because it only embedded the summary).
AUTO_EMBED_CONFIG = {
    "summary": {
        # "model" omitted -> moofile's built-in voyage-4-nano default
        "target": "summary_embedding",
        "dims": 512,
        "precision": "int8",
        "normalize": True,
        "max_length": 1024,
        # voyage-4-nano is asymmetric: queries carry an instruction prefix, docs do not.
        "query_prefix": "Represent the query for retrieving supporting documents: ",
        "doc_prefix": "",
    },
    "search_text": {
        # "model" omitted -> moofile's built-in voyage-4-nano default
        "target": "search_embedding",
        "dims": 512,
        "precision": "int8",
        "normalize": True,
        "max_length": 1024,
        "query_prefix": "Represent the query for retrieving supporting documents: ",
        "doc_prefix": "",
    },
}

# Max results for any search
MAX_SEARCH_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 20


# ---------------------------------------------------------------------------
# Tag normalization & aliases
# ---------------------------------------------------------------------------

# Canonical tag pattern: lowercase alphanumeric tokens joined by a single
# hyphen or dot (dots preserve version tags like 'v1.2.0' / 'llama.cpp').
TAG_RE = re.compile(r"^[a-z0-9]+([.-][a-z0-9]+)*$")
MAX_TAG_LEN = 50

# Legacy spellings / synonyms -> canonical tag.  Consulted at write time
# (new tags are coerced to the canonical form) and at query time (searching
# any spelling still finds the posts).  Add entries here when the lint
# surfaces a genuine duplicate.
TAG_ALIASES = {
    "skills": "skill",
    "opensource": "open-source",
    "openai-proxy": "llmproxy",
    "llm-proxy": "llmproxy",
    "max_length": "max-length",
    # Confirmed synonyms from the 2026-09-11 ``tags --lint`` pass. Deliberately
    # NOT aliased (advisory-only near-dupes that are genuinely different tags):
    # pengy <-> pengyr (different editions), date tags (2026-08-22 vs
    # 2026-08-23), and unrelated d=1 pairs like gaming <-> naming.
    "bot-talk": "bottalk",
    "llama-cpp": "llama.cpp",
    "game-dev": "gamedev",
    "bug-fix": "bugfix",
    "gotchas": "gotcha",
    "pengy-r": "pengyr",
}


def normalize_tag(tag: str) -> str:
    """Normalize a raw tag to canonical kebab/dotted-case form.

    Lowercases, trims, and collapses runs of non-alphanumeric characters to
    a single hyphen, preserving dots so version tags ('v1.2.0', 'llama.cpp')
    survive.  'Voyage 4 Nano', 'voyage_4_nano' and 'VOYAGE 4 NANO' all
    become 'voyage-4-nano'.
    """
    t = re.sub(r"[^a-z0-9.]+", "-", tag.strip().lower())
    t = re.sub(r"-{2,}", "-", t)
    t = re.sub(r"\.{2,}", ".", t)
    t = t.strip("-.")
    return t[:MAX_TAG_LEN] if len(t) > MAX_TAG_LEN else t


def _ts(doc: dict, field: str) -> float:
    """Epoch seconds for a datetime field, or 0.0 if unset/absent."""
    v = doc.get(field)
    return v.timestamp() if isinstance(v, datetime) else 0.0


def _modified_sort_key(doc: dict) -> tuple[float, float]:
    """Sort key for "last modified" ordering (newest activity first).

    A post's modification time is ``updated_at`` when it has been edited, else
    its ``created_at``.  ``created_at`` is the tie-breaker so a batch of
    never-edited posts keeps its original newest-first order.  moofile's
    ``.sort()`` sorts a single field and, under descending order, floats a
    *missing* value to the top — so a plain ``.sort("updated_at")`` would put
    every never-edited post first.  Coalescing here is what makes it correct.
    """
    created = _ts(doc, "created_at")
    updated = _ts(doc, "updated_at")
    return (updated or created, created)


def _levenshtein(a: str, b: str) -> int:
    """Edit distance, bounded cheaply for short tag pairs."""
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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
            vector_indexes={"summary_embedding": 512, "search_embedding": 512},
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

    def count_posts(self) -> int:
        """Return the current number of live memories."""
        return self.db.count()

    def create_post(
        self,
        title: str,
        summary: str,
        tags: list[str],
        body: str,
        identity: str,
        status: str = "active",
        superseded_by: str | None = None,
    ) -> dict:
        """Insert a new post document.

        Returns the stored document (with ``_id`` and auto-embedding populated).
        """
        doc = {
            "title": title,
            "summary": summary,
            "tags": self._canonicalize_tags(tags),
            "body": body,
            "identity": identity,
            "status": status,
            "superseded_by": superseded_by,
            "created_at": datetime.now(timezone.utc),
            "updated_at": None,
            "update_history": [],
            "human_annotation": None,
            "search_text": self._build_search_text(summary, body),
        }
        return self.db.insert(doc)

    def get_post(self, post_id: str) -> dict | None:
        """Fetch a single post by its ``_id``."""
        return self.db.find_one({"_id": post_id})

    def related_posts(self, post_id: str) -> dict | None:
        """Return the supersedes graph + tag-neighbours around a post.

        Returns ``None`` if the post doesn't exist.  Otherwise:
          - ``superseded_by``: the post that replaced this one (the
            ``superseded_by`` link target), if any.
          - ``supersedes``: posts that point at ``post_id`` as their replacement.
          - ``related_by_tag``: other posts sharing a tag, newest first (the
            explicit-link complement to fuzzy semantic search).
        """
        doc = self.get_post(post_id)
        if doc is None:
            return None

        superseded_by = None
        target = doc.get("superseded_by")
        if target:
            superseded_by = self.get_post(target)

        supersedes = (
            self.db.find({"superseded_by": post_id})
            .sort("created_at", descending=True)
            .limit(50)
            .to_list()
        )

        neighbours: dict[str, dict] = {}
        for tag in doc.get("tags") or []:
            for d in self.db.find({"tags": {"$elemMatch": {"$eq": tag}}}).to_list():
                if d["_id"] != post_id:
                    neighbours[d["_id"]] = d
        related = sorted(
            neighbours.values(),
            key=lambda x: x.get("created_at") or datetime.min,
            reverse=True,
        )

        return {
            "superseded_by": superseded_by,
            "supersedes": supersedes,
            "related_by_tag": related,
            "related_by_tag_total": len(related),
        }

    def list_posts(
        self,
        skip: int = 0,
        limit: int = 20,
        identity: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "any",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        statuses: list[str] | None = None,
        sort: str = "created",
    ) -> tuple[list[dict], int]:
        """List posts with optional filtering and sorting (newest first).

        ``tag_mode`` is ``"any"`` (posts carrying ANY of ``tags``) or
        ``"all"`` (posts carrying EVERY one of ``tags``). ``created_after``/
        ``created_before`` bound the creation window (after inclusive, before
        exclusive). ``statuses`` lists the post statuses to include (None =
        no status filter).

        ``sort`` is ``"created"`` (default — newest created first) or
        ``"modified"`` (most recently created *or updated* first, so an edit
        bubbles a post back to the top; see ``_modified_sort_key``).
        Returns (documents, total_count).
        """
        tags = self._expand_query_tags(tags, tag_mode)
        filter_dict = self._build_search_filter(
            identity, tags, tag_mode,
            created_after=created_after, created_before=created_before,
            statuses=statuses,
        )

        if sort == "modified":
            # Coalesced updated_at/created_at needs a Python pass (moofile
            # sorts one field and floats missing values to the top).  The whole
            # matching set is sorted, then paged, so the page is the true top-N.
            docs = self.db.find(filter_dict).to_list()
            docs.sort(key=_modified_sort_key, reverse=True)
            return docs[skip : skip + limit], len(docs)

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

        new_tags = (
            self._canonicalize_tags(update.tags) if update.tags is not None else None
        )
        if new_tags is not None and new_tags != doc.get("tags"):
            set_fields["tags"] = new_tags
            changes_parts.append("tags")

        if update.status is not None and update.status != doc.get("status"):
            set_fields["status"] = update.status
            changes_parts.append("status")

        if (
            "superseded_by" in update.model_fields_set
            and update.superseded_by != doc.get("superseded_by")
        ):
            set_fields["superseded_by"] = update.superseded_by
            changes_parts.append("superseded_by")

        if update.body is not None and update.body != doc.get("body"):
            set_fields["body"] = update.body
            changes_parts.append("body")

        if (
            "human_annotation" in update.model_fields_set
            and update.human_annotation != doc.get("human_annotation")
        ):
            set_fields["human_annotation"] = update.human_annotation
            changes_parts.append("human_annotation")

        # Rebuild the semantic search text whenever summary or body changes.
        if "summary" in set_fields or "body" in set_fields:
            set_fields["search_text"] = self._build_search_text(
                set_fields.get("summary", doc.get("summary", "")),
                set_fields.get("body", doc.get("body", "")),
            )

        if not set_fields:
            return doc  # No changes

        # Set updated_at
        set_fields["updated_at"] = now

        # Build the update record
        prior = {field: doc.get(field) for field in changes_parts}
        update_record = {
            "identity": update.identity,
            "timestamp": now,
            "changes": ", ".join(changes_parts),
            "prior": prior,
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

    def set_human_annotation(self, post_id: str, annotation: str | None) -> dict | None:
        """Set, replace, or clear a human annotation and audit the change.

        Annotation edits are ordinary memory changes, so route them through
        ``update_post`` rather than bypassing the append-only audit trail.
        ``None`` is used when the web UI clears a note.
        """
        return self.update_post(
            post_id,
            PostUpdate(identity="human", human_annotation=annotation),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_semantic(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "any",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        statuses: list[str] | None = None,
        with_scores: bool = False,
    ) -> list:
        """Semantic (vector) search on the ``search_text`` (summary+body) field.

        The query text is embedded automatically using the configured model.
        Returns ``[(doc, score), ...]`` sorted by relevance (descending), or —
        with ``with_scores=True`` — ``[(doc, score, {"semantic": score})]``
        where score is the raw cosine similarity.
        """
        tags = self._expand_query_tags(tags, tag_mode)
        pre_filter = self._build_search_filter(
            identity, tags, tag_mode,
            created_after=created_after, created_before=created_before,
            statuses=statuses,
        )
        results = (
            self.db.find(pre_filter)
            .semantic("search_text", query, limit=limit)
            .to_list()
        )
        if with_scores:
            return [(doc, score, {"semantic": score}) for doc, score in results]
        return results

    def search_lexical(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "any",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        statuses: list[str] | None = None,
        with_scores: bool = False,
    ) -> list:
        """Lexical (BM25) search across indexed text fields.

        Searches ``title``, ``summary``, ``tags`` and ``body``.
        Returns ``[(doc, score), ...]`` sorted by relevance (descending).
        Results are combined and deduplicated across fields.
        """
        tags = self._expand_query_tags(tags, tag_mode)
        pre_filter = self._build_search_filter(
            identity, tags, tag_mode,
            created_after=created_after, created_before=created_before,
            statuses=statuses,
        )

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
        )[:limit]
        if with_scores:
            return [
                (doc, score, {"lexical": score}) for doc, score in sorted_results
            ]
        return sorted_results

    def search_hybrid(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        identity: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "any",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        statuses: list[str] | None = None,
        with_scores: bool = False,
    ) -> list:
        """Hybrid search: BM25 + semantic vector search fused via RRF.

        The semantic leg embeds the query and compares against
        ``search_embedding`` (summary+body).  Both result sets are combined
        using Reciprocal Rank Fusion.  With ``with_scores=True`` returns
        ``[(doc, rrf_score, {"semantic": cos|None, "lexical": bm25|None})]``
        so callers can judge match confidence (and detect "nothing relevant").
        """
        tags = self._expand_query_tags(tags, tag_mode)
        pre_filter = self._build_search_filter(
            identity, tags, tag_mode,
            created_after=created_after, created_before=created_before,
            statuses=statuses,
        )

        # Get both result sets
        semantic_results = (
            self.db.find(pre_filter)
            .semantic("search_text", query, limit=limit * 3)
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
        sem_scores: dict[str, float] = {}
        for doc, sc in semantic_results:
            doc_map[doc["_id"]] = doc
            sem_scores[doc["_id"]] = sc
        lex_scores: dict[str, float] = {}
        for doc_id, (doc, sc) in lexical_results.items():
            doc_map[doc_id] = doc
            lex_scores[doc_id] = sc

        fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        top = fused[:limit]
        if not with_scores:
            return [(doc_map[did], score) for did, score in top]
        return [
            (
                doc_map[did],
                score,
                {
                    "semantic": sem_scores.get(did),
                    "lexical": lex_scores.get(did),
                },
            )
            for did, score in top
        ]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def list_tags(
        self,
        prefix: str | None = None,
        min_count: int = 1,
    ) -> list[dict]:
        """Aggregate tag frequencies across all posts.

        Returns a list of ``{"tag": str, "count": int}`` sorted by count
        descending (ties broken alphabetically).  Only tags used on at least
        ``min_count`` posts are included, optionally restricted to tags that
        start with ``prefix``.
        """
        counts: Counter[str] = Counter()
        for doc in self.db.find({}).to_list():
            for tag in doc.get("tags") or []:
                counts[tag] += 1

        tags = [
            {"tag": tag, "count": n}
            for tag, n in counts.items()
            if n >= min_count and (prefix is None or tag.startswith(prefix))
        ]
        tags.sort(key=lambda t: (-t["count"], t["tag"]))
        return tags

    def _known_tag_lookup(self) -> dict[str, str]:
        """Map normalized tag -> canonical stored form.

        Combines the existing vocabulary (so format variants of a live tag
        coerce to it) with ``TAG_ALIASES`` (which take precedence, so legacy
        spellings always map to their canonical target).
        """
        lookup: dict[str, str] = {}
        for entry in self.list_tags():
            lookup[normalize_tag(entry["tag"])] = entry["tag"]
        for alias, canon in TAG_ALIASES.items():
            lookup[normalize_tag(alias)] = canon
        return lookup

    def _canonicalize_tags(self, tags: list[str] | None) -> list[str]:
        """Normalize + coerce tags to canonical stored forms (write path).

        Format variants of an existing tag and known aliases map to the
        canonical spelling; genuinely new tags are stored in normalized
        kebab/dotted case.  Empty results and duplicates are dropped.
        """
        if not tags:
            return []
        lookup = self._known_tag_lookup()
        out: list[str] = []
        for tag in tags:
            nt = normalize_tag(tag)
            if not nt:
                continue
            canon = lookup.get(nt, nt)
            if canon not in out:
                out.append(canon)
        return out

    def _expand_query_tags(
        self, tags: list[str] | None, tag_mode: str
    ) -> list[str] | None:
        """Canonicalize query tags so aliases / legacy spellings still match.

        In ``any`` mode both the canonical form and the (normalized) original
        spelling are kept so pre-migration tags still match; in ``all`` mode
        only the canonical form is used (requiring every listed tag on a
        post).  Returns ``None`` when there is nothing to filter on.
        """
        if not tags:
            return None
        lookup = self._known_tag_lookup()
        expanded: list[str] = []
        for tag in tags:
            nt = normalize_tag(tag)
            canon = lookup.get(nt, nt)
            if canon not in expanded:
                expanded.append(canon)
            if tag_mode != "all" and nt != canon and nt not in expanded:
                expanded.append(nt)
        return expanded

    def lint_tags(self, fuzzy_limit: int = 40) -> dict:
        """Tag hygiene report — the input for a consolidation round.

        Returns normalized-form collisions, tags that break the canonical
        pattern, fuzzy near-duplicate candidate pairs (advisory: edit
        distance alone can pair unrelated tags like 'rrf'/'rrd'), and the
        single-use (long-tail) tags.
        """
        docs = self.db.find({}).to_list()
        counts: Counter[str] = Counter()
        tag_posts: dict[str, list[str]] = defaultdict(list)
        for d in docs:
            for t in d.get("tags") or []:
                counts[t] += 1
                tag_posts[t].append(str(d.get("title", ""))[:70])

        tags_sorted = sorted(counts)

        norm_groups: dict[str, list[str]] = defaultdict(list)
        for t in tags_sorted:
            norm_groups[normalize_tag(t)].append(t)
        collisions = [
            {
                "normalized": n,
                "variants": v,
                "count": sum(counts[x] for x in v),
            }
            for n, v in sorted(norm_groups.items())
            if len(v) > 1
        ]

        violations = [
            {"tag": t, "count": counts[t]}
            for t in tags_sorted
            if not TAG_RE.match(t)
        ]

        aliased = [
            {
                "tag": t,
                "count": counts[t],
                "canonical": TAG_ALIASES[nt],
            }
            for t in tags_sorted
            if (nt := normalize_tag(t)) in TAG_ALIASES and TAG_ALIASES[nt] != t
        ]
        aliased.sort(key=lambda a: (-a["count"], a["tag"]))

        pairs: list[dict] = []
        for i in range(len(tags_sorted)):
            for j in range(i + 1, len(tags_sorted)):
                a, b = tags_sorted[i], tags_sorted[j]
                d = _levenshtein(a, b)
                if d <= 2 and min(len(a), len(b)) >= 4:
                    pairs.append(
                        {
                            "a": a,
                            "a_count": counts[a],
                            "b": b,
                            "b_count": counts[b],
                            "distance": d,
                            "posts": sorted(set(tag_posts[a] + tag_posts[b]))[:6],
                        }
                    )
        pairs.sort(
            key=lambda p: (p["distance"], -max(p["a_count"], p["b_count"]), p["a"])
        )

        return {
            "total_tags": len(tags_sorted),
            "normalized_collisions": collisions,
            "pattern_violations": violations,
            "aliased_tags": aliased,
            "near_duplicates": pairs[:fuzzy_limit],
            "single_use_tags": [
                {"tag": t, "count": 1} for t in tags_sorted if counts[t] == 1
            ],
        }



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
    def _build_search_text(summary: str, body: str) -> str:
        """Combine summary + body into the semantic search text (``search_text``).

        Summary first (it carries the headline semantic content), then the body
        so body-only facts are retrievable.  This is what ``search_embedding``
        embeds.
        """
        return f"{summary or ''}\n\n{body or ''}".strip()

    @staticmethod
    def _build_search_filter(
        identity: str | None = None,
        tags: list[str] | None = None,
        tag_mode: str = "any",
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        statuses: list[str] | None = None,
    ) -> dict:
        """Build a moofile filter dict for search pre-filtering.

        ``tag_mode``:
          - ``"any"`` — match posts carrying ANY of the given tags (OR)
          - ``"all"`` — match posts carrying EVERY given tag (AND)

        ``created_after``/``created_before`` bound ``created_at`` (after is
        inclusive ``$gte``, before exclusive ``$lt``). ``statuses`` restricts
        to the given lifecycle statuses (None = no status filter).
        """
        filter_dict: dict = {}
        if identity:
            filter_dict["identity"] = identity
        if tags:
            if tag_mode == "all":
                filter_dict["$and"] = [
                    {"tags": {"$elemMatch": {"$eq": tag}}} for tag in tags
                ]
            else:
                filter_dict["tags"] = {"$elemMatch": {"$in": tags}}
        if created_after is not None or created_before is not None:
            created_range: dict = {}
            if created_after is not None:
                created_range["$gte"] = created_after
            if created_before is not None:
                created_range["$lt"] = created_before
            filter_dict["created_at"] = created_range
        if statuses:
            filter_dict["status"] = {"$in": statuses}
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
