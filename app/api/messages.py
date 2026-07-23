"""
Messages API endpoints.
Handles sending, receiving, searching, and managing WhatsApp messages.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.core.auth import get_current_user
from app.core.message_history import message_history_db
from app.core.whatsapp_client import wa_client
from app.models.schemas import (
    BulkMessageRequest,
    BulkMessageResponse,
    ChatInfo,
    MessageRecord,
    SearchRequest,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.allowed_contacts import allowed_contacts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/messages", tags=["Messages"])


@router.post("/send", response_model=SendMessageResponse)
async def send_message(request: SendMessageRequest, user: dict = Depends(get_current_user)):
    """
    Send a text message to a WhatsApp number.
    Phone number should include country code without '+' (e.g., 989123456789).
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    permission = await allowed_contacts_service.check_allowed(request.phone)
    if not permission["allowed"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "recipient_not_allowed",
                "phone": permission["phone"],
                "reason": permission["reason"],
                "hint": "Add this contact to the allow-list (Permissions tab) or turn off enforcement.",
            },
        )

    result = await wa_client.send_message(request.phone, request.message)
    return SendMessageResponse(**result)


_MAX_MEDIA_UPLOAD = 128 * 1024 * 1024  # WhatsApp caps media around 100 MB

# UI/API "kind" values accepted for outgoing media; anything else (or an
# unrecognized mimetype) is sent as a document.
_SENDABLE_KINDS = {"image", "video", "gif", "audio", "voice", "document", "sticker"}


def _kind_from_mimetype(mimetype: str) -> str:
    if mimetype.startswith("image/"):
        return "sticker" if mimetype == "image/webp" else "image"
    if mimetype.startswith("video/"):
        return "video"
    if mimetype.startswith("audio/"):
        return "audio"
    return "document"


@router.post("/send-media", response_model=SendMessageResponse)
async def send_media(
    phone: str = Form(..., description="Recipient phone, digits with country code"),
    file: UploadFile = File(..., description="The media file to send"),
    caption: str = Form("", description="Optional caption (images/videos/documents)"),
    kind: str = Form("", description="Optional override: image|video|gif|audio|voice|document|sticker"),
    user: dict = Depends(get_current_user),
):
    """
    Send a media message. The media kind is derived from the file's mimetype
    unless `kind` is given explicitly (e.g. `voice` to send an audio file as
    a WhatsApp voice note).
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    permission = await allowed_contacts_service.check_allowed(phone)
    if not permission["allowed"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "recipient_not_allowed",
                "phone": permission["phone"],
                "reason": permission["reason"],
                "hint": "Add this contact to the allow-list (Permissions tab) or turn off enforcement.",
            },
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MAX_MEDIA_UPLOAD:
        raise HTTPException(status_code=413, detail="Media file too large (max 128 MB)")

    mimetype = file.content_type or "application/octet-stream"
    resolved_kind = kind.strip().lower()
    if resolved_kind not in _SENDABLE_KINDS:
        resolved_kind = _kind_from_mimetype(mimetype)

    result = await wa_client.send_media(
        phone=phone,
        data=data,
        kind=resolved_kind,
        mimetype=mimetype,
        filename=file.filename,
        caption=caption.strip(),
    )
    return SendMessageResponse(**{k: v for k, v in result.items() if k in
                                  {"success", "to", "message", "timestamp", "message_id", "error"}})


@router.post("/send-bulk", response_model=BulkMessageResponse)
async def send_bulk_messages(request: BulkMessageRequest, user: dict = Depends(get_current_user)):
    """
    Send the same message to multiple recipients with anti-spam delay.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    results = []
    sent = 0
    failed = 0

    for phone in request.phones:
        permission = await allowed_contacts_service.check_allowed(phone)
        if not permission["allowed"]:
            results.append(SendMessageResponse(
                success=False,
                to=phone,
                message=request.message,
                error=f"recipient_not_allowed: {permission['reason']}",
            ))
            failed += 1
            continue

        result = await wa_client.send_message(phone, request.message)
        response = SendMessageResponse(**result)
        results.append(response)

        if result.get("success"):
            sent += 1
        else:
            failed += 1

        # Anti-spam delay
        await asyncio.sleep(request.delay_seconds)

    return BulkMessageResponse(
        total=len(request.phones),
        sent=sent,
        failed=failed,
        results=results,
    )


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(300, ge=1, le=1000, description="Maximum conversations to return"),
    user: dict = Depends(get_current_user),
):
    """
    All conversations from the persisted message store (live + imported),
    with last-message preview — works even while WhatsApp is disconnected.
    Names resolve to contact names where known (stored chat name, live
    WhatsApp contacts cache, then the permissions allow-list).
    """
    conversations = await message_history_db.list_conversations(limit)

    # Overlay better display names for chats that only have a raw phone/jid.
    contacts_cache = getattr(wa_client, "_contacts_cache", None) or {}
    try:
        allowed = await allowed_contacts_service.list_contacts()
    except Exception:
        allowed = []
    allowed_by_phone = {
        "".join(ch for ch in (c.get("phone") or "") if ch.isdigit()): c.get("name")
        for c in allowed
        if c.get("name")
    }
    for conv in conversations:
        if conv["name"] and conv["name"] != (conv.get("phone") or conv["chat_jid"].split("@")[0]):
            continue  # already a human name
        cached = contacts_cache.get(conv["chat_jid"]) or {}
        cached_name = cached.get("name")
        if cached_name and cached_name != "Unknown":
            conv["name"] = cached_name
            continue
        phone = conv.get("phone") or ""
        if phone and phone in allowed_by_phone:
            conv["name"] = allowed_by_phone[phone]

    return {"conversations": conversations, "total": len(conversations)}


