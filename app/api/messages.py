"""
Messages API endpoints.
Handles sending, receiving, searching, and managing WhatsApp messages.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
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
