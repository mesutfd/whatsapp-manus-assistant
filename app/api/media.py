"""
Media API — streams stored media binaries (originals and thumbnails)
out of GridFS. Message documents reference these via media.file_id /
media.thumb_id.
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core import media_store
from app.core.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/media", tags=["Media"])

_CHUNK = 1024 * 1024


@router.get("/{file_id}")
async def get_media(
    file_id: str,
    download: bool = False,
    user: dict = Depends(get_current_user),
):
    """Stream one stored media file. `?download=1` forces a save-as
    Content-Disposition with the original filename."""
    try:
        stream, length, mimetype, filename = await media_store.open_stream(file_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Media not found")

    async def iterate():
        try:
            while True:
                chunk = await stream.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            await stream.close()

    headers = {"Content-Length": str(length), "Cache-Control": "private, max-age=86400"}
    if download and filename:
        safe = re.sub(r'[\r\n"\\]', "_", filename)
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return StreamingResponse(
        iterate(), media_type=mimetype or "application/octet-stream", headers=headers
    )
