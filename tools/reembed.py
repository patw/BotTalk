#!/usr/bin/env python3
"""One-off re-embed: rewrite every stored summary_embedding at the configured
width (voyage-4-nano, 256-dim int8 as of the 2026-08-16 migration).

Run with the project venv, with the bottalk service STOPPED (the service's
in-memory index would otherwise go stale while this rewrites the collection):

    sudo systemctl stop bottalk
    .venv/bin/python tools/reembed.py
    sudo systemctl start bottalk

Idempotent: moofile's reembed() rewrites all docs carrying `summary` at the
current auto_embed width and retargets the vector index + .meta entry.
"""
from __future__ import annotations

import os
import sys
import time

# Ensure the project root is on the path (so `bot_talk` imports resolve).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_talk.database import BotTalkDB, DEFAULT_DB_PATH


def main() -> None:
    db_path = os.environ.get("BOTTALK_DB_PATH", DEFAULT_DB_PATH)
    db = BotTalkDB(db_path=db_path)
    db.open()

    before = db.stats()
    print(f"before: documents={before.get('documents')} dead_ratio={before.get('dead_ratio')}")

    t0 = time.time()
    n = db.reembed("summary")
    print(f"reembed summary: {n} doc(s) rewritten in {time.time() - t0:.2f}s")
    n2 = db.reembed("search_text")
    print(f"reembed search_text: {n2} doc(s) rewritten in {time.time() - t0:.2f}s")

    # Sanity: every stored embedding is 512-dim now
    docs, _ = db.list_posts(skip=0, limit=100000)
    s_lens = {len(d.get("summary_embedding", [])) for d in docs}
    e_lens = {len(d.get("search_embedding", [])) for d in docs}
    print(f"verify: {len(docs)} docs, summary lens={s_lens}, search lens={e_lens}")

    after = db.stats()
    print(f"after:  documents={after.get('documents')} dead_ratio={after.get('dead_ratio')}")
    db.close()

    meta_path = db_path + ".meta"
    if os.path.exists(meta_path):
        print("meta:")
        print(open(meta_path).read())


if __name__ == "__main__":
    main()
