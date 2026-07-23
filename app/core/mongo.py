"""
Shared MongoDB connection, index setup, and the auto-increment counter helper
used by collections that need small integer ids (auto_reply_rules,
scheduled_sends) for backwards compatibility with existing API contracts
(e.g. `rule_id: int` / `send_id: int` path params).
"""

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


def get_db() -> AsyncIOMotorDatabase:
    """Lazily create the Motor client/database handle (cheap, no I/O yet)."""
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(settings.MONGO_URI)
        _db = _client[settings.MONGO_DB_NAME]
    return _db


async def next_sequence(name: str) -> int:
    """Atomically allocate the next integer id for `name` (e.g. 'rule_id')."""
    db = get_db()
    doc = await db.counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


async def set_sequence_if_higher(name: str, value: int) -> None:
    """Bump a counter up to at least `value` (used by the one-time SQLite
    migration so post-migration inserts don't collide with imported ids)."""
    db = get_db()
    await db.counters.update_one(
        {"_id": name},
        [{"$set": {"seq": {"$max": ["$seq", value]}}}],
        upsert=True,
    )


async def ensure_indexes() -> None:
    """Create all indexes this app relies on. Safe to call every startup."""
    db = get_db()

    await db.auto_reply_rules.create_index("id", unique=True)
    await db.auto_reply_rules.create_index([("priority", 1), ("id", 1)])

    await db.contact_personas.create_index("contact", unique=True)

    await db.scheduled_sends.create_index("id", unique=True)
    await db.scheduled_sends.create_index([("status", 1), ("scheduled_at", 1)])

    await db.rule_cooldowns.create_index([("rule_id", 1), ("contact", 1)], unique=True)

    await db.messages.create_index(
        [("chat_jid", 1), ("msg_id", 1), ("timestamp", 1)], unique=True
    )
    await db.messages.create_index([("chat_jid", 1), ("timestamp", 1)])
    await db.messages.create_index([("from_phone", 1), ("timestamp", 1)])

    await db.allowed_contacts.create_index("id", unique=True)

    await db.muted_chats.create_index("chat_key", unique=True)

    logger.info("MongoDB indexes ensured on %s", settings.MONGO_DB_NAME)
