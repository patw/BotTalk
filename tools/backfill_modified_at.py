#!/usr/bin/env python3
"""Migration: set ``modified_at`` on every existing post.

BotTalk orders the web list (and tag browse) by ``modified_at`` — the last time
a memory was created *or* updated.  Posts written before the field existed need
it backfilled to ``updated_at`` (if edited) else ``created_at``; without it the
"last modified" sort has nothing to read for those memories.

Normally **nothing is needed**: the server runs this automatically on startup
(``get_db()`` calls ``BotTalkDB.backfill_modified_at()``), so a plain
``git pull`` + restart migrates each copy.  This script is for when you want to
run it explicitly — many copies you don't control the restart of, a mounted DB,
or a pre-flight before starting a new version.

Idempotent: posts that already carry ``modified_at`` are skipped, so it is safe
to re-run.  Run with the bottalk service STOPPED:

    sudo systemctl stop bottalk
    .venv/bin/python tools/backfill_modified_at.py
    sudo systemctl start bottalk

Honours ``BOTTALK_DB_PATH`` (defaults to the repo's ``bottalk.bson``), so point
it at any copy's DB file.
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
    migrated = db.backfill_modified_at()
    db.compact()  # reclaim the superseded records the backfill wrote

    remaining = len(db.db.find({"modified_at": {"$exists": False}}).to_list())
    print(f"db:       {db_path}")
    print(f"backfill: set modified_at on {migrated}/{len(docs)} docs")
    print(f"verify:   docs still missing modified_at = {remaining}")
    print(f"stats:    {db.stats()}")
    db.close()


if __name__ == "__main__":
    main()
