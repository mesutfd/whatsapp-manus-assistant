"""
Restore chat history from WhatsApp backup files.

Supported inputs (auto-detected):
  - Single "Export chat" .txt transcript (iOS or Android formatting)
  - .zip archives: a per-chat export ("WhatsApp Chat - Name.zip" containing
    _chat.txt) or a bundle of several exported .txt transcripts
  - Decrypted Android msgstore.db (both the legacy `messages` schema and the
    modern `message`/`chat`/`jid` schema)
  - iOS ChatStorage.sqlite (from an iPhone/iTunes backup)

Encrypted Android backups (.crypt12/.crypt14/.crypt15) can NOT be read here —
they must be decrypted first with the user's 64-digit key (e.g. wa-crypt-tools).

All parsers emit message records in the exact shape the live message store
uses, so imported history behaves identically to live traffic.
"""

import hashlib
import io
import logging
import re
import sqlite3
import zipfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Shared text-transcript parsing ──────────────────────────────────────────

# iOS:     [24/03/2024, 14:23:11] John Doe: message text
# Android: 24/03/2024, 14:23 - John Doe: message text
_LINE_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?"
    r"\s*[-–]?\s*([^:\n]{1,60}):\s(.*)$"
)
# Date/time-stamped line that isn't "Name: text" — a system notice.
_PREFIX_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?"
)
_INVISIBLE_CHARS = "‎‏"

# Filename prefixes WhatsApp uses for exports (English variants; other
# locales fall back to the raw file stem as the chat name).
_SUBJECT_PREFIXES = (
    "whatsapp chat with ",
    "whatsapp chat - ",
    "chat with ",
)

CRYPT_EXTENSIONS = (".crypt12", ".crypt14", ".crypt15")
SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_status_jid(jid: str) -> bool:
    """WhatsApp Status (stories) pseudo-chats — not real conversations."""
    return jid == "status@broadcast" or jid.endswith("@status") or jid.endswith(".status")

# Core Data epoch (iOS ZMESSAGEDATE is seconds since 2001-01-01).
_APPLE_EPOCH = datetime(2001, 1, 1)


def _clean_line(line: str) -> str:
    for ch in _INVISIBLE_CHARS:
        line = line.replace(ch, "")
    return line.replace("\xa0", " ").replace(" ", " ").rstrip("\r\n")


def _parse_date(date_str: str) -> Tuple[int, int, int]:
    d, m, y = (int(p) for p in date_str.split("/"))
    if y < 100:
        y += 2000
    # Exports are DD/MM in most locales; a phone with a US region format
    # produces MM/DD and, when unambiguous (day > 12), we swap.
    if m > 12 and d <= 12:
        d, m = m, d
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
    try:
        return datetime(y, mo, d, h, mi, s).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return f"{y:04d}-01-01T00:00:00"


def _stable_id(chat_jid: str, timestamp: str, sender: str, text: str) -> str:
    digest = hashlib.sha1(
        f"{chat_jid}|{timestamp}|{sender}|{text}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"import-{digest[:20]}"


def _phone_from_name(name: str) -> Optional[str]:
    """Unsaved contacts appear in exports as their phone ('+98 912 345 6789')."""
    stripped = re.sub(r"[\s\-().]", "", name or "")
    if re.fullmatch(r"\+?\d{8,15}", stripped):
        return stripped.lstrip("+")
    return None


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^\w]+", "-", (name or "chat").strip().lower()).strip("-")
    return cleaned or "chat"


def subject_from_filename(filename: str) -> Optional[str]:
    """'WhatsApp Chat with John Doe.txt' -> 'John Doe'. None for '_chat.txt'."""
    stem = re.sub(r"\.(txt|zip)$", "", filename.rsplit("/", 1)[-1], flags=re.I).strip()
    if not stem or stem.lower() in ("_chat", "chat"):
        return None
    lowered = stem.lower()
    for prefix in _SUBJECT_PREFIXES:
        if lowered.startswith(prefix):
            return stem[len(prefix):].strip() or None
    return stem


