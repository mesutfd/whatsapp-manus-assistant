"""
Binary media storage backed by MongoDB GridFS.

Message documents hold a `media` sub-document (kind, mimetype, caption
metadata, ...) plus GridFS ids: `file_id` for the original bytes and, for
images, `thumb_id` for a small JPEG preview. This module owns the GridFS
bucket and thumbnail generation; serving happens in app.api.media.
"""

import io
import logging
from typing import Any, Dict, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from app.core.mongo import get_db

logger = logging.getLogger(__name__)

BUCKET_NAME = "media"
THUMB_MAX_DIM = 480
THUMB_JPEG_QUALITY = 75

# Media kinds a message can carry. "voice" is a WhatsApp voice note (PTT),
# "audio" a shared audio file; "gif" is WhatsApp's mp4-with-gifPlayback.
MEDIA_KINDS = (
    "image", "video", "gif", "audio", "voice",
    "document", "sticker", "contact", "location",
)


def _bucket() -> AsyncIOMotorGridFSBucket:
    return AsyncIOMotorGridFSBucket(get_db(), bucket_name=BUCKET_NAME)


def make_image_thumbnail(data: bytes) -> Optional[bytes]:
    """Downscale image bytes to a small JPEG. Returns None if the bytes
    aren't a decodable image. Synchronous — call via asyncio.to_thread."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            # Flatten transparency onto white so JPEG doesn't go black.
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=THUMB_JPEG_QUALITY)
        return out.getvalue()
    except Exception as e:
        logger.debug("Thumbnail generation failed: %s", e)
        return None


async def save_bytes(
    data: bytes,
    filename: Optional[str] = None,
    mimetype: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Store one blob in GridFS, returns its id as a hex string."""
    meta = dict(metadata or {})
    if mimetype:
        meta["mimetype"] = mimetype
    file_id = await _bucket().upload_from_stream(
        filename or "file", data, metadata=meta
    )
    return str(file_id)


async def open_stream(file_id: str):
    """Open a GridFS download stream. Returns (stream, length, mimetype,
    filename) or raises FileNotFoundError."""
    try:
        oid = ObjectId(file_id)
    except (InvalidId, TypeError):
        raise FileNotFoundError(file_id)
    try:
        stream = await _bucket().open_download_stream(oid)
    except Exception:
        raise FileNotFoundError(file_id)
    meta = stream.metadata or {}
    return stream, stream.length, meta.get("mimetype"), stream.filename


async def delete_all() -> int:
    """Drop every stored media blob (used by full re-imports)."""
    db = get_db()
    files = db[f"{BUCKET_NAME}.files"]
    count = await files.count_documents({})
    await files.drop()
    await db[f"{BUCKET_NAME}.chunks"].drop()
    return count
