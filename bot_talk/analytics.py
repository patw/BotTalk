"""Event-based usage and corpus analytics for BotTalk."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import os

from moofile import Collection


class AnalyticsDB:
    def __init__(self, path: str):
        self.path = path
        self._db: Collection | None = None

    @property
    def db(self) -> Collection:
        if self._db is None:
            self._db = Collection(self.path)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    def record(self, event: str, post_id: str | None = None,
               tags: list[str] | None = None, query: str | None = None,
               session_id: str | None = None, mode: str | None = None,
               result_count: int | None = None, result_ids: list | None = None,
               created_at=None) -> None:
        now = datetime.now(timezone.utc)
        self.db.insert({
            "event": event, "post_id": post_id, "tags": tags or [],
            "query": query, "session_id": session_id, "mode": mode,
            "result_count": result_count, "result_ids": result_ids or [],
            "created_at": created_at,
            "timestamp": now, "date": now.date().isoformat(),
        })

    def report(self, days: int = 30, documents: list[dict] | None = None) -> dict:
        days = max(1, min(days, 3650))
        today = datetime.now(timezone.utc).date()
        cutoff_date = today - timedelta(days=days - 1)
        cutoff = cutoff_date.isoformat()
        events = self.db.find({"date": {"$gte": cutoff}}).to_list()
        documents = documents or []
        memory_hits, tag_hits, tag_accesses = Counter(), Counter(), Counter()
        tag_coverage, identity_hits = Counter(), Counter()
        query_counts, zero_queries, modes = Counter(), Counter(), Counter()
        by_day = defaultdict(lambda: {"memories_added": 0, "memories_searched": 0, "memory_accesses": 0})
        accessed_ids, searches, accesses = set(), [], []
        for event in events:
            day, kind = event.get("date"), event.get("event")
            if kind == "memory_access" and event.get("post_id"):
                pid = event["post_id"]
                accesses.append(event); accessed_ids.add(pid); memory_hits[pid] += 1
                by_day[day]["memory_accesses"] += 1
                for tag in event.get("tags") or []:
                    tag_hits[tag] += 1; tag_accesses[tag] += 1
            elif kind == "memory_added":
                by_day[day]["memories_added"] += 1
            elif kind == "memory_search":
                searches.append(event); by_day[day]["memories_searched"] += 1
                query = (event.get("query") or "").strip().lower()
                if query: query_counts[query] += 1
                if event.get("result_count", 0) == 0 and query: zero_queries[query] += 1
                modes[event.get("mode") or "unknown"] += 1
        for doc in documents:
            for tag in doc.get("tags") or []: tag_coverage[tag] += 1
            identity_hits[doc.get("identity") or "unknown"] += memory_hits.get(doc.get("_id"), 0)

        # Search-to-access funnel. A search counts as acted-upon when a memory
        # it returned is retrieved within five minutes (post_id funnel, recorded
        # on every search as result_ids), or when the same session retrieves a
        # memory within five minutes (session funnel, when X-BotTalk-Session is
        # supplied). The post_id funnel needs no client changes and is the
        # primary signal.
        WINDOW = timedelta(minutes=5)
        access_by_post: dict[str, list] = defaultdict(list)
        for a in accesses:
            if a.get("post_id") and a.get("timestamp"):
                access_by_post[a["post_id"]].append(a["timestamp"])

        def _result_accessed_soon(post_ids, ts) -> bool:
            if not ts or not post_ids:
                return False
            for pid in post_ids:
                if any(timedelta(0) <= ats - ts <= WINDOW
                       for ats in access_by_post.get(pid, ())):
                    return True
            return False

        acted_searches = 0
        for search in searches:
            ts = search.get("timestamp")
            sid = search.get("session_id")
            if _result_accessed_soon(search.get("result_ids") or [], ts):
                acted_searches += 1
            elif (sid and ts and any(
                    a.get("session_id") == sid and a.get("timestamp") and
                    timedelta(0) <= a["timestamp"] - ts <= WINDOW
                    for a in accesses)):
                acted_searches += 1
        daily = []
        for i in range(days):
            d = (cutoff_date + timedelta(days=i)).isoformat()
            daily.append({"date": d, **by_day[d]})
        total_accesses = sum(memory_hits.values())
        total_memories = len(documents)
        top_memory_ids = [k for k, _ in memory_hits.most_common(10)]
        never_accessed = [d for d in documents if d.get("_id") not in accessed_ids]
        ages = []
        for event in accesses:
            created = event.get("created_at")
            if created and event.get("timestamp"):
                try: ages.append(max(0, (event["timestamp"] - created).total_seconds() / 86400))
                except (TypeError, ValueError): pass
        tag_stats = []
        for tag, coverage in tag_coverage.most_common():
            tag_stats.append({"tag": tag, "memories": coverage, "accesses": tag_accesses[tag],
                              "access_rate": round(tag_accesses[tag] / coverage, 3) if coverage else 0})
        return {
            "days": days, "total_memories": total_memories,
            "total_searches": len(searches), "total_accesses": total_accesses,
            "top_memories": [{"post_id": k, "hits": v} for k, v in memory_hits.most_common(10)],
            "top_tags": [{"tag": k, "hits": v} for k, v in tag_hits.most_common(25)],
            "tag_stats": tag_stats[:50], "daily": daily,
            "top_queries": [{"query": k, "searches": v} for k, v in query_counts.most_common(25)],
            "zero_result_queries": [{"query": k, "searches": v} for k, v in zero_queries.most_common(25)],
            "search_modes": [{"mode": k, "searches": v} for k, v in modes.most_common()],
            "unused_memories": len(never_accessed), "single_access_memories": sum(1 for v in memory_hits.values() if v == 1),
            "corpus_reach_pct": round(100 * len(accessed_ids) / total_memories, 1) if total_memories else 0,
            "top10_concentration_pct": round(100 * sum(memory_hits[k] for k in top_memory_ids) / total_accesses, 1) if total_accesses else 0,
            "search_to_access_pct": round(100 * acted_searches / len(searches), 1) if searches else 0,
            "avg_memory_age_days": round(sum(ages) / len(ages), 1) if ages else 0,
            "identity_hits": [{"identity": k, "hits": v} for k, v in identity_hits.most_common() if v],
            "tag_stats": tag_stats[:50],
        }


_analytics: AnalyticsDB | None = None

def get_analytics() -> AnalyticsDB:
    global _analytics
    if _analytics is None:
        base = os.environ.get("BOTTALK_DB_PATH")
        _analytics = AnalyticsDB((base + ".analytics") if base else os.path.join(os.path.dirname(os.path.dirname(__file__)), "analytics.bson"))
    return _analytics

def close_analytics() -> None:
    global _analytics
    if _analytics is not None:
        _analytics.close(); _analytics = None
