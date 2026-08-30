#!/usr/bin/env python3
"""Migration: set ``status`` on every existing post (default ``active``).

BotTalk now tracks a post lifecycle status (``active`` / ``superseded`` /
``deprecated``).  All posts that predate the field are implicitly active; this
one-time backfill writes the field explicitly so status filtering (which uses
``$in`` and would otherwise *exclude* docs that lack the field) is correct for
old memories too.  Idempotent: docs that already have ``status`` are skipped.

Run with the bottalk service STOPPED:

    sudo systemctl stop bottalk
    .venv/bin/python tools/backfill_status.py
    sudo systemctl start bottalk
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_talk.database import BotTalkDB, DEFAULT_DB_PATH

NO_AUTO_EMBED: dict = {}


def main() -> None:
    db_path = os.environ.get("BOTTALK_DB_PATH", DEFAULT_DB_PATH)

    db = BotTalkDB(db_path=db_path, auto_embed=NO_AUTO_EMBED)
    db.open()
    docs = db.db.find({}).to_list()
    missing = [d for d in docs if not d.get("status")]
    for d in missing:
        # metadata write only — no embedding rebuild needed
        db.db.update_one({"_id": d["_id"]}, set={"status": "active"})
    db.compact()

    distinct = {d.get("status") for d in db.db.find({}).to_list()}
    print(f"backfill: set status='active' on {len(missing)}/{len(docs)} docs")
    print(f"verify:   distinct statuses now = {sorted(distinct)}")
    print(f"stats:    {db.stats()}")
    db.close()


if __name__ == "__main__":
    main()
