"""
One-time manual import of WhatsApp chat history.

whatsmeow/neonize (and therefore this app's message store) only ever sees
messages sent or received while the process is connected — there is no
protocol-level way to backfill years of prior conversation for an
already-linked device. The practical workaround: WhatsApp's own "Export
chat" feature (Chat > More > Export chat > Without Media) produces a full
text transcript, which this endpoint parses and inserts into the persisted
message store so it shows up in /messages/history, /messages/chat/{phone},
search, and LLM auto-reply context exactly like live messages.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.auth import get_current_user
from app.core.message_history import message_history_db
from app.core.whatsapp_client import wa_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/messages", tags=["Messages"])

# Matches both common export styles after normalizing whitespace/markers:
#   iOS:     [24/03/2024, 14:23:11] John Doe: message text
#   Android: 24/03/2024, 14:23 - John Doe: message text
_LINE_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?"
    r"\s*[-–]?\s*([^:\n]{1,60}):\s(.*)$"
)
# A line that starts with a date/time stamp but isn't "Name: text" — a
# system notice ("Messages are end-to-end encrypted...", "X joined using
# invite link", etc.). Skip these instead of misattributing them.
_PREFIX_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?"
)
_INVISIBLE_CHARS = "‎‏"


def _clean_line(line: str) -> str:
    for ch in _INVISIBLE_CHARS:
        line = line.replace(ch, "")
    return line.replace("\xa0", " ").replace(" ", " ").rstrip("\r\n")


def _parse_date(date_str: str) -> Tuple[int, int, int]:
    d, m, y = (int(p) for p in date_str.split("/"))
    if y < 100:
        y += 2000
    # WhatsApp exports are DD/MM in the vast majority of locales; if the
    # exporting phone used a US (MM/DD) region format, dates will be off.
    return y, m, d


def _parse_time(time_str: str) -> Tuple[int, int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([APap][Mm])?$", time_str.strip())
    if not m:
        return 0, 0, 0
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    ampm = (m.group(4) or "").lower()
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return h, mi, s


def _to_iso(date_str: str, time_str: str) -> str:
    y, mo, d = _parse_date(date_str)
    h, mi, s = _parse_time(time_str)
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"


def parse_whatsapp_export(
    raw_text: str, phone: str, other_name: str, chat_jid: str
) -> List[Dict[str, Any]]:
    """Parse a WhatsApp 'Export chat' .txt transcript into message records."""
    other_name_lower = other_name.strip().lower()
    records: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    idx = 0

    for raw_line in raw_text.splitlines():
        line = _clean_line(raw_line)
        if not line.strip():
            continue

        match = _LINE_RE.match(line)
        if match:
            date_str, time_str, sender, text = match.groups()
            sender = sender.strip()
            sender_lower = sender.lower()
            is_from_me = not (other_name_lower in sender_lower or sender_lower in other_name_lower)

            idx += 1
            lowered_text = text.lower()
            msg_type = "media" if "omitted" in lowered_text else "text"

            current = {
                "id": f"import-{phone}-{idx}",
                "from": "me" if is_from_me else chat_jid,
                "from_phone": None if is_from_me else phone,
                "chat_jid": chat_jid,
                "sender_name": "Me" if is_from_me else sender,
                "text": text,
                "timestamp": _to_iso(date_str, time_str),
                "is_group": False,
                "is_from_me": is_from_me,
                "type": msg_type,
            }
            records.append(current)
            continue

        if _PREFIX_RE.match(line):
            # System notice (encryption banner, group-add notice, ...) — skip
            # and don't let later continuation lines glue onto the wrong message.
            current = None
            continue

        # Continuation of a multi-line message.
        if current is not None:
            current["text"] = f"{current['text']}\n{line}"

    return records


@router.post("/import-history")
async def import_history(
    file: UploadFile = File(..., description="WhatsApp chat export .txt file"),
    phone: str = Form(..., description="The other participant's phone number, digits only (e.g. 989123456789)"),
    other_name: str = Form(..., description="Exactly how that contact's name appears in the exported chat file"),
    user: dict = Depends(get_current_user),
):
    """
    **Import chat history (one-time, manual)** — upload a WhatsApp
    "Export chat" .txt file (Chat > ⋮ > More > Export chat > Without Media)
    for a single 1:1 conversation and merge it into the persisted message
    store, so /messages/*, /smart/search, and LLM auto-reply context can
    see it alongside live traffic.

    Safe to re-run with the same file — duplicate lines are ignored.
    """
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="Expected a WhatsApp chat export .txt file")

    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    raw_bytes = await file.read()
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raw_text = raw_bytes.decode("utf-8", errors="replace")

    chat_jid = f"{digits}@s.whatsapp.net"
    records = parse_whatsapp_export(raw_text, digits, other_name, chat_jid)

    if not records:
        return {
            "success": False,
            "error": "no_messages_parsed",
            "message": (
                "Couldn't find any messages in that file. Make sure it's an "
                "unmodified WhatsApp chat export, and that 'other_name' matches "
                "the sender name shown in the file."
            ),
        }

    inserted = await message_history_db.insert_many(records, source="import")
    new_total = await wa_client.reload_message_store()

    return {
        "success": True,
        "phone": digits,
        "chat_jid": chat_jid,
        "parsed_messages": len(records),
        "newly_inserted": inserted,
        "duplicates_skipped": len(records) - inserted,
        "message_store_size_after_reload": new_total,
    }
