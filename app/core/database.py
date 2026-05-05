"""
SQLite persistence for app-level state: assistant config, auto-reply rules,
contact personas, scheduled sends. Uses aiosqlite so writes don't block the
event loop.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite

from app.core.config import settings

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    enabled         INTEGER NOT NULL DEFAULT 0,
    default_message TEXT NOT NULL DEFAULT '',
    assistant_name  TEXT NOT NULL DEFAULT 'iDeep AI',
    llm_enabled     INTEGER NOT NULL DEFAULT 0,
    llm_system_prompt TEXT NOT NULL DEFAULT '',
    quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,
    quiet_hours_start   TEXT NOT NULL DEFAULT '22:00',
    quiet_hours_end     TEXT NOT NULL DEFAULT '08:00',
    quiet_hours_timezone TEXT NOT NULL DEFAULT 'UTC',
    quiet_hours_message  TEXT NOT NULL DEFAULT '',
    quiet_hours_defer_scheduled INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auto_reply_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact         TEXT,
    keyword         TEXT,
    match_mode      TEXT NOT NULL DEFAULT 'contains',  -- contains|exact|starts_with|regex
    message         TEXT NOT NULL DEFAULT '',
    use_llm         INTEGER NOT NULL DEFAULT 0,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contact_personas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact         TEXT NOT NULL UNIQUE, -- phone digits or JID
    display_name    TEXT,
    notes           TEXT,                  -- short background fed into system prompt
    system_prompt_override TEXT,           -- optional full override
    use_llm         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scheduled_sends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phone           TEXT NOT NULL,
    message         TEXT NOT NULL,
    scheduled_at    TEXT NOT NULL,        -- ISO 8601 UTC
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|sent|failed|cancelled
    sent_at         TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_cooldowns (
    rule_id         INTEGER NOT NULL,
    contact         TEXT NOT NULL,
    last_fired_at   REAL NOT NULL,
    PRIMARY KEY (rule_id, contact)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled_sends(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON auto_reply_rules(enabled, priority);
"""


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


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


