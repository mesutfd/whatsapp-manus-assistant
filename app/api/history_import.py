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

import asyncio
import logging
import os
import re
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.auth import get_current_user
from app.core.message_history import message_history_db
from app.core.whatsapp_client import wa_client
from app.services import backup_import, media_import

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


# ─── Restore from backup (auto-detecting) ────────────────────────────────────

_INSERT_CHUNK = 2000
# Media bundles (DB + all referenced photos/audio/documents) can be large.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB


async def _insert_chunked(records: List[Dict[str, Any]]) -> int:
    inserted = 0
    for i in range(0, len(records), _INSERT_CHUNK):
        inserted += await message_history_db.insert_many(
            records[i : i + _INSERT_CHUNK], source="import"
        )
    return inserted


def _contact_name_lookup() -> Dict[str, str]:
    """jid -> display name, from the live contacts cache (best effort)."""
    cache = getattr(wa_client, "_contacts_cache", None) or {}
    lookup: Dict[str, str] = {}
    for jid, info in cache.items():
        name = (info or {}).get("name")
        if name and name != "Unknown":
            lookup[jid] = name
    return lookup


async def _read_upload_to_temp(file: UploadFile, suffix: str) -> str:
    """Stream the upload to a temp file (SQLite needs a real path)."""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wa-backup-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Backup file too large")
                out.write(chunk)
        return path
    except Exception:
        os.unlink(path)
        raise


def _find_sqlite_in_zip(zip_path: str) -> Optional[str]:
    """Return the entry name of a WhatsApp sqlite DB inside a bundle zip
    (ChatStorage*.sqlite / msgstore*.db at any depth), or None."""
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if re.fullmatch(r"chatstorage.*\.sqlite|msgstore.*\.db", base):
                return name
    return None


