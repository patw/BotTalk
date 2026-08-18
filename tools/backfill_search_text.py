#!/usr/bin/env python3
"""Migration: populate ``search_text`` + ``search_embedding`` (two-phase, batched).

BotTalk now embeds summary+body (``search_text``) for the semantic leg, so
body-only knowledge is retrievable (eval RUN 4: body-only facts were invisible
to semantic because it only embedded the summary).

Two phases so the per-doc auto-embed (slow: ~0.3s/doc) never runs:

  Phase A — write ``search_text`` on every doc *without* auto-embedding
            (open with ``auto_embed={}`` so update_one is a plain field write;
            seconds for the whole corpus).
  Phase B — reopen with the full config and ``db.reembed("search_text")``,
            which batch-embeds the whole corpus (moofile batches at
            ``AutoEmbedConfig::batch_size`` — the fast path reembed uses).

Run with the bottalk service STOPPED:

    sudo systemctl stop bottalk
    .venv/bin/python tools/backfill_search_text.py
    sudo systemctl start bottalk

Idempotent: docs that already have ``search_text`` are skipped in phase A
(their vectors are still rewritten by the phase-B reembed, which is harmless).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_talk.database import BotTalkDB, DEFAULT_DB_PATH

NO_AUTO_EMBED: dict = {}


def main() -> None:
    db_path = os.environ.get("BOTTALK_DB_PATH", DEFAULT_DB_PATH)

    # ---- Phase A: write search_text without embedding ---------------------
    db = BotTalkDB(db_path=db_path, auto_embed=NO_AUTO_EMBED)
    db.open()
    docs = db.db.find({}).to_list()
    missing = [d for d in docs if not d.get("search_text")]
    t0 = time.time()
    for d in missing:
        st = BotTalkDB._build_search_text(d.get("summary", ""), d.get("body", ""))
        db.db.update_one({"_id": d["_id"]}, set={"search_text": st})
    print(f"phase A: wrote search_text to {len(missing)}/{len(docs)} docs in {time.time() - t0:.1f}s")
    db.close()

    # ---- Phase B: batched embedding via reembed ---------------------------
    db2 = BotTalkDB(db_path=db_path)  # full AUTO_EMBED_CONFIG
    db2.open()
    t0 = time.time()
    n = db2.reembed("search_text")
    print(f"phase B: reembedded search_text for {n} doc(s) in {time.time() - t0:.1f}s")

    db2.compact()

    docs2 = db2.db.find({}).to_list()
    n_se = sum(1 for d in docs2 if d.get("search_embedding"))
    lens = {len(d.get("search_embedding", [])) for d in docs2 if d.get("search_embedding")}
    print(f"verify: {n_se}/{len(docs2)} docs have search_embedding, lengths={lens}")
    print(f"stats:  {db2.stats()}")
    db2.close()


if __name__ == "__main__":
    main()