@router.get("/conversation")
async def get_conversation(
    chat_jid: str = Query(..., description="Chat JID, e.g. 989123456789@s.whatsapp.net"),
    limit: int = Query(50, ge=1, le=500, description="Messages per page"),
    before: str = Query("", description="Return messages older than this timestamp (for paging up)"),
    user: dict = Depends(get_current_user),
):
    """
    One conversation's messages (oldest-first within the page), from the
    persisted store. Pass `before` = the oldest timestamp you have to load
    the previous page.
    """
    messages = await message_history_db.get_chat(chat_jid, limit, before)
    return {
        "chat_jid": chat_jid,
        "messages": messages,
        "count": len(messages),
        "has_more": len(messages) == limit,
    }


@router.get("/chats")
async def get_chats(
    limit: int = Query(50, description="Maximum chats to return"),
    user: dict = Depends(get_current_user),
):
    """
    Get all recent chats/conversations with last message preview.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    chats = await wa_client.get_chats()
    return {"chats": chats[:limit], "total": len(chats)}


@router.get("/chat/{phone}")
async def get_chat_messages(
    phone: str,
    limit: int = Query(50, description="Maximum messages to return"),
    user: dict = Depends(get_current_user),
):
    """
    Get messages from a specific chat by phone number.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    messages = await wa_client.get_chat_messages(phone, limit)
    return {"phone": phone, "messages": messages, "count": len(messages)}


@router.post("/search")
async def search_messages(request: SearchRequest, user: dict = Depends(get_current_user)):
    """
    Search through stored messages by text content or contact name.
    Useful for queries like: "when was my date with Masoud Nayebi?"
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    results = await wa_client.search_messages(request.query, request.contact)
    return {
        "query": request.query,
        "contact_filter": request.contact,
        "results": results[:request.limit],
        "total_matches": len(results),
    }


@router.get("/history")
async def get_message_history(
    limit: int = Query(100, description="Number of messages"),
    offset: int = Query(0, description="Offset for pagination"),
    user: dict = Depends(get_current_user),
):
    """
    Get stored message history with pagination.
    """
    messages = wa_client.get_stored_messages(limit, offset)
    return {
        "messages": messages,
        "limit": limit,
        "offset": offset,
        "total_stored": len(wa_client._message_store),
    }


@router.get("/unread")
async def get_unread_messages(user: dict = Depends(get_current_user)):
    """
    Get unread/recent messages since last check.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    # Return last 20 messages that are not from the user
    messages = [
        msg for msg in wa_client._message_store
        if not msg.get("is_from_me", False)
    ]
    return {"unread": messages[-20:], "count": len(messages[-20:])}
