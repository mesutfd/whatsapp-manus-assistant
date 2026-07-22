#!/usr/bin/env python3
"""
One-time migration: copy existing SQLite (app.db, messages.db) and the JSON
allow-list (allowed_contacts.json) into MongoDB.

Run once, inside the app container, after the `mongo` service is up:

    docker exec ideep-whatsapp-bot python scripts/migrate_to_mongo.py

Safe to re-run: each collection is skipped (not overwritten) if it already
has documents in MongoDB, so a second run is a no-op rather than a
duplicate-inserting or data-clobbering operation.
"""

import json
import sqlite3
import sys
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402


def _rows(con: sqlite3.Connection, table: str):
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(f"SELECT * FROM {table}")
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in cur.fetchall()]


def migrate_app_db(mongo_db):
    path = Path(settings.APP_DB_PATH)
    if not path.exists():
        print(f"[skip] {path} not found — nothing to migrate from app.db")
        return

    con = sqlite3.connect(str(path))

    # assistant_config (singleton)
    if mongo_db.assistant_config.count_documents({}) == 0:
        rows = _rows(con, "assistant_config")
        if rows:
            row = rows[0]
            row.pop("id", None)
            row["_id"] = "singleton"
            for k in ("enabled", "llm_enabled", "quiet_hours_enabled", "quiet_hours_defer_scheduled"):
                if k in row:
                    row[k] = bool(row[k])
            mongo_db.assistant_config.insert_one(row)
            print("[ok] assistant_config migrated")
    else:
        print("[skip] assistant_config already has data in MongoDB")

    # auto_reply_rules
    if mongo_db.auto_reply_rules.count_documents({}) == 0:
        rows = _rows(con, "auto_reply_rules")
        max_id = 0
        for row in rows:
            row["use_llm"] = bool(row["use_llm"])
            row["enabled"] = bool(row["enabled"])
            max_id = max(max_id, int(row["id"]))
        if rows:
            mongo_db.auto_reply_rules.insert_many(rows)
            mongo_db.counters.update_one(
                {"_id": "rule_id"}, {"$max": {"seq": max_id}}, upsert=True
            )
            print(f"[ok] auto_reply_rules migrated ({len(rows)} rows)")
    else:
        print("[skip] auto_reply_rules already has data in MongoDB")

    # contact_personas
    if mongo_db.contact_personas.count_documents({}) == 0:
        rows = _rows(con, "contact_personas")
        for row in rows:
            row.pop("id", None)
            row["use_llm"] = bool(row["use_llm"])
        if rows:
            mongo_db.contact_personas.insert_many(rows)
            print(f"[ok] contact_personas migrated ({len(rows)} rows)")
    else:
        print("[skip] contact_personas already has data in MongoDB")

    # scheduled_sends
    if mongo_db.scheduled_sends.count_documents({}) == 0:
        rows = _rows(con, "scheduled_sends")
        max_id = 0
        for row in rows:
            max_id = max(max_id, int(row["id"]))
        if rows:
            mongo_db.scheduled_sends.insert_many(rows)
            mongo_db.counters.update_one(
                {"_id": "send_id"}, {"$max": {"seq": max_id}}, upsert=True
            )
            print(f"[ok] scheduled_sends migrated ({len(rows)} rows)")
    else:
        print("[skip] scheduled_sends already has data in MongoDB")

    # rule_cooldowns
    if mongo_db.rule_cooldowns.count_documents({}) == 0:
        rows = _rows(con, "rule_cooldowns")
        if rows:
            mongo_db.rule_cooldowns.insert_many(rows)
            print(f"[ok] rule_cooldowns migrated ({len(rows)} rows)")
    else:
        print("[skip] rule_cooldowns already has data in MongoDB")

    con.close()


def migrate_messages(mongo_db):
    path = Path(settings.MESSAGE_STORE_DB)
    if not path.exists():
        print(f"[skip] {path} not found — nothing to migrate from messages.db")
        return
    if mongo_db.messages.count_documents({}) != 0:
        print("[skip] messages already has data in MongoDB")
        return

    con = sqlite3.connect(str(path))
    rows = _rows(con, "messages")
    con.close()
    if not rows:
        print("[skip] messages.db has no rows")
        return

    docs = []
    for row in rows:
        row.pop("row_id", None)
        row["is_group"] = bool(row["is_group"])
        row["is_from_me"] = bool(row["is_from_me"])
        docs.append(row)
    mongo_db.messages.insert_many(docs)
    print(f"[ok] messages migrated ({len(docs)} rows)")


def migrate_allowed_contacts(mongo_db):
    path = Path(settings.WA_STORE_PATH) / "allowed_contacts.json"
    if not path.exists():
        print(f"[skip] {path} not found — nothing to migrate from allowed_contacts.json")
        return

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if mongo_db.permissions_config.count_documents({}) == 0:
        mongo_db.permissions_config.insert_one(
            {"_id": "singleton", "enabled": bool(data.get("enabled", False))}
        )
        print("[ok] permissions_config migrated")
    else:
        print("[skip] permissions_config already has data in MongoDB")

    if mongo_db.allowed_contacts.count_documents({}) == 0:
        contacts = data.get("contacts") or []
        if contacts:
            mongo_db.allowed_contacts.insert_many(contacts)
            print(f"[ok] allowed_contacts migrated ({len(contacts)} rows)")
    else:
        print("[skip] allowed_contacts already has data in MongoDB")


def main():
    print(f"Connecting to MongoDB at {settings.MONGO_URI} (db={settings.MONGO_DB_NAME})")
    client = MongoClient(settings.MONGO_URI)
    mongo_db = client[settings.MONGO_DB_NAME]

    migrate_app_db(mongo_db)
    migrate_messages(mongo_db)
    migrate_allowed_contacts(mongo_db)

    print("Migration complete.")


if __name__ == "__main__":
    main()
