"""
Webhooks API endpoints.
Manage webhook registrations for pushing events to external services (Manus, n8n, etc.)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.core.webhooks import webhook_service
from app.models.schemas import WebhookInfo, WebhookRegisterRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.get("/")
async def list_webhooks(user: dict = Depends(get_current_user)):
    """
    List all registered webhooks.
    """
    webhooks = webhook_service.list_webhooks()
    return {"webhooks": webhooks, "total": len(webhooks)}


@router.post("/register")
async def register_webhook(
    request: WebhookRegisterRequest,
    user: dict = Depends(get_current_user),
):
    """
    Register a new webhook endpoint.
    Events will be POSTed to this URL when they occur.

    Available events:
    - message: New incoming message
    - message_sent: Message sent successfully
    - auto_reply_sent: Auto-reply was sent
    - connected: WhatsApp connected
    - disconnected: WhatsApp disconnected
    - qr: New QR code available
    - receipt: Message delivery receipt
    - *: All events
    """
    webhook = webhook_service.register_webhook(
        url=request.url,
        events=request.events,
        secret=request.secret,
        name=request.name,
    )
    return {"success": True, "webhook": webhook}


@router.delete("/{webhook_id}")
async def remove_webhook(webhook_id: int, user: dict = Depends(get_current_user)):
    """
    Remove a webhook by ID.
    """
    success = webhook_service.remove_webhook(webhook_id)
    if not success:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"success": True, "message": f"Webhook {webhook_id} removed"}
