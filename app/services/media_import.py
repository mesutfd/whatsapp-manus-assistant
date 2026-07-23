"""
Attach media binaries from a backup bundle to parsed message records.

A "bundle" is a .zip produced by scripts/export_ios_backup_bundle.py:
ChatStorage.sqlite at the root plus the referenced media files stored at
their ZMEDIALOCALPATH-relative paths (Media/<jid>/x/y/<uuid>.<ext>).

For every record whose media has a `local_path` present in the zip, the
original bytes go to GridFS and images/stickers additionally get a small
JPEG thumbnail. Everything else stays a placeholder.
"""

import asyncio
import logging
import mimetypes
import zipfile
from typing import Any, Dict, List, Optional

from app.core import media_store

logger = logging.getLogger(__name__)

VIDEO_KINDS = ("video", "gif")
_THUMB_KINDS = ("image", "sticker")


def _guess_mimetype(media: Dict[str, Any]) -> Optional[str]:
    if media.get("mimetype"):
        return media["mimetype"]
    path = media.get("local_path") or media.get("filename") or ""
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if path.lower().endswith(".opus"):
        return "audio/ogg"
    return None


async def attach_media_from_zip(
    records: List[Dict[str, Any]],
    zip_path: str,
    include_videos: bool = False,
) -> Dict[str, int]:
    """Fill file_id/thumb_id on records' media from the bundle zip.
    Mutates records in place; returns import statistics."""
    stats = {
        "media_attached": 0,
        "thumbnails_created": 0,
        "media_missing_from_bundle": 0,
        "videos_skipped": 0,
    }

    archive = zipfile.ZipFile(zip_path)
    names = set(archive.namelist())

    try:
        for record in records:
            media = record.get("media")
            if not media:
                continue
            local_path = media.get("local_path")
            if not local_path:
                continue
            if media["kind"] in VIDEO_KINDS and not include_videos:
                stats["videos_skipped"] += 1
                continue
            if local_path not in names:
                stats["media_missing_from_bundle"] += 1
                continue

            try:
                data = await asyncio.to_thread(archive.read, local_path)
            except Exception as e:
                logger.warning("Unreadable bundle entry %s: %s", local_path, e)
                stats["media_missing_from_bundle"] += 1
                continue

            mimetype = _guess_mimetype(media)
            filename = media.get("filename") or local_path.rsplit("/", 1)[-1]
            media["file_id"] = await media_store.save_bytes(
                data, filename=filename, mimetype=mimetype,
                metadata={"kind": media["kind"], "role": "original"},
            )
            media["size"] = len(data)
            if mimetype:
                media["mimetype"] = mimetype
            media["placeholder"] = False
            stats["media_attached"] += 1

            if media["kind"] in _THUMB_KINDS:
                thumb = await asyncio.to_thread(media_store.make_image_thumbnail, data)
                if thumb:
                    media["thumb_id"] = await media_store.save_bytes(
                        thumb, filename="thumb.jpg", mimetype="image/jpeg",
                        metadata={"kind": media["kind"], "role": "thumbnail"},
                    )
                    stats["thumbnails_created"] += 1
    finally:
        archive.close()

    return stats
