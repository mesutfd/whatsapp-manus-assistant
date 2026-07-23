"""
Persistent storage for the WhatsApp message store, backed by MongoDB.

`WhatsAppClientManager._message_store` used to be a plain in-memory list,
wiped on every restart and only ever containing traffic seen while the
process was running. This module backs it with MongoDB so:
  - live messages survive restarts
  - historical messages (imported from a WhatsApp chat export, or the
    bulk all-chats backup import) show up in /messages/history,
    /messages/chat/{phone}, search, and LLM auto-reply context exactly
    like live ones.
"""

import logging
from typing import Any, Dict, List

from pymongo import UpdateOne

from app.core.config import settings
from app.core.mongo import ensure_indexes, get_db

logger = logging.getLogger(__name__)


def _record_to_doc(msg: Dict[str, Any], source: str) -> Dict[str, Any]:
    doc = {
        "msg_id": str(msg.get("id") or ""),
        "chat_jid": msg.get("chat_jid") or "unknown",
        "chat_name": msg.get("chat_name"),
        "from_jid": msg.get("from"),
        "from_phone": msg.get("from_phone"),
        "sender_name": msg.get("sender_name"),
        "text": msg.get("text"),
        "timestamp": msg.get("timestamp") or "",
        "is_group": bool(msg.get("is_group")),
        "is_from_me": bool(msg.get("is_from_me")),
        "type": msg.get("type") or "text",
        "source": source,
    }
    if msg.get("media"):
        doc["media"] = msg["media"]
    return doc


def _doc_to_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "id": doc.get("msg_id"),
        "from": doc.get("from_jid"),
        "from_phone": doc.get("from_phone"),
        "chat_jid": doc.get("chat_jid"),
        "sender_name": doc.get("sender_name"),
        "text": doc.get("text"),
        "timestamp": doc.get("timestamp"),
        "is_group": bool(doc.get("is_group")),
        "is_from_me": bool(doc.get("is_from_me")),
        "type": doc.get("type"),
        "source": doc.get("source"),
    }
    if doc.get("media"):
        record["media"] = doc["media"]
    return record


def _natural_key(msg: Dict[str, Any], source: str) -> Dict[str, Any]:
    doc = _record_to_doc(msg, source)
    return {"chat_jid": doc["chat_jid"], "msg_id": doc["msg_id"], "timestamp": doc["timestamp"]}


class MessageHistoryDB:
    """MongoDB-backed persisted message store."""

    def __init__(self, db_path: str):
        # Kept only for interface parity with the old constructor signature.
        self.db_path = db_path

    async def initialize(self) -> None:
        await ensure_indexes()
        logger.info("Message history ready (MongoDB: %s)", settings.MONGO_DB_NAME)

    async def insert(self, msg: Dict[str, Any], source: str = "live") -> None:
        """Persist one message record. Idempotent on (chat_jid, msg_id, timestamp)."""
        db = get_db()
        key = _natural_key(msg, source)
        await db.messages.update_one(
            key, {"$setOnInsert": _record_to_doc(msg, source)}, upsert=True
        )

    async def insert_many(self, msgs: List[Dict[str, Any]], source: str = "import") -> int:
        """Bulk-insert historical messages. Returns count actually inserted (dupes ignored)."""
        if not msgs:
            return 0
        db = get_db()
        ops = [
            UpdateOne(_natural_key(m, source), {"$setOnInsert": _record_to_doc(m, source)}, upsert=True)
            for m in msgs
        ]
        result = await db.messages.bulk_write(ops, ordered=False)
        return result.upserted_count

    async def load_recent(self, limit: int) -> List[Dict[str, Any]]:
        """Load the most recent `limit` messages across all chats, oldest first
        (matching the ordering `_message_store` expects — newest at the end)."""
        db = get_db()
        cursor = db.messages.find({}).sort("timestamp", -1).limit(limit)
        records = [_doc_to_record(d) async for d in cursor]
        records.reverse()
        return records

    async def count(self) -> int:
        db = get_db()
        return await db.messages.count_documents({})

    async def list_conversations(self, limit: int = 200) -> List[Dict[str, Any]]:
        """All chats with last-message preview, newest activity first."""
        db = get_db()
        pipeline = [
            {"$sort": {"chat_jid": 1, "timestamp": 1}},
            {"$group": {
                "_id": "$chat_jid",
                "last_text": {"$last": "$text"},
                "last_type": {"$last": "$type"},
                "last_timestamp": {"$last": "$timestamp"},
                "last_from_me": {"$last": "$is_from_me"},
                "is_group": {"$last": "$is_group"},
                "message_count": {"$sum": 1},
                "chat_name": {"$last": "$chat_name"},
                # [timestamp, name] of the newest incoming message; arrays sort
                # above null in BSON so $max picks the latest real pair.
                "partner_pair": {"$max": {"$cond": [
                    {"$eq": ["$is_from_me", False]},
                    ["$timestamp", "$sender_name"],
                    None,
                ]}},
            }},
            {"$sort": {"last_timestamp": -1}},
            {"$limit": limit},
        ]
        conversations = []
        async for doc in db.messages.aggregate(pipeline):
            jid = doc["_id"]
            partner = (doc.get("partner_pair") or [None, None])[1]
            is_group = bool(doc.get("is_group"))
            name = doc.get("chat_name") or (None if is_group else partner) or jid.split("@")[0]
            phone = jid.split("@")[0] if jid.endswith("@s.whatsapp.net") else None
            conversations.append({
                "chat_jid": jid,
                "name": name,
                "phone": phone,
                "is_group": is_group,
                "message_count": doc["message_count"],
                "last_text": doc.get("last_text"),
                "last_type": doc.get("last_type"),
                "last_timestamp": doc.get("last_timestamp"),
                "last_from_me": bool(doc.get("last_from_me")),
            })
        return conversations

    async def get_chat(
        self, chat_jid: str, limit: int = 50, before: str = ""
    ) -> List[Dict[str, Any]]:
        """Messages of one chat, oldest-first. `before` (a timestamp string)
        pages backwards through history."""
        db = get_db()
        query: Dict[str, Any] = {"chat_jid": chat_jid}
        if before:
            query["timestamp"] = {"$lt": before}
        cursor = db.messages.find(query).sort("timestamp", -1).limit(limit)
        records = [_doc_to_record(d) async for d in cursor]
        records.reverse()
        return records

    async def get_chat_multi(self, chat_jids: List[str], limit: int = 50) -> List[Dict[str, Any]]:
        """Most recent messages across several JID forms of the same chat
        (phone-form, LID-form), oldest-first. Used to build LLM context, where
        a chat's messages may be stored under either addressing form."""
        jids = [j for j in chat_jids if j]
        if not jids:
            return []
        db = get_db()
        cursor = (
            db.messages.find({"chat_jid": {"$in": jids}})
            .sort("timestamp", -1)
            .limit(limit)
        )
        records = [_doc_to_record(d) async for d in cursor]
        records.reverse()
        return records

    async def clear_all(self) -> Dict[str, int]:
        """Wipe the whole message store AND all stored media binaries.
        Used before a full re-import from backup."""
        from app.core import media_store

        db = get_db()
        messages = await db.messages.count_documents({})
        await db.messages.drop()
        media_files = await media_store.delete_all()
        await ensure_indexes()
        return {"messages_deleted": messages, "media_files_deleted": media_files}


# Singleton, initialized via wa_client.initialize() in main.py's lifespan.
message_history_db = MessageHistoryDB(settings.MESSAGE_STORE_DB)