def parse_txt_transcript(raw_text: str) -> List[Dict[str, str]]:
    """Parse a transcript into raw entries: {sender, timestamp, text, type}."""
    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None

    for raw_line in raw_text.splitlines():
        line = _clean_line(raw_line)
        if not line.strip():
            continue

        match = _LINE_RE.match(line)
        if match:
            date_str, time_str, sender, text = match.groups()
            current = {
                "sender": sender.strip(),
                "timestamp": _to_iso(date_str, time_str),
                "text": text,
                "type": "media" if "omitted" in text.lower() else "text",
            }
            entries.append(current)
            continue

        if _PREFIX_RE.match(line):
            current = None  # system notice — skip, don't glue continuations
            continue

        if current is not None:
            current["text"] = f"{current['text']}\n{line}"

    return entries


# ─── Owner ("me") detection for bulk text imports ────────────────────────────


def detect_owner(chats: List[Dict[str, Any]]) -> Optional[str]:
    """
    Given parsed chats [{subject, entries}], guess which sender name is the
    backup owner. Votes:
      - in a 2-sender chat whose subject matches one sender, the other is "me"
      - a sender appearing across several chats is likely "me"
    """
    votes: Dict[str, int] = {}

    seen_in_chats: Dict[str, set] = {}
    for i, chat in enumerate(chats):
        senders = {e["sender"] for e in chat["entries"]}
        subject = (chat.get("subject") or "").strip().lower()
        for s in senders:
            seen_in_chats.setdefault(s.lower(), set()).add(i)
        if subject and len(senders) == 2:
            lowered = {s.lower(): s for s in senders}
            matched = next(
                (v for k, v in lowered.items() if subject in k or k in subject), None
            )
            if matched:
                other = next(s for s in senders if s != matched)
                votes[other] = votes.get(other, 0) + 3

    for sender_lower, chat_ids in seen_in_chats.items():
        if len(chat_ids) >= 2:
            original = next(
                e["sender"]
                for chat in chats
                for e in chat["entries"]
                if e["sender"].lower() == sender_lower
            )
            votes[original] = votes.get(original, 0) + len(chat_ids)

    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def records_from_transcript(
    entries: List[Dict[str, str]],
    subject: Optional[str],
    owner_name: Optional[str],
    phone: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert raw transcript entries into message-store records."""
    if not entries:
        return []

    subject = (subject or "").strip()
    owner_lower = (owner_name or "").strip().lower()
    senders = {e["sender"] for e in entries}

    def is_me(sender: str) -> bool:
        s = sender.lower()
        if owner_lower:
            return s == owner_lower
        if subject:
            subj = subject.lower()
            return not (subj in s or s in subj)
        return False

    non_owner = {s for s in senders if not is_me(s)}
    is_group = len(non_owner) > 1

    digits = re.sub(r"\D", "", phone or "") or (
        None if is_group else _phone_from_name(subject)
    )
    if digits:
        chat_jid = f"{digits}@s.whatsapp.net"
    elif is_group:
        chat_jid = f"{_slug(subject or 'group')}@import.g.us"
    else:
        chat_jid = f"{_slug(subject or next(iter(non_owner), 'chat'))}@import.chat"

    records: List[Dict[str, Any]] = []
    for e in entries:
        from_me = is_me(e["sender"])
        sender_phone = None if from_me else (digits if not is_group else _phone_from_name(e["sender"]))
        records.append({
            "id": _stable_id(chat_jid, e["timestamp"], e["sender"], e["text"]),
            "chat_name": subject or None,
            "from": "me" if from_me else chat_jid,
            "from_phone": sender_phone,
            "chat_jid": chat_jid,
            "sender_name": "Me" if from_me else e["sender"],
            "text": e["text"],
            "timestamp": e["timestamp"],
            "is_group": is_group,
            "is_from_me": from_me,
            "type": e["type"],
        })
    return records


# ─── ZIP archives ────────────────────────────────────────────────────────────


def parse_zip_backup(
    data: bytes, zip_filename: str, phone: Optional[str], other_name: Optional[str]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse a zip that contains one or more exported chat transcripts."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    chats: List[Dict[str, Any]] = []
    skipped: List[str] = []

    for info in archive.infolist():
        name = info.filename
        if info.is_dir() or name.rsplit("/", 1)[-1].startswith("."):
            continue
        if not name.lower().endswith(".txt"):
            continue  # media files in a "with media" export
        if info.file_size > 100 * 1024 * 1024:
            skipped.append(f"{name} (too large)")
            continue
        try:
            text = archive.read(info).decode("utf-8", errors="replace")
        except Exception as exc:  # corrupt entry — keep going
            skipped.append(f"{name} ({exc})")
            continue
        entries = parse_txt_transcript(text)
        if not entries:
            skipped.append(f"{name} (no messages recognized)")
            continue
        subject = subject_from_filename(name) or subject_from_filename(zip_filename)
        chats.append({"subject": subject, "entries": entries, "file": name})

    owner = detect_owner(chats) if len(chats) > 1 else None
    # Explicit other_name (single-chat zip) beats heuristics.
    if other_name and len(chats) == 1:
        chats[0]["subject"] = other_name

    records: List[Dict[str, Any]] = []
    for chat in chats:
        records.extend(
            records_from_transcript(
                chat["entries"],
                chat["subject"],
                owner,
                phone if len(chats) == 1 else None,
            )
        )

    summary = {
        "kind": "zip",
        "chats": len(chats),
        "owner_name_detected": owner,
        "skipped_files": skipped,
    }
    return records, summary


# ─── SQLite databases (Android msgstore.db / iOS ChatStorage.sqlite) ─────────


def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _jid_phone(jid: str) -> Optional[str]:
    if jid and "@" in jid:
        user = jid.split("@", 1)[0].split(":", 1)[0]
        if user.isdigit():
            return user
    return None


def _ms_to_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OSError, TypeError, OverflowError):
        return ""


# Media message-type codes shared by the legacy/modern Android schemas.
_ANDROID_MEDIA_TYPES = {1, 2, 3, 4, 5, 9, 13, 14, 16, 20, 23, 28, 29, 37, 82}

# iOS ZMESSAGETYPE -> media kind, verified empirically against real
# ChatStorage databases (joining ZWAMEDIAITEM's file extensions):
#   1=jpg image, 2=mp4/mov video, 3=opus/mp3 audio, 4=vCard contact,
#   5=lat/long location, 8=pdf/docx/... document, 11=gif (mp4), 15=webp sticker
_IOS_KIND_BY_TYPE = {
    1: "image", 2: "video", 3: "audio", 4: "contact",
    5: "location", 8: "document", 11: "gif", 15: "sticker",
}


def sniff_sqlite_kind(db_path: str) -> Optional[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = _table_names(conn)
    finally:
        conn.close()
    if "ZWAMESSAGE" in tables:
        return "ios_sqlite"
    if "message" in tables or "messages" in tables:
        return "android_sqlite"
    return None


def parse_android_msgstore(
    db_path: str, name_lookup: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse a DECRYPTED Android msgstore.db (legacy or modern schema)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = _table_names(conn)
        if "message" in tables and "chat" in tables and "jid" in tables:
            rows = conn.execute(
                """
                SELECT m.key_id      AS key_id,
                       m.from_me     AS from_me,
                       m.timestamp   AS ts,
                       m.text_data   AS text,
                       m.message_type AS mtype,
                       cj.raw_string AS chat_jid,
                       sj.raw_string AS sender_jid
                FROM message m
                JOIN chat c        ON c._id = m.chat_row_id
                JOIN jid cj        ON cj._id = c.jid_row_id
                LEFT JOIN jid sj   ON sj._id = m.sender_jid_row_id
                ORDER BY m.timestamp
                """
            )
            schema = "modern"
        elif "messages" in tables:
            rows = conn.execute(
                """
                SELECT key_id,
                       key_from_me     AS from_me,
                       timestamp       AS ts,
                       data            AS text,
                       media_wa_type   AS mtype,
                       key_remote_jid  AS chat_jid,
                       remote_resource AS sender_jid
                FROM messages
                ORDER BY timestamp
                """
            )
            schema = "legacy"
        else:
            raise ValueError("Not a recognizable WhatsApp msgstore database")

        records, skipped_system = _sqlite_rows_to_records(
            rows, name_lookup, media_types=_ANDROID_MEDIA_TYPES, to_iso=_ms_to_iso
        )
    finally:
        conn.close()

    chats = {r["chat_jid"] for r in records}
    return records, {
        "kind": "android_sqlite",
        "schema": schema,
        "chats": len(chats),
        "system_rows_skipped": skipped_system,
    }


def _sqlite_rows_to_records(rows, name_lookup, media_types, to_iso):
    records: List[Dict[str, Any]] = []
    skipped_system = 0
    for row in rows:
        chat_jid = row["chat_jid"] or ""
        if not chat_jid or _is_status_jid(chat_jid):
            continue
        text = row["text"]
        try:
            mtype = int(row["mtype"] or 0)
        except (TypeError, ValueError):
            mtype = 0
        is_media = mtype in media_types
        if not text and not is_media:
            skipped_system += 1
            continue

        timestamp = to_iso(row["ts"])
        if not timestamp:
            continue
        from_me = bool(row["from_me"])
        is_group = chat_jid.endswith("@g.us")
        sender_jid = (row["sender_jid"] or "") if (is_group and not from_me) else (
            "" if from_me else chat_jid
        )
        sender_phone = None if from_me else _jid_phone(sender_jid or chat_jid)
        sender_name = (
            "Me"
            if from_me
            else name_lookup.get(sender_jid or chat_jid)
            or sender_phone
            or (sender_jid or chat_jid).split("@")[0]
        )
        msg_id = str(row["key_id"] or "") or _stable_id(
            chat_jid, timestamp, sender_name, str(text or "")
        )
        records.append({
            "id": msg_id,
            "from": "me" if from_me else (sender_jid or chat_jid),
            "from_phone": sender_phone,
            "chat_jid": chat_jid,
            "sender_name": sender_name,
            "text": text,
            "timestamp": timestamp,
            "is_group": is_group,
            "is_from_me": from_me,
            "type": "media" if is_media and not text else "text",
        })
    return records, skipped_system


def _ios_media_dict(kind: str, mi: Optional[sqlite3.Row]) -> Dict[str, Any]:
    """Build a message `media` sub-document from a ZWAMEDIAITEM row.

    Starts as a placeholder (no bytes); the bundle-import step fills
    file_id/thumb_id when the actual file is present in the uploaded bundle.
    ZVCARDSTRING is overloaded by WhatsApp: a real vCard for contact
    messages, the mimetype string (e.g. 'image/jpeg') for most others.
    """
    local_path = mi["ZMEDIALOCALPATH"] if mi else None
    vcs = (mi["ZVCARDSTRING"] or "") if mi else ""

    if kind == "audio" and local_path and local_path.lower().endswith(".opus"):
        kind = "voice"  # WhatsApp voice notes are .opus; shared audio files aren't

    media: Dict[str, Any] = {"kind": kind, "file_id": None, "thumb_id": None,
                             "placeholder": True}
    if local_path:
        media["local_path"] = local_path
    if mi:
        if kind == "contact":
            if vcs:
                media["vcard"] = vcs
            if mi["ZVCARDNAME"]:
                media["contact_name"] = mi["ZVCARDNAME"]
        elif "/" in vcs and len(vcs) < 100:
            media["mimetype"] = vcs
        if kind == "location":
            media["latitude"] = mi["ZLATITUDE"]
            media["longitude"] = mi["ZLONGITUDE"]
            if mi["ZTITLE"]:
                media["location_name"] = mi["ZTITLE"]
            media["placeholder"] = False
        elif kind == "document" and mi["ZTITLE"]:
            media["filename"] = mi["ZTITLE"]
        # ZMOVIEDURATION is overloaded (e.g. PDF page count for documents) —
        # only meaningful as seconds for playable media.
        if mi["ZMOVIEDURATION"] and kind in ("audio", "voice", "video", "gif"):
            media["duration"] = int(mi["ZMOVIEDURATION"])
        if mi["ZFILESIZE"]:
            media["size"] = int(mi["ZFILESIZE"])
    if kind == "contact":
        media["placeholder"] = False
    return media


def parse_ios_chatstorage(
    db_path: str, name_lookup: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse an iOS ChatStorage.sqlite database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        member_names: Dict[int, Tuple[str, str]] = {}
        if "ZWAGROUPMEMBER" in _table_names(conn):
            for r in conn.execute(
                "SELECT Z_PK, ZMEMBERJID, ZCONTACTNAME FROM ZWAGROUPMEMBER"
            ):
                member_names[r["Z_PK"]] = (r["ZMEMBERJID"] or "", r["ZCONTACTNAME"] or "")

        media_items: Dict[int, sqlite3.Row] = {}
        if "ZWAMEDIAITEM" in _table_names(conn):
            for r in conn.execute(
                """
                SELECT Z_PK, ZMEDIALOCALPATH, ZVCARDSTRING, ZVCARDNAME, ZTITLE,
                       ZMOVIEDURATION, ZFILESIZE, ZLATITUDE, ZLONGITUDE
                FROM ZWAMEDIAITEM
                """
            ):
                media_items[r["Z_PK"]] = r

        rows = conn.execute(
            """
            SELECT m.ZSTANZAID    AS key_id,
                   m.ZISFROMME    AS from_me,
                   m.ZMESSAGEDATE AS ts,
                   m.ZTEXT        AS text,
                   m.ZMESSAGETYPE AS mtype,
                   m.ZFROMJID     AS from_jid,
                   m.ZGROUPMEMBER AS group_member_pk,
                   m.ZMEDIAITEM   AS media_pk,
                   s.ZCONTACTJID  AS chat_jid,
                   s.ZPARTNERNAME AS partner_name,
                   s.ZSESSIONTYPE AS session_type
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
            ORDER BY m.ZMESSAGEDATE
            """
        ).fetchall()

        records: List[Dict[str, Any]] = []
        skipped_system = 0
        for row in rows:
            chat_jid = row["chat_jid"] or ""
            if not chat_jid or _is_status_jid(chat_jid):
                continue
            text = row["text"]
            mtype = int(row["mtype"] or 0)
            kind = _IOS_KIND_BY_TYPE.get(mtype)
            media = (
                _ios_media_dict(kind, media_items.get(row["media_pk"]))
                if kind else None
            )
            if not text and media is None:
                skipped_system += 1
                continue
            try:
                dt = _APPLE_EPOCH + timedelta(seconds=float(row["ts"] or 0))
                timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except (OverflowError, ValueError):
                continue

            from_me = bool(row["from_me"])
            is_group = chat_jid.endswith("@g.us")
            sender_jid, member_name = "", ""
            if not from_me:
                if is_group and row["group_member_pk"] in member_names:
                    sender_jid, member_name = member_names[row["group_member_pk"]]
                sender_jid = sender_jid or row["from_jid"] or chat_jid
            sender_phone = None if from_me else _jid_phone(sender_jid)
            sender_name = (
                "Me"
                if from_me
                else member_name
                or (None if is_group else row["partner_name"])
                or name_lookup.get(sender_jid)
                or sender_phone
                or sender_jid.split("@")[0]
            )
            msg_id = str(row["key_id"] or "") or _stable_id(
                chat_jid, timestamp, sender_name, str(text or "")
            )
            record = {
                "id": msg_id,
                "chat_name": row["partner_name"],
                "from": "me" if from_me else sender_jid,
                "from_phone": sender_phone,
                "chat_jid": chat_jid,
                "sender_name": sender_name,
                "text": text,
                "timestamp": timestamp,
                "is_group": is_group,
                "is_from_me": from_me,
                "type": media["kind"] if media else "text",
            }
            if media:
                record["media"] = media
            records.append(record)
    finally:
        conn.close()

    chats = {r["chat_jid"] for r in records}
    return records, {
        "kind": "ios_sqlite",
        "chats": len(chats),
        "system_rows_skipped": skipped_system,
    }
