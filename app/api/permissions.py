"""
Permissions / Allowed-Contacts API.

Lets the panel curate which WhatsApp contacts the assistant (Manus / LLM) is
allowed to message. Provides full CRUD plus a master on/off toggle and an
LLM-facing instructions endpoint so the agent knows exactly who it can reach
and how to recognise them.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.schemas import (
    AllowedContact,
    AllowedContactCreate,
    AllowedContactUpdate,
    PermissionsCheckResponse,
    PermissionsConfig,
    PermissionsToggle,
)
from app.services.allowed_contacts import allowed_contacts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/permissions", tags=["Permissions (Allowed Contacts)"])


# ─── State & Toggle ──────────────────────────────────────────────────────────


@router.get("/", response_model=PermissionsConfig)
async def get_permissions(user: dict = Depends(get_current_user)):
    """Return the master toggle plus the full allow-list."""
    state = await allowed_contacts_service.get_state()
    return PermissionsConfig(**state)


@router.put("/toggle", response_model=PermissionsConfig)
async def toggle_permissions(
    body: PermissionsToggle,
    user: dict = Depends(get_current_user),
):
    """Turn allow-list enforcement on/off globally."""
    state = await allowed_contacts_service.set_enabled(body.enabled)
    return PermissionsConfig(**state)


@router.get("/check", response_model=PermissionsCheckResponse)
async def check_allowed(
    phone: str = Query(..., description="Phone number to test against the allow-list"),
    user: dict = Depends(get_current_user),
):
    """Quick check used by the UI / LLM before attempting a send."""
    result = await allowed_contacts_service.check_allowed(phone)
    return PermissionsCheckResponse(**result)


# ─── CRUD ────────────────────────────────────────────────────────────────────


@router.get("/contacts", response_model=List[AllowedContact])
async def list_contacts(user: dict = Depends(get_current_user)):
    """List every allow-listed contact (in insertion order)."""
    return await allowed_contacts_service.list_contacts()


@router.post("/contacts", response_model=AllowedContact, status_code=201)
async def create_contact(
    body: AllowedContactCreate,
    user: dict = Depends(get_current_user),
):
    """Add a new contact to the allow-list."""
    try:
        return await allowed_contacts_service.add_contact(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/contacts/{contact_id}", response_model=AllowedContact)
async def get_contact(contact_id: str, user: dict = Depends(get_current_user)):
    contact = await allowed_contacts_service.get_contact(contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.put("/contacts/{contact_id}", response_model=AllowedContact)
async def update_contact(
    contact_id: str,
    body: AllowedContactUpdate,
    user: dict = Depends(get_current_user),
):
    """Partially update an existing allow-listed contact."""
    try:
        updated = await allowed_contacts_service.update_contact(
            contact_id, body.model_dump(exclude_unset=True)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return updated


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: str, user: dict = Depends(get_current_user)):
    removed = await allowed_contacts_service.delete_contact(contact_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"success": True, "id": contact_id}


# ─── LLM Instructions ────────────────────────────────────────────────────────


@router.get("/instructions")
async def llm_instructions(user: dict = Depends(get_current_user)):
    """
    Machine-readable instructions for the LLM (Manus, etc.).

    Designed to be fetched at the start of an agent session so the model knows:
    - Whether outbound messages are restricted to a curated allow-list
    - Which contacts it may message and how to recognise each one
    - Tone / language / relation hints attached to each contact
    """
    state = await allowed_contacts_service.get_state()
    enforced = bool(state["enabled"])
    contacts = state["contacts"]

    if enforced:
        policy = (
            "Outbound WhatsApp messages are RESTRICTED. You may only send to "
            "contacts listed in `allowed_contacts` whose `enabled` field is true. "
            "Match user requests against `name`, `phone`, `relation`, "
            "`llm_friendly_names`, `tags`, and `attributes`. If the user names a "
            "person not on this list, do NOT send — explain that this contact is "
            "not on the allow-list and ask the user to add them via the panel."
        )
    else:
        policy = (
            "Outbound WhatsApp permissions are currently UNRESTRICTED — the user "
            "has not turned on the allow-list. You may message any resolvable "
            "WhatsApp contact, but still confirm with the user before sending to "
            "anyone whose identity you are unsure of."
        )

    return {
        "assistant_name": settings.ASSISTANT_NAME,
        "send_policy": {
            "enforced": enforced,
            "description": policy,
        },
        "allowed_contacts": [
            {
                "id": c["id"],
                "name": c["name"],
                "phone": c["phone"],
                "relation": c.get("relation"),
                "llm_friendly_names": c.get("llm_friendly_names", []),
                "tags": c.get("tags", []),
                "notes": c.get("notes"),
                "attributes": c.get("attributes", {}),
                "enabled": c.get("enabled", True),
            }
            for c in contacts
        ],
        "total_contacts": len(contacts),
        "active_contacts": sum(1 for c in contacts if c.get("enabled", True)),
    }
