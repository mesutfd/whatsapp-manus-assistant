"""
Allowed-contacts service.

Stores the list of contacts the assistant (Manus/LLM) is permitted to send
WhatsApp messages to, plus the master enforcement toggle. Backed by MongoDB
(collections `allowed_contacts` and `permissions_config`).
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.mongo import get_db

logger = logging.getLogger(__name__)

_CONFIG_ID = "singleton"


def _normalize_phone(phone: str) -> str:
    """Strip everything that is not a digit. Matches the WhatsApp client logic."""
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


# Minimum digit overlap before we accept a suffix match. National subscriber
# numbers are virtually always >= 8 digits, so this is conservative enough to
# avoid colliding two distinct contacts while still letting a stored
# "9358181152" match an incoming "989358181152" (and vice versa).
_PHONE_SUFFIX_MIN = 8


def _phones_match(stored: str, target: str) -> bool:
    """
    Compare two normalized phone numbers tolerantly.

    Exact-equal wins; otherwise we accept a match when the shorter number is a
    suffix of the longer one and overlaps by at least `_PHONE_SUFFIX_MIN`
    digits. This covers the common case where the allow-list stores a local
    number ("9358181152") and the API receives one with the country code
    prefixed ("989358181152"), or vice versa.
    """
    if not stored or not target:
        return False
    if stored == target:
        return True
    short, long_ = (stored, target) if len(stored) <= len(target) else (target, stored)
    if len(short) < _PHONE_SUFFIX_MIN:
        return False
    return long_.endswith(short)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


class AllowedContactsService:
    """MongoDB-backed CRUD for the assistant's allow-list of contacts."""

    def __init__(self) -> None:
        pass

    # ─── Public state ────────────────────────────────────────────────────

    async def get_state(self) -> Dict[str, Any]:
        db = get_db()
        cfg = await db.permissions_config.find_one({"_id": _CONFIG_ID})
        enabled = bool(cfg.get("enabled")) if cfg else False
        contacts = [_strip_mongo_id(c) async for c in db.allowed_contacts.find({})]
        return {"enabled": enabled, "contacts": contacts}

    async def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        db = get_db()
        await db.permissions_config.update_one(
            {"_id": _CONFIG_ID}, {"$set": {"enabled": bool(enabled)}}, upsert=True
        )
        return await self.get_state()

    async def is_enabled(self) -> bool:
        db = get_db()
        cfg = await db.permissions_config.find_one({"_id": _CONFIG_ID})
        return bool(cfg.get("enabled")) if cfg else False

    # ─── CRUD ────────────────────────────────────────────────────────────

    async def list_contacts(self) -> List[Dict[str, Any]]:
        db = get_db()
        return [_strip_mongo_id(c) async for c in db.allowed_contacts.find({})]

    async def get_contact(self, contact_id: str) -> Optional[Dict[str, Any]]:
        db = get_db()
        doc = await db.allowed_contacts.find_one({"id": contact_id})
        return _strip_mongo_id(doc)

    async def add_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        db = get_db()
        phone = _normalize_phone(payload.get("phone", ""))
        if not phone:
            raise ValueError("phone is required and must contain digits")

        # Deduplicate by normalized phone (tolerant of country-code differences)
        existing = [c async for c in db.allowed_contacts.find({})]
        if any(_phones_match(c.get("phone", ""), phone) for c in existing):
            raise ValueError(f"A contact with phone {phone} is already on the allow-list")

        now = _now_iso()
        contact = {
            "id": uuid.uuid4().hex,
            "name": payload.get("name", "").strip() or phone,
            "phone": phone,
            "relation": payload.get("relation"),
            "llm_friendly_names": list(payload.get("llm_friendly_names") or []),
            "tags": list(payload.get("tags") or []),
            "notes": payload.get("notes"),
            "attributes": dict(payload.get("attributes") or {}),
            "enabled": bool(payload.get("enabled", True)),
            "created_at": now,
            "updated_at": now,
        }
        await db.allowed_contacts.insert_one(contact)
        return _strip_mongo_id(contact)

    async def update_contact(
        self, contact_id: str, patch: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        db = get_db()
        contact = await db.allowed_contacts.find_one({"id": contact_id})
        if contact is None:
            return None

        updates: Dict[str, Any] = {}
        for key in (
            "name", "relation", "notes", "enabled",
            "llm_friendly_names", "tags", "attributes",
        ):
            if key in patch and patch[key] is not None:
                updates[key] = patch[key]

        if patch.get("phone") is not None:
            new_phone = _normalize_phone(patch["phone"])
            if not new_phone:
                raise ValueError("phone must contain digits")
            others = [c async for c in db.allowed_contacts.find({"id": {"$ne": contact_id}})]
            if any(_phones_match(c.get("phone", ""), new_phone) for c in others):
                raise ValueError(f"Another contact already has phone {new_phone}")
            updates["phone"] = new_phone

        updates["updated_at"] = _now_iso()
        await db.allowed_contacts.update_one({"id": contact_id}, {"$set": updates})
        return await self.get_contact(contact_id)

    async def delete_contact(self, contact_id: str) -> bool:
        db = get_db()
        result = await db.allowed_contacts.delete_one({"id": contact_id})
        return result.deleted_count > 0

    # ─── Enforcement ─────────────────────────────────────────────────────

    async def find_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """Return the allow-list entry matching a phone number, if any."""
        target = _normalize_phone(phone)
        if not target:
            return None
        db = get_db()
        async for c in db.allowed_contacts.find({}):
            if _phones_match(c.get("phone", ""), target):
                return _strip_mongo_id(c)
        return None

    async def check_allowed(self, phone: str) -> Dict[str, Any]:
        """
        Decide whether a send to `phone` should be permitted.

        When the master switch is OFF, every send is allowed and `contact` is
        whichever matching entry was found (or None). When it's ON, sends are
        only allowed if the phone matches an entry with `enabled=True`.
        """
        db = get_db()
        cfg = await db.permissions_config.find_one({"_id": _CONFIG_ID})
        enforced = bool(cfg.get("enabled")) if cfg else False
        target = _normalize_phone(phone)

        contact = await self.find_by_phone(target) if target else None

        if not enforced:
            return {
                "phone": target or phone,
                "allowed": True,
                "enforced": False,
                "contact": contact,
                "reason": None,
            }

        if not target:
            return {
                "phone": phone,
                "allowed": False,
                "enforced": True,
                "contact": None,
                "reason": "phone could not be parsed",
            }

        if contact is None:
            return {
                "phone": target,
                "allowed": False,
                "enforced": True,
                "contact": None,
                "reason": "not on the allow-list",
            }

        if not contact.get("enabled", True):
            return {
                "phone": target,
                "allowed": False,
                "enforced": True,
                "contact": contact,
                "reason": "contact is disabled in the allow-list",
            }

        return {
            "phone": target,
            "allowed": True,
            "enforced": True,
            "contact": contact,
            "reason": None,
        }


# Module-level singleton — all routers and the WhatsApp client share this.
allowed_contacts_service = AllowedContactsService()