def _extract_zip_entry_to_temp(zip_path: str, entry: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wa-bundle-db-")
    with os.fdopen(fd, "wb") as out, zipfile.ZipFile(zip_path) as archive:
        with archive.open(entry) as src:
            while True:
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    return path


@router.post("/import-backup")
async def import_backup(
    file: UploadFile = File(..., description="WhatsApp backup: chat export .txt, export .zip, decrypted msgstore.db, iOS ChatStorage.sqlite, or a media bundle .zip (DB + media files)"),
    phone: str = Form("", description="Optional: other participant's phone (single-chat .txt/.zip only)"),
    other_name: str = Form("", description="Optional: other participant's name as it appears in the export (single-chat .txt/.zip only)"),
    include_videos: bool = Form(False, description="Media bundles: also store full video originals (large)"),
    user: dict = Depends(get_current_user),
):
    """
    **Restore previous chats from a WhatsApp backup.** Upload one file and the
    type is auto-detected:

    - `.txt` — a single "Export chat" transcript (per-chat backup)
    - `.zip` — a per-chat export with media, or an archive bundling many
      exported `.txt` transcripts (only the transcripts are imported)
    - **decrypted** Android `msgstore.db` (full backup — all chats)
    - iOS `ChatStorage.sqlite` from an iPhone backup (full backup — all chats)
    - **media bundle** `.zip` — a ChatStorage/msgstore DB plus the actual
      media files (see scripts/export_ios_backup_bundle.py). Messages AND
      media are imported: originals into GridFS, plus small thumbnails for
      images.

    Encrypted `.crypt12/.crypt14/.crypt15` files are rejected: decrypt them
    first with your 64-digit backup key (e.g. the `wa-crypt-tools` project).

    Everything is written to the persisted message store, so restored chats
    appear in history, search, and LLM auto-reply context like live messages.
    Re-uploading the same backup is safe — duplicates are skipped.
    """
    filename = (file.filename or "backup").strip()
    lower = filename.lower()

    if lower.endswith(backup_import.CRYPT_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=(
                "This is an ENCRYPTED WhatsApp backup. It must be decrypted with "
                "your 64-digit backup key before it can be imported (see the "
                "'wa-crypt-tools' project: wadecrypt key msgstore.db.crypt15 "
                "msgstore.db), then upload the resulting msgstore.db here."
            ),
        )

    head = await file.read(16)
    await file.seek(0)

    tmp_path: Optional[str] = None
    db_tmp_path: Optional[str] = None
    try:
        # ── Full backups: SQLite databases ──────────────────────────────
        if head.startswith(backup_import.SQLITE_MAGIC):
            tmp_path = await _read_upload_to_temp(file, suffix=".db")
            kind = await asyncio.to_thread(backup_import.sniff_sqlite_kind, tmp_path)
            if kind is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This SQLite database doesn't look like a WhatsApp message "
                        "store (expected msgstore.db or ChatStorage.sqlite)."
                    ),
                )
            names = _contact_name_lookup()
            parser = (
                backup_import.parse_ios_chatstorage
                if kind == "ios_sqlite"
                else backup_import.parse_android_msgstore
            )
            records, summary = await asyncio.to_thread(parser, tmp_path, names)

        # ── ZIP: media bundle, per-chat export, or bundle of .txt exports ─
        elif head.startswith(b"PK"):
            tmp_path = await _read_upload_to_temp(file, suffix=".zip")
            try:
                db_entry = await asyncio.to_thread(_find_sqlite_in_zip, tmp_path)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Corrupt or unreadable .zip file")

            if db_entry:
                # Bundle: sqlite DB + media files at their local-path names.
                db_tmp_path = await asyncio.to_thread(
                    _extract_zip_entry_to_temp, tmp_path, db_entry, ".db"
                )
                kind = await asyncio.to_thread(backup_import.sniff_sqlite_kind, db_tmp_path)
                if kind is None:
                    raise HTTPException(
                        status_code=400,
                        detail="The database inside this zip isn't a WhatsApp message store.",
                    )
                names = _contact_name_lookup()
                parser = (
                    backup_import.parse_ios_chatstorage
                    if kind == "ios_sqlite"
                    else backup_import.parse_android_msgstore
                )
                records, summary = await asyncio.to_thread(parser, db_tmp_path, names)
                media_stats = await media_import.attach_media_from_zip(
                    records, tmp_path, include_videos=include_videos
                )
                summary = {**summary, "kind": f"{kind}_bundle", **media_stats}
            else:
                with open(tmp_path, "rb") as fh:
                    raw = fh.read()
                try:
                    records, summary = backup_import.parse_zip_backup(
                        raw, filename, phone or None, other_name or None
                    )
                except zipfile.BadZipFile:
                    raise HTTPException(status_code=400, detail="Corrupt or unreadable .zip file")

        # ── Plain text: single chat export ──────────────────────────────
        else:
            raw = await file.read()
            if len(raw) > 200 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Text export too large")
            text = raw.decode("utf-8", errors="replace")
            entries = backup_import.parse_txt_transcript(text)
            subject = (other_name or "").strip() or backup_import.subject_from_filename(filename)
            records = backup_import.records_from_transcript(
                entries, subject, owner_name=None, phone=phone or None
            )
            summary = {"kind": "txt", "chats": 1 if records else 0}
    finally:
        for path in (tmp_path, db_tmp_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    if not records:
        return {
            "success": False,
            "error": "no_messages_parsed",
            "message": (
                "No messages could be read from that file. Supported: WhatsApp "
                "'Export chat' .txt/.zip, decrypted Android msgstore.db, or iOS "
                "ChatStorage.sqlite."
            ),
            **summary,
        }

    inserted = await _insert_chunked(records)
    new_total = await wa_client.reload_message_store()

    return {
        "success": True,
        "parsed_messages": len(records),
        "newly_inserted": inserted,
        "duplicates_skipped": len(records) - inserted,
        "message_store_size_after_reload": new_total,
        **summary,
    }
