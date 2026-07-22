"""
WhatsApp Client Manager - Core integration with Neonize library.
Handles connection, QR code generation, message sending/receiving, and session management.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import qrcode
import segno
from neonize.aioze.client import NewAClient
from neonize.aioze.events import (
    ConnectedEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    QREv,
    ReceiptEv,
    StreamReplacedEv,
)
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    Message as WAMessage,
)
from neonize.utils.jid import JIDToNonAD, build_jid, Jid2String

from app.core.config import settings

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    QR_READY = "qr_ready"
    PAIR_CODE_READY = "pair_code_ready"
    CONNECTED = "connected"
    LOGGED_OUT = "logged_out"


class WhatsAppClientManager:
    """
    Manages the WhatsApp client lifecycle including connection,
    authentication, message handling, and event routing.
    """

    def __init__(self):
        self._client: Optional[NewAClient] = None
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._qr_data: Optional[str] = None
        self._qr_base64: Optional[str] = None
        self._pair_code: Optional[str] = None
        self._phone_number: Optional[str] = None
        self._connected_at: Optional[datetime] = None
        self._device_info: Optional[Dict] = None
        self._message_handlers: List[Callable] = []
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._message_store: List[Dict] = []
        self._contacts_cache: Dict[str, Dict] = {}
        # LID (WhatsApp's privacy-mode addressing, `<opaque>@lid`) -> real
        # phone number, resolved from whatsmeow's own lid_map table and
        # cached in-process since the mapping never changes for a given LID.
        self._lid_pn_cache: Dict[str, str] = {}
        self._is_running: bool = False
        self._connection_task: Optional[asyncio.Task] = None
        # Periodic watcher that detects silent session expiry (whatsmeow keeps
        # the TCP up but is_logged_in flips to False) and forces a re-link.
        self._health_task: Optional[asyncio.Task] = None
        self._health_check_interval_s: int = 60
        # Drives the "send is failing -> session is probably broken" heuristic.
        # Whatsmeow returns a non-zero message id even when the recipient never
        # gets the message (e.g. device-list/Signal session is stale). Tracking
        # this lets us surface "session likely broken; please re-link" instead
        # of repeatedly reporting false success.
        self._consecutive_send_no_receipt: int = 0
        self._last_sent_id: Optional[str] = None
        self._last_sent_at: Optional[float] = None
        self._last_receipt_at: Optional[float] = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        """Cheap, synchronous check used by request handlers.

        Reflects only our cached state, which is kept in sync by event handlers
        (Connected/Disconnected/LoggedOut/StreamReplaced) and by the periodic
        _health_check_loop. We deliberately do NOT call _client.is_logged_in
        here because that is an async accessor on the aioze client (it returns
        a coroutine via asyncio.to_thread); calling it synchronously would
        always be truthy and would also leak un-awaited coroutines.
        """
        return self._state == ConnectionState.CONNECTED

    async def _check_logged_in(self) -> bool:
        """Authoritative async check against the underlying whatsmeow client."""
        if self._client is None:
            return False
        try:
            logged_in = await self._client.is_logged_in
            connected = await self._client.is_connected
            return bool(logged_in and connected)
        except Exception as e:
            logger.warning(f"Logged-in check raised: {e}")
            return False

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Normalize a phone number to digits only (no '+', spaces, dashes, parens)."""
        if not phone:
            return ""
        digits = re.sub(r"\D", "", phone)
        return digits

    def _resolve_lid_phone(self, lid_jid: str) -> Optional[str]:
        """
        Resolve a `<opaque>@lid` JID to the real `<phone>@s.whatsapp.net` JID
        using whatsmeow's own lid<->phone-number map (whatsmeow_lid_map),
        which it populates automatically from WhatsApp protocol metadata
        (contact sync, message SenderAlt, etc.) — independent of whatever
        SenderAlt this specific message did or didn't carry.

        Returns None if the JID isn't a LID or the mapping isn't known yet.
        """
        if not lid_jid or "@lid" not in lid_jid:
            return None
        lid_user = lid_jid.split("@", 1)[0]
        if lid_user in self._lid_pn_cache:
            return self._lid_pn_cache[lid_user]
        try:
            with sqlite3.connect(settings.WA_DATABASE_PATH, timeout=2.0) as con:
                cur = con.execute(
                    "SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid_user,)
                )
                row = cur.fetchone()
        except Exception as e:
            logger.debug("LID map lookup failed for %s: %s", lid_user, e)
            return None
        if not row or not row[0]:
            return None
        resolved = f"{row[0]}@s.whatsapp.net"
        self._lid_pn_cache[lid_user] = resolved
        return resolved

    @property
    def qr_data(self) -> Optional[str]:
        return self._qr_data

    @property
    def qr_base64(self) -> Optional[str]:
        return self._qr_base64

    @property
    def pair_code(self) -> Optional[str]:
        return self._pair_code

    @property
    def device_info(self) -> Optional[Dict]:
        return self._device_info

    @property
    def connected_at(self) -> Optional[datetime]:
        return self._connected_at

    def _generate_qr_base64(self, data: str) -> str:
        """Generate QR code image as base64 string."""
        qr = qrcode.QRCode(version=None, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    async def _sync_settings_from_db(self) -> None:
        """Mirror DB-stored config into in-process settings on startup."""
        try:
            from app.core.database import db

            cfg = await db.get_config()
            if cfg:
                settings.AUTO_REPLY_ENABLED = bool(cfg.get("enabled"))
                if cfg.get("default_message"):
                    settings.AUTO_REPLY_MESSAGE = cfg["default_message"]
                if cfg.get("assistant_name"):
                    settings.ASSISTANT_NAME = cfg["assistant_name"]
        except Exception as e:
            logger.warning(f"Could not sync assistant config from DB: {e}")

    async def _load_message_store_from_db(self) -> None:
        """Hydrate the in-memory message store from persisted history so
        restarts (and any imported historical messages) survive."""
        if not settings.MESSAGE_STORE_ENABLED:
            return
        try:
            from app.core.message_history import message_history_db

            await message_history_db.initialize()
            self._message_store = await message_history_db.load_recent(settings.MAX_STORED_MESSAGES)
            logger.info("Loaded %d messages from persisted history", len(self._message_store))
        except Exception as e:
            logger.warning(f"Could not load persisted message history: {e}")

    async def initialize(self):
        """Initialize the WhatsApp client with event handlers."""
        logger.info("Initializing WhatsApp client...")
        await self._sync_settings_from_db()
        await self._load_message_store_from_db()

        self._client = NewAClient(
            settings.WA_DATABASE_PATH,
            uuid=settings.WA_SESSION_NAME,
        )

        # Override neonize's default terminal QR printer. The default uses
        # segno's compact mode (Unicode half-blocks), which renders unscannably
        # in many terminals. Use full blocks for a reliably scannable QR and
        # point users to the web UI as the recommended path.
        @self._client.qr
        async def on_qr_terminal(client: NewAClient, data_qr: bytes):
            try:
                qr_str = data_qr.decode("utf-8") if isinstance(data_qr, (bytes, bytearray)) else str(data_qr)
                self._qr_data = qr_str
                self._qr_base64 = self._generate_qr_base64(qr_str)
                self._state = ConnectionState.QR_READY
                print("\n" + "=" * 60)
                print("Scan this QR with WhatsApp > Linked Devices > Link a device")
                print("(Or open the web UI for a sharper QR image)")
                print("=" * 60)
                segno.make_qr(qr_str).terminal(compact=False, border=2)
                print("=" * 60 + "\n")
                await self._emit_event("qr", {"qr_data": qr_str})
            except Exception as e:
                logger.error(f"Failed to render terminal QR: {e}")

        @self._client.event(PairStatusEv)
        async def on_pair_status(client: NewAClient, event: PairStatusEv):
            logger.info(f"Pair status event received")
            await self._emit_event("pair_status", {"status": "paired"})

        @self._client.event(ConnectedEv)
        async def on_connected(client: NewAClient, event: ConnectedEv):
            logger.info("WhatsApp connected successfully!")
            self._state = ConnectionState.CONNECTED
            self._connected_at = datetime.utcnow()
            self._qr_data = None
            self._qr_base64 = None
            self._pair_code = None
            self._consecutive_send_no_receipt = 0
            self._device_info = {
                "connected_at": self._connected_at.isoformat(),
            }
            self._start_health_watcher()
            await self._emit_event("connected", self._device_info)

        @self._client.event(DisconnectedEv)
        async def on_disconnected(client: NewAClient, event: DisconnectedEv):
            logger.warning("WhatsApp disconnected")
            self._state = ConnectionState.DISCONNECTED
            self._connected_at = None
            await self._emit_event("disconnected", {})

        @self._client.event(LoggedOutEv)
        async def on_logged_out(client: NewAClient, event: LoggedOutEv):
            logger.warning("WhatsApp session logged out (device unlinked from phone)")
            self._stop_health_watcher()
            # Wipe the DB so the next /connect produces a fresh QR rather than
            # re-binding the now-invalid device record.
            try:
                await self.logout()
            except Exception as e:
                logger.error(f"Auto-logout after LoggedOutEv failed: {e}")
            await self._emit_event("logged_out", {})

        @self._client.event(StreamReplacedEv)
        async def on_stream_replaced(client: NewAClient, event: StreamReplacedEv):
            logger.warning("WhatsApp stream replaced (logged in elsewhere)")
            self._state = ConnectionState.DISCONNECTED
            self._connected_at = None
            await self._emit_event("stream_replaced", {})

        @self._client.event(MessageEv)
        async def on_message(client: NewAClient, event: MessageEv):
            await self._handle_incoming_message(client, event)

        @self._client.event(ReceiptEv)
        async def on_receipt(client: NewAClient, event: ReceiptEv):
            # Any server/delivery/read receipt is proof the session is healthy.
            self._consecutive_send_no_receipt = 0
            self._last_receipt_at = time.time()
            await self._emit_event("receipt", {
                "type": str(event.Type),
                "timestamp": str(event.Timestamp),
            })

        logger.info("WhatsApp client initialized with all event handlers")

    async def connect(self):
        """Start the WhatsApp client connection."""
        if self._is_running:
            logger.warning("Client is already running")
            return

        if self._client is None:
            await self.initialize()

        # Reset transient state before a fresh connect attempt
        self._qr_data = None
        self._qr_base64 = None
        self._pair_code = None
        self._state = ConnectionState.CONNECTING
        self._is_running = True

        try:
            logger.info("Connecting WhatsApp client...")
            await self._client.connect()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            self._state = ConnectionState.DISCONNECTED
            self._is_running = False
            raise

    async def disconnect(self):
        """Disconnect the WhatsApp client."""
        if self._client and self._is_running:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.error(f"Disconnect error: {e}")
            finally:
                self._is_running = False
                self._state = ConnectionState.DISCONNECTED
                self._stop_health_watcher()
                logger.info("WhatsApp client disconnected")

    async def logout(self) -> Dict[str, Any]:
        """
        Log out of WhatsApp completely: unlink the device on the server side
        (if possible), tear down the local connection, and wipe the local
        session database so the next connect() will issue a fresh QR.
        """
        # 1) Try a clean server-side logout first (only meaningful while logged in).
        if self._client is not None:
            try:
                # is_logged_in is async on the aioze client (asyncio.to_thread).
                if await self._client.is_logged_in:
                    await self._client.logout()
                    logger.info("Server-side logout succeeded")
            except Exception as e:
                # Non-fatal — we'll still wipe local state below.
                logger.warning(f"Server-side logout failed (continuing): {e}")

            try:
                await self._client.disconnect()
            except Exception as e:
                logger.warning(f"Disconnect during logout failed (continuing): {e}")

        self._stop_health_watcher()
        self._is_running = False
        self._state = ConnectionState.LOGGED_OUT
        self._connected_at = None
        self._device_info = None
        self._qr_data = None
        self._qr_base64 = None
        self._pair_code = None
        # Drop the cached client so the next connect() re-initializes against
        # the fresh DB and re-registers handlers.
        self._client = None

        # 2) Wipe the local session database so the next connect() forces a fresh QR.
        wiped = []
        db_path = Path(settings.WA_DATABASE_PATH)
        for candidate in (db_path, db_path.with_suffix(db_path.suffix + "-wal"),
                          db_path.with_suffix(db_path.suffix + "-shm")):
            try:
                if candidate.exists():
                    candidate.unlink()
                    wiped.append(str(candidate))
            except Exception as e:
                logger.warning(f"Failed to remove {candidate}: {e}")

        await self._emit_event("logged_out", {"wiped": wiped})
        logger.info(f"Local session wiped: {wiped}")
        return {"wiped": wiped}

    # ─── Session Health Watcher ──────────────────────────────────────────
    #
    # WhatsApp sessions can go stale silently: whatsmeow keeps the TCP up and
    # IsConnected() returns true, but the device has been unlinked or the
    # Signal session is broken — sends still get a server-side message ID but
    # the recipient never receives anything (your "successful sent but nothing
    # arrives" symptom after a few days).
    #
    # The watcher polls is_logged_in periodically. If it flips to False we
    # transition to LOGGED_OUT so the UI demands a re-link instead of letting
    # callers keep firing into the void.

    def _start_health_watcher(self) -> None:
        if self._health_task and not self._health_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._health_task = loop.create_task(self._health_check_loop())

    def _stop_health_watcher(self) -> None:
        task = self._health_task
        self._health_task = None
        if task and not task.done():
            task.cancel()

    async def _health_check_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._health_check_interval_s)
                if self._state != ConnectionState.CONNECTED:
                    continue
                ok = await self._check_logged_in()
                if not ok:
                    logger.warning(
                        "Health check: session is no longer logged in. "
                        "Wiping local state so the UI prompts a re-link."
                    )
                    await self._emit_event(
                        "session_expired",
                        {"reason": "is_logged_in=False at scheduled health check"},
                    )
                    try:
                        await self.logout()
                    except Exception as e:
                        logger.error(f"Auto-logout after stale session failed: {e}")
                    # Stop polling — a fresh /connect will start a new watcher.
                    self._health_task = None
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Health check loop crashed: {e}")

    async def get_pair_code(self, phone_number: str) -> Optional[str]:
        """Get a pair code for linking via phone number (alternative to QR)."""
        if self._client is None:
            await self.initialize()

        normalized = self.normalize_phone(phone_number)
        if not normalized:
            logger.error(f"Invalid phone for pair code: {phone_number!r}")
            return None

        # PairPhone requires an active (un-logged-in) connection task. Start one
        # if the client isn't already running, otherwise the call hangs.
        if not self._is_running:
            self._state = ConnectionState.CONNECTING
            self._is_running = True
            try:
                await self._client.connect()
            except Exception as e:
                logger.error(f"Connect-for-paircode failed: {e}")
                self._is_running = False
                self._state = ConnectionState.DISCONNECTED
                return None

        try:
            self._phone_number = normalized
            code = await self._client.PairPhone(normalized, True)
            self._pair_code = code
            self._state = ConnectionState.PAIR_CODE_READY
            logger.info(f"Pair code generated for {normalized}: {code}")
            return code
        except Exception as e:
            logger.error(f"Failed to get pair code: {e}")
            return None

    async def send_message(self, phone: str, message: str) -> Dict[str, Any]:
        """Send a text message to a phone number.

        Validates that the number is registered on WhatsApp and that the
        server actually accepted the send (non-zero Timestamp/ID) before
        returning success=True. This prevents the "shows sent but never
        arrives" failure mode where a malformed JID silently produced an
        empty SendResponse.
        """
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        # Authoritative preflight against whatsmeow. Catches the "session went
        # stale a few days ago" case where our cached state still reads
        # CONNECTED but the server has unlinked the device.
        if not await self._check_logged_in():
            await self._emit_event(
                "session_expired",
                {"reason": "preflight is_logged_in=False before send"},
            )
            try:
                await self.logout()
            except Exception as e:
                logger.error(f"Auto-logout after preflight failure: {e}")
            return {
                "success": False,
                "error": (
                    "WhatsApp session is no longer logged in. "
                    "Click 'Connect' to scan a fresh QR and re-link."
                ),
                "to": phone,
            }

        normalized = self.normalize_phone(phone)
        if not normalized:
            return {"success": False, "error": "Invalid phone number (no digits)", "to": phone}
        if len(normalized) < 8 or len(normalized) > 15:
            return {
                "success": False,
                "error": (
                    f"Invalid phone number length ({len(normalized)} digits). "
                    "Use international format with country code, e.g. 989121234567."
                ),
                "to": phone,
            }

        # Verify the number is actually on WhatsApp and obtain the canonical JID.
        try:
            checks = await self._client.is_on_whatsapp(normalized)
        except Exception as e:
            logger.error(f"is_on_whatsapp lookup failed for {normalized}: {e}")
            return {"success": False, "error": f"Lookup failed: {e}", "to": phone}

        target_jid = None
        for r in checks or []:
            if getattr(r, "IsIn", False):
                target_jid = r.JID
                break
        if target_jid is None:
            return {
                "success": False,
                "error": f"Number {normalized} is not registered on WhatsApp",
                "to": phone,
            }

        try:
            resp = await self._client.send_message(target_jid, message)
        except Exception as e:
            logger.error(f"Failed to send message to {normalized}: {e}")
            return {"success": False, "error": str(e), "to": phone}

        # neonize raises on Go-side errors, but a malformed/unroutable send can
        # still come back with an empty SendResponse. Treat zero-Timestamp and
        # missing ID as a delivery failure rather than reporting a false success.
        msg_id = getattr(resp, "ID", "") if resp else ""
        ts = int(getattr(resp, "Timestamp", 0) or 0) if resp else 0
        if not msg_id or ts == 0:
            logger.error(
                f"Send to {normalized} returned empty response (id={msg_id!r}, ts={ts})"
            )
            return {
                "success": False,
                "error": "WhatsApp did not acknowledge the send (no message id/timestamp)",
                "to": phone,
            }

        # Track delivery health: if we keep sending but never see receipts, the
        # session is almost certainly stale (the "id returned but recipient
        # never sees it" failure mode after a few days).
        self._last_sent_id = msg_id
        self._last_sent_at = time.time()
        self._consecutive_send_no_receipt += 1
        STALE_SEND_THRESHOLD = 3
        if self._consecutive_send_no_receipt >= STALE_SEND_THRESHOLD:
            logger.warning(
                f"{self._consecutive_send_no_receipt} sends without a receipt — "
                "scheduling a session re-validation."
            )

            async def _revalidate() -> None:
                # Give whatsmeow ~10s to deliver and emit a receipt.
                await asyncio.sleep(10)
                if self._consecutive_send_no_receipt < STALE_SEND_THRESHOLD:
                    return
                if not await self._check_logged_in():
                    await self._emit_event(
                        "session_expired",
                        {"reason": "no receipts after multiple sends"},
                    )
                    try:
                        await self.logout()
                    except Exception as e:
                        logger.error(f"Auto-logout after stale-sends failed: {e}")

            try:
                asyncio.get_running_loop().create_task(_revalidate())
            except RuntimeError:
                pass

        sent_iso = (
            datetime.utcfromtimestamp(ts).isoformat() if ts else datetime.utcnow().isoformat()
        )
        result = {
            "success": True,
            "to": normalized,
            "message": message,
            "timestamp": sent_iso,
            "message_id": msg_id,
        }

        # Mirror outgoing sends into the message store so they show up in
        # /messages/history alongside incoming messages.
        if settings.MESSAGE_STORE_ENABLED:
            chat_jid = Jid2String(target_jid)
            sent_record = {
                "id": msg_id,
                "from": "me",
                "chat_jid": chat_jid,
                "sender_name": "Me",
                "text": message,
                "timestamp": sent_iso,
                "is_group": False,
                "is_from_me": True,
                "type": "text",
            }
            self._message_store.append(sent_record)
            if len(self._message_store) > settings.MAX_STORED_MESSAGES:
                self._message_store = self._message_store[-settings.MAX_STORED_MESSAGES:]
            await self._persist_message(sent_record)

        logger.info(f"Message sent to {normalized} (id={msg_id})")
        await self._emit_event("message_sent", result)
        return result

    async def send_reply(self, phone: str, message: str, quoted_message_id: Optional[str] = None) -> Dict[str, Any]:
        """Send a reply message."""
        return await self.send_message(phone, message)

    async def get_contacts(self) -> List[Dict[str, Any]]:
        """Get all contacts from WhatsApp."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        try:
            contacts = await self._client.get_all_contacts()
            result = []
            for contact in contacts:
                contact_info = {
                    "jid": Jid2String(contact.JID) if hasattr(contact, 'JID') else str(contact),
                    "name": getattr(contact, 'FullName', None) or getattr(contact, 'PushName', None) or getattr(contact, 'BusinessName', None) or "Unknown",
                    "phone": getattr(contact, 'Phone', None),
                }
                result.append(contact_info)
            self._contacts_cache = {c["jid"]: c for c in result}
            return result
        except Exception as e:
            logger.error(f"Failed to get contacts: {e}")
            return []

    async def get_chats(self) -> List[Dict[str, Any]]:
        """Get recent chats/conversations."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        # Return stored messages grouped by chat
        chats = {}
        for msg in self._message_store:
            chat_id = msg.get("chat_jid", msg.get("from", "unknown"))
            if chat_id not in chats:
                chats[chat_id] = {
                    "chat_jid": chat_id,
                    "name": msg.get("sender_name", chat_id),
                    "last_message": msg.get("text", ""),
                    "last_timestamp": msg.get("timestamp", ""),
                    "unread_count": 0,
                    "messages": [],
                }
            chats[chat_id]["messages"].append(msg)
            chats[chat_id]["last_message"] = msg.get("text", "")
            chats[chat_id]["last_timestamp"] = msg.get("timestamp", "")

        return list(chats.values())

    async def get_chat_messages(self, phone: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get messages from a specific chat."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        jid_str = phone if "@" in phone else f"{phone}@s.whatsapp.net"
        messages = [
            msg for msg in self._message_store
            if msg.get("chat_jid", "").startswith(phone)
            or msg.get("from", "").startswith(phone)
            or (msg.get("from_phone") or "").startswith(phone)
        ]
        return messages[-limit:]

    async def search_messages(self, query: str, contact: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search through stored messages."""
        results = []
        query_lower = query.lower()

        for msg in self._message_store:
            text = msg.get("text", "").lower()
            sender_name = msg.get("sender_name", "").lower()

            if query_lower in text or query_lower in sender_name:
                if contact:
                    contact_lower = contact.lower()
                    from_phone = (msg.get("from_phone") or "").lower()
                    if (
                        contact_lower in msg.get("from", "").lower()
                        or contact_lower in sender_name
                        or (from_phone and contact_lower in from_phone)
                    ):
                        results.append(msg)
                else:
                    results.append(msg)

        return results

    async def get_profile(self, phone: str) -> Dict[str, Any]:
        """Get profile information for a contact."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        normalized = self.normalize_phone(phone)
        try:
            jid = build_jid(normalized)
            profile_pic = await self._client.get_profile_picture(jid)
            return {
                "phone": normalized,
                "profile_picture_url": profile_pic.URL if profile_pic else None,
                "profile_picture_id": profile_pic.ID if profile_pic else None,
            }
        except Exception as e:
            logger.error(f"Failed to get profile for {normalized}: {e}")
            return {"phone": normalized, "error": str(e)}

    async def check_phone_registered(self, phones: List[str]) -> List[Dict[str, Any]]:
        """Check if phone numbers are registered on WhatsApp."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        normalized = [self.normalize_phone(p) for p in phones if self.normalize_phone(p)]
        if not normalized:
            return []
        try:
            results = await self._client.is_on_whatsapp(*normalized)
            return [
                {
                    "phone": r.Query,
                    "is_registered": r.IsIn,
                    "jid": Jid2String(r.JID) if r.IsIn else None,
                }
                for r in results
            ]
        except Exception as e:
            logger.error(f"Failed to check phones: {e}")
            return []

    async def get_groups(self) -> List[Dict[str, Any]]:
        """Get all groups the user is part of."""
        if not self.is_connected:
            raise ConnectionError("WhatsApp is not connected")

        try:
            groups = await self._client.get_joined_groups()
            return [
                {
                    "jid": Jid2String(g.JID),
                    "name": g.GroupName.Name if hasattr(g, 'GroupName') else "Unknown",
                    "participant_count": len(g.Participants) if hasattr(g, 'Participants') else 0,
                }
                for g in groups
            ]
        except Exception as e:
            logger.error(f"Failed to get groups: {e}")
            return []

    # ─── Auto-Reply System (DB-backed) ───────────────────────────────────

    async def get_auto_reply_config(self) -> Dict[str, Any]:
        """Read live auto-reply config (singleton row + rules) from the DB."""
        from app.core.database import db

        cfg = await db.get_config()
        rules = await db.list_rules()
        return {
            "enabled": bool(cfg.get("enabled")),
            "message": cfg.get("default_message", ""),
            "assistant_name": cfg.get("assistant_name", settings.ASSISTANT_NAME),
            "llm_enabled": bool(cfg.get("llm_enabled")),
            "llm_system_prompt": cfg.get("llm_system_prompt", ""),
            "quiet_hours": {
                "enabled": bool(cfg.get("quiet_hours_enabled")),
                "start": cfg.get("quiet_hours_start", "22:00"),
                "end": cfg.get("quiet_hours_end", "08:00"),
                "timezone": cfg.get("quiet_hours_timezone", "UTC"),
                "message": cfg.get("quiet_hours_message", ""),
                "defer_scheduled": bool(cfg.get("quiet_hours_defer_scheduled")),
            },
            "rules": rules,
        }

    async def set_auto_reply_config(self, **fields: Any) -> Dict[str, Any]:
        """Persist core auto-reply config fields to the DB."""
        from app.core.database import db

        await db.update_config(**fields)
        # Mirror to in-process settings so log lines / status reflect reality.
        if "enabled" in fields and fields["enabled"] is not None:
            settings.AUTO_REPLY_ENABLED = bool(fields["enabled"])
        if "default_message" in fields and fields["default_message"]:
            settings.AUTO_REPLY_MESSAGE = fields["default_message"]
        if "assistant_name" in fields and fields["assistant_name"]:
            settings.ASSISTANT_NAME = fields["assistant_name"]
        return await self.get_auto_reply_config()

    @staticmethod
    def _rule_matches(rule: Dict, sender_jid: str, sender_phone: str, text: str) -> bool:
        """Apply contact + keyword filters with the configured match mode."""
        contact = (rule.get("contact") or "").strip()
        if contact:
            target = contact.lower()
            if target not in sender_jid.lower() and target not in sender_phone.lower():
                return False

        keyword = (rule.get("keyword") or "").strip()
        if not keyword:
            # Contact-only rule (or universal if both empty) is fine.
            return True

        mode = (rule.get("match_mode") or "contains").lower()
        haystack = text or ""
        needle = keyword
        if mode == "regex":
            try:
                return re.search(needle, haystack, re.IGNORECASE) is not None
            except re.error:
                return False
        h_low = haystack.lower()
        n_low = needle.lower()
        if mode == "exact":
            return h_low.strip() == n_low.strip()
        if mode == "starts_with":
            return h_low.lstrip().startswith(n_low)
        return n_low in h_low  # contains (default)

    async def _evaluate_auto_reply(
        self,
        sender_jid: str,
        message_text: str,
        sender_pushname: Optional[str] = None,
        sender_jid_alt: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Decide whether to auto-reply. Returns (should_reply, reply_text, matched_rule).

        Resolution order:
            1. Global enabled? — no → silent.
            2. Quiet hours? — yes → away message or silent.
            3. First matching enabled rule (in priority order) → its reply.
            4. No rule matched + global llm_enabled → LLM catch-all.
            5. No rule matched + no rules at all → default static message.
            6. Otherwise (rules exist but none matched, no LLM) → silent.
        """
        from app.core.database import db
        from app.core.llm import LLMError, llm_client
        from app.core.quiet_hours import is_quiet_now

        cfg = await db.get_config()
        if not cfg.get("enabled"):
            logger.debug("Auto-reply skipped: globally disabled")
            return False, "", None

        if is_quiet_now(cfg):
            quiet_msg = (cfg.get("quiet_hours_message") or "").strip()
            if not quiet_msg:
                logger.info("Auto-reply skipped: in quiet hours, no away message set")
                return False, "", None
            logger.info("Auto-reply: sending quiet-hours away message")
            return True, quiet_msg, None

        rules = await db.list_rules()
        # Prefer the resolved phone form so phone-keyed rules still match
        # senders WhatsApp addressed via LID (privacy-mode addressing).
        sender_phone = (
            self.normalize_phone(sender_jid_alt) if sender_jid_alt else self.normalize_phone(sender_jid)
        )
        now_ts = time.time()

        matched_rule: Optional[Dict[str, Any]] = None
        for rule in rules:
            if not rule.get("enabled"):
                continue
            if not self._rule_matches(rule, sender_jid, sender_phone, message_text):
                continue
            cooldown = int(rule.get("cooldown_seconds") or 0)
            if cooldown > 0:
                last = await db.get_cooldown(int(rule["id"]), sender_jid)
                if last is not None and (now_ts - last) < cooldown:
                    logger.info("Auto-reply skipped: rule %s on cooldown for %s", rule["id"], sender_jid)
                    return False, "", rule
            matched_rule = rule
            break

        # Resolve persona — used by the LLM path whether or not a rule matched.
        # sender_jid_alt is the alternate addressing form: when Sender is
        # `<opaque>@lid`, SenderAlt is the `<phone>@s.whatsapp.net` form (and
        # vice versa). Without trying both, LID-addressed messages would never
        # match a persona stored by phone number.
        persona = await db.find_persona_for_jid(
            sender_jid, pushname=sender_pushname, jid_alt=sender_jid_alt,
        )
        if persona:
            logger.debug(
                "Persona matched for %s (alt=%s): %s",
                sender_jid, sender_jid_alt or "-",
                persona.get("display_name") or persona.get("contact"),
            )
        else:
            logger.debug(
                "No persona matched for sender=%s alt=%s pushname=%s",
                sender_jid, sender_jid_alt or "-", sender_pushname or "-",
            )

        # ─── Path A: a rule matched ───────────────────────────────────────
        if matched_rule is not None:
            cooldown = int(matched_rule.get("cooldown_seconds") or 0)
            persona_wants_llm = persona is not None and bool(persona.get("use_llm"))
            persona_blocks_llm = persona is not None and not bool(persona.get("use_llm"))
            wants_llm = (
                bool(matched_rule.get("use_llm"))
                or persona_wants_llm
                or (bool(cfg.get("llm_enabled")) and not (matched_rule.get("message") or "").strip())
            )
            if persona_blocks_llm:
                wants_llm = False

            if wants_llm:
                reply = await self._llm_reply(cfg, persona, sender_jid, message_text)
                if reply is not None:
                    if cooldown > 0:
                        await db.touch_cooldown(int(matched_rule["id"]), sender_jid, now_ts)
                    return True, reply, matched_rule
                # else fall through to static text

            reply_text = (matched_rule.get("message") or cfg.get("default_message") or "").strip()
            if not reply_text:
                return False, "", matched_rule
            if cooldown > 0:
                await db.touch_cooldown(int(matched_rule["id"]), sender_jid, now_ts)
            return True, reply_text, matched_rule

        # ─── Path B: no rule matched ──────────────────────────────────────
        llm_globally_on = bool(cfg.get("llm_enabled"))
        persona_wants_llm = persona is not None and bool(persona.get("use_llm"))
        persona_blocks_llm = persona is not None and not bool(persona.get("use_llm"))

        # A persona with use_llm=True opts this contact into LLM replies, even
        # if the global llm_enabled toggle is off.
        should_use_llm = (llm_globally_on or persona_wants_llm) and not persona_blocks_llm

        if should_use_llm:
            reply = await self._llm_reply(cfg, persona, sender_jid, message_text)
            if reply is not None:
                source = "persona-driven" if persona_wants_llm and not llm_globally_on else "catch-all"
                logger.info("Auto-reply: LLM (%s) replied to %s", source, sender_jid)
                return True, reply, None
            # LLM failed/unavailable — fall through

        if not rules:
            # No rules configured — use the default message.
            default_msg = (cfg.get("default_message") or settings.AUTO_REPLY_MESSAGE or "").strip()
            if default_msg:
                return True, default_msg, None

        logger.info(
            "Auto-reply skipped: no rule matched and "
            "%s",
            "LLM not configured/enabled" if not llm_globally_on or not llm_client.is_configured
            else "LLM call failed",
        )
        return False, "", None

    async def _llm_reply(
        self,
        cfg: Dict[str, Any],
        persona: Optional[Dict[str, Any]],
        sender_jid: str,
        message_text: str,
    ) -> Optional[str]:
        """Build prompt, call LLM. Returns None on failure or when not configured."""
        from app.core.llm import LLMError, llm_client

        if not llm_client.is_configured:
            logger.info("LLM reply requested but provider is not configured (%s)", llm_client.provider)
            return None

        system_prompt = (
            (persona or {}).get("system_prompt_override")
            or cfg.get("llm_system_prompt")
            or settings.LLM_SYSTEM_PROMPT
        )
        notes = (persona or {}).get("notes") or ""
        if notes:
            system_prompt = f"{system_prompt}\n\nContact context:\n{notes}"

        history = self._recent_history_for_chat(sender_jid, settings.LLM_HISTORY_SIZE)
        try:
            return await llm_client.generate_reply(
                system_prompt=system_prompt,
                history=history,
                user_message=message_text,
            )
        except LLMError as e:
            logger.warning("LLM reply failed: %s", e)
            return None

    def _recent_history_for_chat(self, sender_jid: str, limit: int) -> List[Dict[str, str]]:
        """Build {role, content} history for the LLM from the in-memory store."""
        if limit <= 0:
            return []
        history: List[Dict[str, str]] = []
        for msg in self._message_store[-500:]:
            if msg.get("chat_jid") != sender_jid and msg.get("from") != sender_jid:
                continue
            text = (msg.get("text") or "").strip()
            if not text or text.startswith("["):
                continue
            role = "assistant" if msg.get("is_from_me") else "user"
            history.append({"role": role, "content": text})
        return history[-limit:]

    async def _persist_message(self, msg_record: Dict[str, Any]) -> None:
        """Write a message record to the persisted history DB (best-effort)."""
        try:
            from app.core.message_history import message_history_db

            await message_history_db.insert(msg_record, source="live")
        except Exception as e:
            logger.warning(f"Failed to persist message {msg_record.get('id')}: {e}")

    # ─── Event System ────────────────────────────────────────────────────

    def on_event(self, event_type: str, handler: Callable):
        """Register an event handler."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    async def _emit_event(self, event_type: str, data: Dict[str, Any]):
        """Emit an event to all registered handlers."""
        handlers = self._event_handlers.get(event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"Event handler error for {event_type}: {e}")

    async def _handle_incoming_message(self, client: NewAClient, event: MessageEv):
        """Process incoming messages, store them, and handle auto-reply."""
        try:
            # Extract message info
            info = event.Info
            message = event.Message

            # Get text content
            text = ""
            if message.conversation:
                text = message.conversation
            elif message.extendedTextMessage and message.extendedTextMessage.text:
                text = message.extendedTextMessage.text
            elif message.imageMessage and message.imageMessage.caption:
                text = f"[Image] {message.imageMessage.caption}"
            elif message.videoMessage and message.videoMessage.caption:
                text = f"[Video] {message.videoMessage.caption}"
            elif message.documentMessage:
                text = f"[Document] {message.documentMessage.fileName}"
            elif message.audioMessage:
                text = "[Audio Message]"
            elif message.stickerMessage:
                text = "[Sticker]"

            sender_jid = Jid2String(info.MessageSource.Sender) if info.MessageSource.Sender else "unknown"
            # SenderAlt is the alternate addressing form (PN <-> LID). Only
            # populated when WhatsApp ships the message with both, so guard
            # against it being a default-empty JID (User == "").
            sender_jid_alt: Optional[str] = None
            try:
                alt = getattr(info.MessageSource, "SenderAlt", None)
                if alt is not None and getattr(alt, "User", ""):
                    sender_jid_alt = Jid2String(alt)
            except Exception:
                sender_jid_alt = None
            # This message didn't carry SenderAlt — fall back to whatsmeow's
            # own durable lid_map (built from prior contact/history sync),
            # rather than leaving LID-addressed senders unresolved.
            if sender_jid_alt is None and sender_jid.endswith("@lid"):
                sender_jid_alt = self._resolve_lid_phone(sender_jid)
            chat_jid = Jid2String(info.MessageSource.Chat) if info.MessageSource.Chat else "unknown"
            sender_name = info.Pushname if hasattr(info, 'Pushname') else sender_jid
            is_group = info.MessageSource.IsGroup if hasattr(info.MessageSource, 'IsGroup') else False
            is_from_me = info.MessageSource.IsFromMe if hasattr(info.MessageSource, 'IsFromMe') else False

            # Resolved phone digits for this sender, regardless of whether
            # WhatsApp addressed the message by phone or by LID — lets
            # phone-based lookups/search/rules work the same either way.
            if sender_jid.endswith("@lid"):
                from_phone = self.normalize_phone(sender_jid_alt) if sender_jid_alt else None
            else:
                from_phone = self.normalize_phone(sender_jid)

            # Build message record
            msg_record = {
                "id": info.ID if hasattr(info, 'ID') else str(time.time()),
                "from": sender_jid,
                "from_phone": from_phone,
                "chat_jid": chat_jid,
                "sender_name": sender_name,
                "text": text,
                "timestamp": datetime.utcnow().isoformat(),
                "is_group": is_group,
                "is_from_me": is_from_me,
                "type": "text" if text and not text.startswith("[") else "media",
            }

            # Store message
            if settings.MESSAGE_STORE_ENABLED:
                self._message_store.append(msg_record)
                if len(self._message_store) > settings.MAX_STORED_MESSAGES:
                    self._message_store = self._message_store[-settings.MAX_STORED_MESSAGES:]
                await self._persist_message(msg_record)

            logger.info(f"Message from {sender_name} ({sender_jid}): {text[:100]}")

            # Emit event
            await self._emit_event("message", msg_record)

            # Auto-reply logic (don't reply to own messages or group messages)
            if not is_from_me and not is_group and text:
                should_reply, reply_text, matched_rule = await self._evaluate_auto_reply(
                    sender_jid, text,
                    sender_pushname=sender_name,
                    sender_jid_alt=sender_jid_alt,
                )
                if should_reply and reply_text:
                    await asyncio.sleep(2)  # Natural delay
                    try:
                        jid = info.MessageSource.Chat
                        await client.send_message(jid, reply_text)
                        logger.info(f"Auto-replied to {sender_name}: {reply_text[:80]}")
                        await self._emit_event("auto_reply_sent", {
                            "to": sender_jid,
                            "message": reply_text,
                            "original_message": text,
                            "rule_id": matched_rule.get("id") if matched_rule else None,
                            "via_llm": bool(matched_rule and matched_rule.get("use_llm")),
                        })
                    except Exception as e:
                        logger.error(f"Auto-reply failed: {e}")

        except Exception as e:
            logger.error(f"Error handling incoming message: {e}")

    # ─── Status & Health ─────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Get current client status (cheap, synchronous)."""
        return {
            "state": self._state.value,
            "is_connected": self.is_connected,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "device_info": self._device_info,
            "stored_messages_count": len(self._message_store),
            "auto_reply_enabled": settings.AUTO_REPLY_ENABLED,
            "contacts_cached": len(self._contacts_cache),
            "last_sent_at": self._last_sent_at,
            "last_receipt_at": self._last_receipt_at,
            "sends_without_receipt": self._consecutive_send_no_receipt,
        }

    async def probe_session(self) -> Dict[str, Any]:
        """Force an authoritative session check by hitting whatsmeow directly."""
        logged_in = await self._check_logged_in()
        if not logged_in and self._state == ConnectionState.CONNECTED:
            self._state = ConnectionState.LOGGED_OUT
            await self._emit_event("session_expired", {"reason": "manual probe"})
        return {
            "state": self._state.value,
            "logged_in": logged_in,
            "last_sent_at": self._last_sent_at,
            "last_receipt_at": self._last_receipt_at,
            "sends_without_receipt": self._consecutive_send_no_receipt,
        }

    def get_stored_messages(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get stored messages with pagination."""
        return self._message_store[-(offset + limit):-offset if offset else None]

    async def reload_message_store(self) -> int:
        """Re-hydrate the in-memory store from the persisted DB (e.g. after
        an import) without requiring a process restart."""
        await self._load_message_store_from_db()
        return len(self._message_store)


# Singleton instance
wa_client = WhatsAppClientManager()