class AppDatabase:
    """Thin async wrapper around aiosqlite for app-level state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._initialized = False

    async def initialize(self) -> None:
        """Create the DB file and schema if missing, seed defaults."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(SCHEMA)
            # Seed singleton config row from .env defaults if missing.
            cur = await db.execute("SELECT id FROM assistant_config WHERE id = 1")
            existing = await cur.fetchone()
            if not existing:
                await db.execute(
                    """
                    INSERT INTO assistant_config (
                        id, enabled, default_message, assistant_name,
                        llm_enabled, llm_system_prompt,
                        quiet_hours_enabled, quiet_hours_start, quiet_hours_end,
                        quiet_hours_timezone, quiet_hours_message, quiet_hours_defer_scheduled
                    ) VALUES (1, ?, ?, ?, 0, ?, 0, '22:00', '08:00', ?, '', 1)
                    """,
                    (
                        1 if settings.AUTO_REPLY_ENABLED else 0,
                        settings.AUTO_REPLY_MESSAGE,
                        settings.ASSISTANT_NAME,
                        settings.LLM_SYSTEM_PROMPT,
                        settings.QUIET_HOURS_TIMEZONE,
                    ),
                )
                await db.commit()
                logger.info("Seeded assistant_config defaults into %s", self.db_path)

        self._initialized = True
        logger.info("App database ready at %s", self.db_path)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    # ─── Assistant config (singleton) ────────────────────────────────────

    async def get_config(self) -> Dict[str, Any]:
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM assistant_config WHERE id = 1")
            row = await cur.fetchone()
            return _row_to_dict(row) if row else {}

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
            clean[k] = (1 if bool(v) else 0) if k in bool_fields else v
        if not clean:
            return await self.get_config()

        sets = ", ".join(f"{k} = ?" for k in clean.keys())
        params = list(clean.values())
        async with self._conn() as db:
            await db.execute(
                f"UPDATE assistant_config SET {sets}, updated_at = datetime('now') WHERE id = 1",
                params,
            )
            await db.commit()
        return await self.get_config()

    # ─── Rules ───────────────────────────────────────────────────────────

    async def list_rules(self) -> List[Dict[str, Any]]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM auto_reply_rules ORDER BY priority ASC, id ASC"
            )
            rows = await cur.fetchall()
            return [_row_to_dict(r) for r in rows]

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
        async with self._conn() as db:
            cur = await db.execute(
                """
                INSERT INTO auto_reply_rules
                    (contact, keyword, match_mode, message, use_llm, cooldown_seconds, enabled, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact or None,
                    keyword or None,
                    match_mode,
                    message,
                    1 if use_llm else 0,
                    int(cooldown_seconds),
                    1 if enabled else 0,
                    int(priority),
                ),
            )
            await db.commit()
            new_id = cur.lastrowid
            cur = await db.execute("SELECT * FROM auto_reply_rules WHERE id = ?", (new_id,))
            row = await cur.fetchone()
            return _row_to_dict(row) if row else {}

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
            clean[k] = (1 if bool(v) else 0) if k in bool_fields else v
        if not clean:
            return await self.get_rule(rule_id)

        sets = ", ".join(f"{k} = ?" for k in clean.keys())
        params = list(clean.values()) + [rule_id]
        async with self._conn() as db:
            await db.execute(f"UPDATE auto_reply_rules SET {sets} WHERE id = ?", params)
            await db.commit()
        return await self.get_rule(rule_id)

    async def get_rule(self, rule_id: int) -> Optional[Dict[str, Any]]:
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM auto_reply_rules WHERE id = ?", (rule_id,))
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None

    async def delete_rule(self, rule_id: int) -> bool:
        async with self._conn() as db:
            cur = await db.execute("DELETE FROM auto_reply_rules WHERE id = ?", (rule_id,))
            await db.commit()
            return cur.rowcount > 0

    # ─── Cooldowns ───────────────────────────────────────────────────────

    async def get_cooldown(self, rule_id: int, contact: str) -> Optional[float]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT last_fired_at FROM rule_cooldowns WHERE rule_id = ? AND contact = ?",
                (rule_id, contact),
            )
            row = await cur.fetchone()
            return float(row["last_fired_at"]) if row else None

    async def touch_cooldown(self, rule_id: int, contact: str, ts: float) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO rule_cooldowns (rule_id, contact, last_fired_at)
                VALUES (?, ?, ?)
                ON CONFLICT(rule_id, contact) DO UPDATE SET last_fired_at = excluded.last_fired_at
                """,
                (rule_id, contact, ts),
            )
            await db.commit()

    # ─── Personas ────────────────────────────────────────────────────────

    async def list_personas(self) -> List[Dict[str, Any]]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM contact_personas ORDER BY display_name COLLATE NOCASE, contact"
            )
            rows = await cur.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def get_persona(self, contact: str) -> Optional[Dict[str, Any]]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM contact_personas WHERE contact = ?", (contact,)
            )
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None

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

        def _digits(s: Optional[str]) -> str:
            return "".join(ch for ch in (s or "") if ch.isdigit())

        digits = _digits(jid)
        digits_alt = _digits(jid_alt)
        # Strip common phone-formatting chars from the stored contact so a
        # row saved as '+1 (555) 123-4567' still matches digits '15551234567'
        # extracted from the JID. Existing rows that pre-date normalize-on-
        # write will hit this clause.
        normalized_sql = (
            "replace(replace(replace(replace(replace(replace("
            "contact, '+', ''), ' ', ''), '-', ''), '(', ''), ')', ''), '.', '')"
        )
        async with self._conn() as db:
            cur = await db.execute(
                f"""
                SELECT * FROM contact_personas
                WHERE contact = ?
                   OR (? != '' AND contact = ?)
                   OR (? != '' AND contact = ?)
                   OR (? != '' AND {normalized_sql} = ?)
                   OR (? != '' AND {normalized_sql} = ?)
                   OR (? != '' AND lower(display_name) = lower(?))
                   OR (? != '' AND lower(contact) = lower(?))
                LIMIT 1
                """,
                (
                    jid or "",
                    jid_alt or "", jid_alt or "",
                    digits, digits,
                    digits_alt, digits_alt,
                    pushname or "", pushname or "",
                    pushname or "", pushname or "",
                ),
            )
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None

    async def upsert_persona(
        self,
        contact: str,
        display_name: Optional[str] = None,
        notes: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        contact = _normalize_contact_key(contact)
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO contact_personas (contact, display_name, notes, system_prompt_override, use_llm)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contact) DO UPDATE SET
                    display_name = excluded.display_name,
                    notes = excluded.notes,
                    system_prompt_override = excluded.system_prompt_override,
                    use_llm = excluded.use_llm
                """,
                (contact, display_name, notes, system_prompt_override, 1 if use_llm else 0),
            )
            await db.commit()
        return await self.get_persona(contact) or {}

    async def delete_persona(self, contact: str) -> bool:
        contact = _normalize_contact_key(contact)
        async with self._conn() as db:
            cur = await db.execute("DELETE FROM contact_personas WHERE contact = ?", (contact,))
            await db.commit()
            return cur.rowcount > 0

    # ─── Scheduled sends ─────────────────────────────────────────────────

    async def list_scheduled(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        async with self._conn() as db:
            if status:
                cur = await db.execute(
                    "SELECT * FROM scheduled_sends WHERE status = ? ORDER BY scheduled_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cur = await db.execute(
                    "SELECT * FROM scheduled_sends ORDER BY scheduled_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cur.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def add_scheduled(self, phone: str, message: str, scheduled_at_iso: str) -> Dict[str, Any]:
        async with self._conn() as db:
            cur = await db.execute(
                """
                INSERT INTO scheduled_sends (phone, message, scheduled_at, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (phone, message, scheduled_at_iso),
            )
            await db.commit()
            new_id = cur.lastrowid
            cur = await db.execute("SELECT * FROM scheduled_sends WHERE id = ?", (new_id,))
            row = await cur.fetchone()
            return _row_to_dict(row) if row else {}

    async def claim_due_scheduled(self, now_iso: str) -> List[Dict[str, Any]]:
        """Return all pending sends due now (status remains 'pending' until updated)."""
        async with self._conn() as db:
            cur = await db.execute(
                """
                SELECT * FROM scheduled_sends
                WHERE status = 'pending' AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT 50
                """,
                (now_iso,),
            )
            rows = await cur.fetchall()
            return [_row_to_dict(r) for r in rows]

    async def mark_scheduled(self, send_id: int, status: str, error: Optional[str] = None, sent_at: Optional[str] = None) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                UPDATE scheduled_sends
                SET status = ?, error = ?, sent_at = COALESCE(?, sent_at)
                WHERE id = ?
                """,
                (status, error, sent_at, send_id),
            )
            await db.commit()

    async def cancel_scheduled(self, send_id: int) -> bool:
        async with self._conn() as db:
            cur = await db.execute(
                "UPDATE scheduled_sends SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (send_id,),
            )
            await db.commit()
            return cur.rowcount > 0


# Singleton, lazy-initialized in main.py lifespan.
db = AppDatabase(settings.APP_DB_PATH)
