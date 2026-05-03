"""
iDeep AI Assistant API endpoints.
Handles auto-reply configuration, assistant behavior, and smart responses.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.whatsapp_client import wa_client
from app.models.schemas import (
    AutoReplyConfig,
    AutoReplyRule,
    AutoReplyStatus,
    SendMessageRequest,
    SendMessageResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assistant", tags=["iDeep AI Assistant"])


@router.get("/config", response_model=AutoReplyStatus)
async def get_assistant_config(user: dict = Depends(get_current_user)):
    """
    Get current iDeep AI Assistant configuration.
    """
    config = wa_client.get_auto_reply_config()
    return AutoReplyStatus(**config)


@router.put("/config")
async def update_assistant_config(
    config: AutoReplyConfig,
    user: dict = Depends(get_current_user),
):
    """
    Update iDeep AI Assistant configuration.
    Enable/disable auto-reply, set default message, and configure rules.
    """
    rules = None
    if config.rules:
        rules = [rule.model_dump() for rule in config.rules]

    wa_client.set_auto_reply(
        enabled=config.enabled,
        message=config.message,
        rules=rules,
    )

    if config.assistant_name:
        settings.ASSISTANT_NAME = config.assistant_name

    return {
        "success": True,
        "message": f"Assistant {'enabled' if config.enabled else 'disabled'}",
        "config": wa_client.get_auto_reply_config(),
    }


@router.post("/reply")
async def send_assistant_reply(
    request: SendMessageRequest,
    user: dict = Depends(get_current_user),
):
    """
    Send a message as the iDeep AI Assistant.
    Prefixes the message with the assistant name.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    # Format as assistant message
    assistant_msg = f"*{settings.ASSISTANT_NAME}*\n\n{request.message}"
    result = await wa_client.send_message(request.phone, assistant_msg)
    return SendMessageResponse(**result)


@router.post("/reply-as-assistant")
async def reply_as_assistant(
    phone: str,
    message: str,
    context: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """
    Send a contextual reply as iDeep AI Assistant.
    Used by Manus to reply on behalf of the user with context.

    Example: Manus tells iDeep to reply to Masoud:
    "Hi I am iDeep AI Assistant, Alireza is not available right now,
    but he would be available in few days and makes contact."
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    # Build contextual assistant message
    full_message = f"*{settings.ASSISTANT_NAME}*\n\n{message}"

    result = await wa_client.send_message(phone, full_message)
    return {
        "success": result.get("success"),
        "to": phone,
        "message_sent": full_message,
        "context": context,
        "timestamp": result.get("timestamp"),
    }


@router.post("/rules/add")
async def add_auto_reply_rule(
    rule: AutoReplyRule,
    user: dict = Depends(get_current_user),
):
    """
    Add a new auto-reply rule.
    Rules can match by contact, keyword, or both.
    """
    current_rules = wa_client._auto_reply_rules
    current_rules.append(rule.model_dump())
    wa_client._auto_reply_rules = current_rules

    return {
        "success": True,
        "message": "Rule added successfully",
        "total_rules": len(current_rules),
    }


@router.delete("/rules/{rule_index}")
async def remove_auto_reply_rule(
    rule_index: int,
    user: dict = Depends(get_current_user),
):
    """
    Remove an auto-reply rule by index.
    """
    rules = wa_client._auto_reply_rules
    if rule_index < 0 or rule_index >= len(rules):
        raise HTTPException(status_code=404, detail="Rule not found")

    removed = rules.pop(rule_index)
    return {
        "success": True,
        "removed_rule": removed,
        "remaining_rules": len(rules),
    }


@router.get("/rules")
async def list_auto_reply_rules(user: dict = Depends(get_current_user)):
    """
    List all auto-reply rules.
    """
    return {
        "rules": wa_client._auto_reply_rules,
        "total": len(wa_client._auto_reply_rules),
    }
