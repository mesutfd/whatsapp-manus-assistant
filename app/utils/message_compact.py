"""Compact message records for API responses.

Stored messages can carry very long pasted texts and verbose media metadata;
returning them raw makes list endpoints (recent chats, search, recent
messages) explode for large imported histories. These helpers truncate text
(with an explicit flag so callers know to re-fetch the full message) and slim
media dicts down to the fields API/LLM consumers actually use.
"""

from typing import Any, Dict

# Media metadata worth returning; the rest (local paths, thumbnail ids, ...)
# is storage detail.
_MEDIA_KEEP_KEYS = ("kind", "file_id", "mimetype", "duration", "size", "placeholder")


def compact_message(msg: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """Copy of a message record with long text truncated and media slimmed.

    Truncation is flagged via `text_truncated` / `full_text_chars` so a
    caller can fetch the message again with a higher cap when it needs the
    full text. `max_chars=0` disables text truncation.
    """
    out = dict(msg)
    text = out.get("text")
    if max_chars and isinstance(text, str) and len(text) > max_chars:
        out["text"] = text[:max_chars]
        out["text_truncated"] = True
        out["full_text_chars"] = len(text)
    media = out.get("media")
    if isinstance(media, dict):
        out["media"] = {k: media[k] for k in _MEDIA_KEEP_KEYS if k in media}
    return out
