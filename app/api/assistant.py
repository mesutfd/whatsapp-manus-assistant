"""
iDeep AI Assistant API endpoints.
Handles auto-reply configuration, rules, contact personas, LLM info.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import db
from app.core.llm import llm_client
from app.core.whatsapp_client import wa_client
from app.models.schemas import (
    AutoReplyConfig,
    AutoReplyRule,
    AutoReplyRuleUpdate,
    AutoReplyStatus,
    ContactPersona,
    LLMInfo,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.allowed_contacts import allowed_contacts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assistant", tags=["iDeep AI Assistant"])


# ─── Config ──────────────────────────────────────────────────────────────────


@router.get("/config", response_model=AutoReplyStatus)
async def get_assistant_config(user: dict = Depends(get_current_user)):
    """Get current iDeep AI Assistant configuration."""
    cfg = await wa_client.get_auto_reply_config()
    return AutoReplyStatus(**cfg)


@router.put("/config", response_model=AutoReplyStatus)
async def update_assistant_config(
    config: AutoReplyConfig,
    user: dict = Depends(get_current_user),
):
    """
    Update iDeep AI Assistant configuration.
    Any unset field is left untouched.
    """
    fields = {}
    if config.enabled is not None:
        fields["enabled"] = config.enabled
    if config.message is not None:
        fields["default_message"] = config.message
    if config.assistant_name is not None:
        fields["assistant_name"] = config.assistant_name
    if config.llm_enabled is not None:
        fields["llm_enabled"] = config.llm_enabled
    if config.llm_system_prompt is not None:
        fields["llm_system_prompt"] = config.llm_system_prompt

    if config.quiet_hours is not None:
        qh = config.quiet_hours
        fields.update({
            "quiet_hours_enabled": qh.enabled,
            "quiet_hours_start": qh.start,
            "quiet_hours_end": qh.end,
            "quiet_hours_timezone": qh.timezone,
            "quiet_hours_message": qh.message,
            "quiet_hours_defer_scheduled": qh.defer_scheduled,
        })

    cfg = await wa_client.set_auto_reply_config(**fields)
    return AutoReplyStatus(**cfg)


@router.get("/llm", response_model=LLMInfo)
async def get_llm_info(user: dict = Depends(get_current_user)):
    """Read-only view of the LLM provider configured via .env."""
    return LLMInfo(**llm_client.info())


# ─── Quick send / Manus integration helpers ──────────────────────────────────


@router.post("/reply")
async def send_assistant_reply(
    request: SendMessageRequest,
    user: dict = Depends(get_current_user),
):
    """Send a message prefixed with the assistant name."""
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
                "hint": "Add this contact to the allow-list in the Permissions tab.",
            },
        )

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
    """Contextual reply used by Manus."""
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
                "hint": "Add this contact to the allow-list in the Permissions tab.",
            },
        )

    full_message = f"*{settings.ASSISTANT_NAME}*\n\n{message}"
    result = await wa_client.send_message(phone, full_message)
    return {
        "success": result.get("success"),
        "to": phone,
        "message_sent": full_message,
        "context": context,
        "timestamp": result.get("timestamp"),
    }


# ─── Rules (CRUD) ────────────────────────────────────────────────────────────


@router.get("/rules")
async def list_rules(user: dict = Depends(get_current_user)):
    rules = await db.list_rules()
    return {"rules": rules, "total": len(rules)}


@router.post("/rules")
async def add_rule(rule: AutoReplyRule, user: dict = Depends(get_current_user)):
    """Add a new auto-reply rule."""
    if not (rule.message or rule.use_llm):
        raise HTTPException(
            status_code=400,
            detail="Provide either a static reply message or enable use_llm.",
        )
    created = await db.add_rule(
        contact=rule.contact,
        keyword=rule.keyword,
        message=rule.message or "",
        match_mode=rule.match_mode,
        use_llm=rule.use_llm,
        cooldown_seconds=rule.cooldown_seconds,
        enabled=rule.enabled,
        priority=rule.priority,
    )
    return {"success": True, "rule": created}


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    update: AutoReplyRuleUpdate,
    user: dict = Depends(get_current_user),
):
    existing = await db.get_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    updated = await db.update_rule(rule_id, **update.model_dump(exclude_unset=True))
    return {"success": True, "rule": updated}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, user: dict = Depends(get_current_user)):
    ok = await db.delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"success": True, "id": rule_id}


# ─── Backwards-compat: POST /rules/add (was used by older UI) ────────────────


@router.post("/rules/add")
async def add_rule_legacy(rule: AutoReplyRule, user: dict = Depends(get_current_user)):
    return await add_rule(rule, user)


# ─── Contact personas ────────────────────────────────────────────────────────


@router.get("/personas")
async def list_personas(user: dict = Depends(get_current_user)):
    personas = await db.list_personas()
    return {"personas": personas, "total": len(personas)}


@router.put("/personas")
async def upsert_persona(
    persona: ContactPersona,
    user: dict = Depends(get_current_user),
):
    """Create or update the persona for a contact (keyed by JID or phone)."""
    if not persona.contact.strip():
        raise HTTPException(status_code=400, detail="contact is required")
    saved = await db.upsert_persona(
        contact=persona.contact.strip(),
        display_name=persona.display_name,
        notes=persona.notes,
        system_prompt_override=persona.system_prompt_override,
        use_llm=persona.use_llm,
    )
    return {"success": True, "persona": saved}


@router.delete("/personas/{contact}")
async def delete_persona(contact: str, user: dict = Depends(get_current_user)):
    ok = await db.delete_persona(contact)
    if not ok:
        raise HTTPException(status_code=404, detail="Persona not found")
    return {"success": True, "contact": contact}
