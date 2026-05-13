"""
Allowed-contacts service.

Stores the list of contacts the assistant (Manus/LLM) is permitted to send
WhatsApp messages to. Backed by a JSON file under WA_STORE_PATH so the data
survives restarts without needing a schema migration.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


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


class AllowedContactsService:
    """JSON-backed CRUD for the assistant's allow-list of contacts."""

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        base = Path(storage_path or settings.WA_STORE_PATH)
        self._path = base / "allowed_contacts.json"
        self._lock = asyncio.Lock()
        self._enabled: bool = False
        self._contacts: List[Dict[str, Any]] = []
        self._loaded = False

    # ─── Load / Save ─────────────────────────────────────────────────────

    def _load_sync(self) -> None:
        """Read the JSON file into memory. Tolerates missing/corrupt files."""
        if not self._path.exists():
            self._enabled = False
            self._contacts = []
            self._loaded = True
            return

        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._enabled = bool(data.get("enabled", False))
            self._contacts = list(data.get("contacts", []))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to read {self._path}: {e}. Starting empty.")
            self._enabled = False
            self._contacts = []
        self._loaded = True

    def _save_sync(self) -> None:
        """Write the current state to disk atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"enabled": self._enabled, "contacts": self._contacts},
                fh,
                indent=2,
                ensure_ascii=False,
            )
        tmp.replace(self._path)

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_sync()

    # ─── Public state ────────────────────────────────────────────────────

    async def get_state(self) -> Dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            return {
                "enabled": self._enabled,
                "contacts": list(self._contacts),
            }

    async def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            self._enabled = bool(enabled)
            self._save_sync()
            return {"enabled": self._enabled, "contacts": list(self._contacts)}

    async def is_enabled(self) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            return self._enabled

    # ─── CRUD ────────────────────────────────────────────────────────────

    async def list_contacts(self) -> List[Dict[str, Any]]:
        async with self._lock:
            await self._ensure_loaded()
            return list(self._contacts)

    async def get_contact(self, contact_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            await self._ensure_loaded()
            return next((c for c in self._contacts if c["id"] == contact_id), None)

    async def add_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            phone = _normalize_phone(payload.get("phone", ""))
            if not phone:
                raise ValueError("phone is required and must contain digits")

            # Deduplicate by normalized phone (tolerant of country-code differences)
            if any(_phones_match(c.get("phone", ""), phone) for c in self._contacts):
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
            self._contacts.append(contact)
            self._save_sync()
            return contact

    async def update_contact(
        self, contact_id: str, patch: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            await self._ensure_loaded()
            contact = next((c for c in self._contacts if c["id"] == contact_id), None)
            if contact is None:
                return None

            for key in (
                "name",
                "relation",
                "notes",
                "enabled",
                "llm_friendly_names",
                "tags",
                "attributes",
            ):
                if key in patch and patch[key] is not None:
                    contact[key] = patch[key]

            if patch.get("phone") is not None:
                new_phone = _normalize_phone(patch["phone"])
                if not new_phone:
                    raise ValueError("phone must contain digits")
                if any(
                    c["id"] != contact_id and _phones_match(c.get("phone", ""), new_phone)
                    for c in self._contacts
                ):
                    raise ValueError(
                        f"Another contact already has phone {new_phone}"
                    )
                contact["phone"] = new_phone

            contact["updated_at"] = _now_iso()
            self._save_sync()
            return contact

    async def delete_contact(self, contact_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            before = len(self._contacts)
            self._contacts = [c for c in self._contacts if c["id"] != contact_id]
            changed = len(self._contacts) != before
            if changed:
                self._save_sync()
            return changed

    # ─── Enforcement ─────────────────────────────────────────────────────

    async def find_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """Return the allow-list entry matching a phone number, if any."""
        target = _normalize_phone(phone)
        if not target:
            return None
        async with self._lock:
            await self._ensure_loaded()
            return next(
                (c for c in self._contacts if _phones_match(c.get("phone", ""), target)),
                None,
            )

    async def check_allowed(self, phone: str) -> Dict[str, Any]:
        """
        Decide whether a send to `phone` should be permitted.

        When the master switch is OFF, every send is allowed and `contact` is
        whichever matching entry was found (or None). When it's ON, sends are
        only allowed if the phone matches an entry with `enabled=True`.
        """
        async with self._lock:
            await self._ensure_loaded()
            enforced = self._enabled
            target = _normalize_phone(phone)
            contact = (
                next(
                    (
                        c
                        for c in self._contacts
                        if _phones_match(c.get("phone", ""), target)
                    ),
                    None,
                )
                if target
                else None
            )

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
