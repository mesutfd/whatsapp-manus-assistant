"""
MongoDB-backed persistence for app-level state: assistant config, auto-reply
rules, contact personas, scheduled sends, rule cooldowns.

Public interface is kept identical to the previous SQLite-backed version so
none of the API routers needed to change. `auto_reply_rules` and
`scheduled_sends` keep small integer `id`s (via app.core.mongo.next_sequence)
because FastAPI path params elsewhere are typed `rule_id: int` / `send_id: int`.
"""

import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.mongo import ensure_indexes, get_db, next_sequence

logger = logging.getLogger(__name__)

_CONFIG_ID = "singleton"


def _normalize_contact_key(contact: str) -> str:
    """
    Canonicalize a persona contact key so phone-shaped inputs match what gets
    extracted from an incoming JID.

    JIDs (containing '@') are kept as-is. Anything else: if it contains at
    least one digit, strip non-digits — '+1 (555) 123-4567' becomes
    '15551234567', the form that matches digits extracted from the sender's
    JID. Non-numeric contacts (e.g. a pushname typed in directly) are
    returned unchanged.
    """
    if not contact:
        return contact
    s = contact.strip()
    if "@" in s:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits if digits else s


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


class AppDatabase:
    """MongoDB-backed app state, drop-in replacement for the old AppDatabase."""

    def __init__(self, db_path: str):
        # Kept only for interface parity with the old constructor signature.
        self.db_path = db_path

    async def initialize(self) -> None:
        await ensure_indexes()
        db = get_db()
        existing = await db.assistant_config.find_one({"_id": _CONFIG_ID})
        if not existing:
            await db.assistant_config.insert_one({
                "_id": _CONFIG_ID,
                "enabled": bool(settings.AUTO_REPLY_ENABLED),
                "default_message": settings.AUTO_REPLY_MESSAGE,
                "assistant_name": settings.ASSISTANT_NAME,
                "llm_enabled": False,
                "llm_system_prompt": settings.LLM_SYSTEM_PROMPT,
                "quiet_hours_enabled": False,
                "quiet_hours_start": "22:00",
                "quiet_hours_end": "08:00",
                "quiet_hours_timezone": settings.QUIET_HOURS_TIMEZONE,
                "quiet_hours_message": "",
                "quiet_hours_defer_scheduled": True,
                "updated_at": None,
            })
            logger.info("Seeded assistant_config defaults into MongoDB")
        logger.info("App database ready (MongoDB: %s)", settings.MONGO_DB_NAME)

    # ─── Assistant config (singleton) ────────────────────────────────────

    async def get_config(self) -> Dict[str, Any]:
        db = get_db()
        doc = await db.assistant_config.find_one({"_id": _CONFIG_ID})
        if not doc:
            return {}
        doc = dict(doc)
        doc["id"] = 1
        doc.pop("_id", None)
        return doc

    async def update_config(self, **fields: Any) -> Dict[str, Any]:
        if not fields:
            return await self.get_config()

        allowed = {
            "enabled", "default_message", "assistant_name",
            "llm_enabled", "llm_system_prompt",
            "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end",
            "quiet_hours_timezone", "quiet_hours_message", "quiet_hours_defer_scheduled",
        }
        bool_fields = {"enabled", "llm_enabled", "quiet_hours_enabled", "quiet_hours_defer_scheduled"}
        clean: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            clean[k] = bool(v) if k in bool_fields else v
        if not clean:
            return await self.get_config()

        from datetime import datetime
        clean["updated_at"] = datetime.utcnow().isoformat()

        db = get_db()
        await db.assistant_config.update_one({"_id": _CONFIG_ID}, {"$set": clean})
        return await self.get_config()

    # ─── Rules ───────────────────────────────────────────────────────────

    async def list_rules(self) -> List[Dict[str, Any]]:
        db = get_db()
        cursor = db.auto_reply_rules.find({}).sort([("priority", 1), ("id", 1)])
        return [_strip_mongo_id(d) async for d in cursor]

    async def add_rule(
        self,
        contact: Optional[str],
        keyword: Optional[str],
        message: str,
        match_mode: str = "contains",
        use_llm: bool = False,
        cooldown_seconds: int = 0,
        enabled: bool = True,
        priority: int = 100,
    ) -> Dict[str, Any]:
        from datetime import datetime

        db = get_db()
        new_id = await next_sequence("rule_id")
        doc = {
            "id": new_id,
            "contact": contact or None,
            "keyword": keyword or None,
            "match_mode": match_mode,
            "message": message,
            "use_llm": bool(use_llm),
            "cooldown_seconds": int(cooldown_seconds),
            "enabled": bool(enabled),
            "priority": int(priority),
            "created_at": datetime.utcnow().isoformat(),
        }
        await db.auto_reply_rules.insert_one(doc)
        return _strip_mongo_id(doc)

    async def update_rule(self, rule_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            "contact", "keyword", "match_mode", "message",
            "use_llm", "cooldown_seconds", "enabled", "priority",
        }
        bool_fields = {"use_llm", "enabled"}
        clean: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            clean[k] = bool(v) if k in bool_fields else v
        if not clean:
            return await self.get_rule(rule_id)

        db = get_db()
        await db.auto_reply_rules.update_one({"id": rule_id}, {"$set": clean})
        return await self.get_rule(rule_id)

    async def get_rule(self, rule_id: int) -> Optional[Dict[str, Any]]:
        db = get_db()
        doc = await db.auto_reply_rules.find_one({"id": rule_id})
        return _strip_mongo_id(doc)

    async def delete_rule(self, rule_id: int) -> bool:
        db = get_db()
        result = await db.auto_reply_rules.delete_one({"id": rule_id})
        return result.deleted_count > 0

    # ─── Cooldowns ───────────────────────────────────────────────────────

    async def get_cooldown(self, rule_id: int, contact: str) -> Optional[float]:
        db = get_db()
        doc = await db.rule_cooldowns.find_one({"rule_id": rule_id, "contact": contact})
        return float(doc["last_fired_at"]) if doc else None

    async def touch_cooldown(self, rule_id: int, contact: str, ts: float) -> None:
        db = get_db()
        await db.rule_cooldowns.update_one(
            {"rule_id": rule_id, "contact": contact},
            {"$set": {"last_fired_at": ts}},
            upsert=True,
        )

    # ─── Personas ────────────────────────────────────────────────────────

    async def list_personas(self) -> List[Dict[str, Any]]:
        db = get_db()
        cursor = db.contact_personas.find({}).collation(
            {"locale": "en", "strength": 2}
        ).sort([("display_name", 1), ("contact", 1)])
        return [_strip_mongo_id(d) async for d in cursor]

    async def get_persona(self, contact: str) -> Optional[Dict[str, Any]]:
        db = get_db()
        doc = await db.contact_personas.find_one({"contact": contact})
        return _strip_mongo_id(doc)

    async def find_persona_for_jid(
        self,
        jid: str,
        pushname: Optional[str] = None,
        jid_alt: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Match a persona by, in order of preference:
            1. Exact JID (Sender, then SenderAlt if provided)
            2. Phone digits extracted from JID (works for @s.whatsapp.net) —
               tried for both Sender and SenderAlt, so a LID-addressed message
               whose Sender is `<opaque>@lid` still matches via the
               `<phone>@s.whatsapp.net` form in SenderAlt.
            3. Pushname == display_name (case-insensitive) — needed for @lid JIDs
               with no SenderAlt and no phone-form persona key.
            4. Pushname == contact (case-insensitive).
        """
        if not jid and not jid_alt and not pushname:
            return None

        db = get_db()
        ci = {"locale": "en", "strength": 2}

        def _digits(s: Optional[str]) -> str:
            return "".join(ch for ch in (s or "") if ch.isdigit())

        # 1. Exact JID
        for candidate in (jid, jid_alt):
            if candidate:
                doc = await db.contact_personas.find_one({"contact": candidate})
                if doc:
                    return _strip_mongo_id(doc)

        # 2. Digits-normalized contact match (handles stored '+1 (555)...' etc.)
        for digits in (_digits(jid), _digits(jid_alt)):
            if digits:
                cursor = db.contact_personas.find({})
                async for doc in cursor:
                    if _normalize_contact_key(doc.get("contact", "")) == digits:
                        return _strip_mongo_id(doc)

        # 3. Pushname == display_name
        if pushname:
            doc = await db.contact_personas.find_one(
                {"display_name": pushname}, collation=ci
            )
            if doc:
                return _strip_mongo_id(doc)

            # 4. Pushname == contact
            doc = await db.contact_personas.find_one(
                {"contact": pushname}, collation=ci
            )
            if doc:
                return _strip_mongo_id(doc)

        return None

    async def upsert_persona(
        self,
        contact: str,
        display_name: Optional[str] = None,
        notes: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        contact = _normalize_contact_key(contact)
        db = get_db()
        await db.contact_personas.update_one(
            {"contact": contact},
            {
                "$set": {
                    "display_name": display_name,
                    "notes": notes,
                    "system_prompt_override": system_prompt_override,
                    "use_llm": bool(use_llm),
                },
                "$setOnInsert": {"contact": contact},
            },
            upsert=True,
        )
        return await self.get_persona(contact) or {}

    async def delete_persona(self, contact: str) -> bool:
        contact = _normalize_contact_key(contact)
        db = get_db()
        result = await db.contact_personas.delete_one({"contact": contact})
        return result.deleted_count > 0

    # ─── Scheduled sends ─────────────────────────────────────────────────

    async def list_scheduled(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        db = get_db()
        query = {"status": status} if status else {}
        cursor = db.scheduled_sends.find(query).sort("scheduled_at", -1).limit(limit)
        return [_strip_mongo_id(d) async for d in cursor]

    async def add_scheduled(self, phone: str, message: str, scheduled_at_iso: str) -> Dict[str, Any]:
        from datetime import datetime

        db = get_db()
        new_id = await next_sequence("send_id")
        doc = {
            "id": new_id,
            "phone": phone,
            "message": message,
            "scheduled_at": scheduled_at_iso,
            "status": "pending",
            "sent_at": None,
            "error": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        await db.scheduled_sends.insert_one(doc)
        return _strip_mongo_id(doc)

    async def claim_due_scheduled(self, now_iso: str) -> List[Dict[str, Any]]:
        """Return all pending sends due now (status remains 'pending' until updated)."""
        db = get_db()
        cursor = (
            db.scheduled_sends.find({"status": "pending", "scheduled_at": {"$lte": now_iso}})
            .sort("scheduled_at", 1)
            .limit(50)
        )
        return [_strip_mongo_id(d) async for d in cursor]

    async def mark_scheduled(self, send_id: int, status: str, error: Optional[str] = None, sent_at: Optional[str] = None) -> None:
        db = get_db()
        update: Dict[str, Any] = {"status": status, "error": error}
        if sent_at is not None:
            update["sent_at"] = sent_at
        await db.scheduled_sends.update_one({"id": send_id}, {"$set": update})

    async def cancel_scheduled(self, send_id: int) -> bool:
        db = get_db()
        result = await db.scheduled_sends.update_one(
            {"id": send_id, "status": "pending"},
            {"$set": {"status": "cancelled"}},
        )
        return result.modified_count > 0


# Singleton, lazy-initialized in main.py lifespan.
db = AppDatabase(settings.APP_DB_PATH)
