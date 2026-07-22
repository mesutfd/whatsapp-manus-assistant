"""
Smart Actions API - Intelligent endpoints designed for AI agent interaction.

These endpoints accept natural-language-style inputs (contact names instead of phone numbers)
and use fuzzy matching to resolve contacts before performing actions.

This is the PRIMARY interface for Manus integration.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.whatsapp_client import wa_client
from app.services.allowed_contacts import allowed_contacts_service
from app.utils.contact_resolver import resolve_contact, resolve_single_contact

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/smart", tags=["Smart Actions (Manus Interface)"])


# ─── Request Models ──────────────────────────────────────────────────────────


class SmartSendRequest(BaseModel):
    """Send a message using contact name (fuzzy matched) or phone number."""
    to: str = Field(
        ...,
        description="Contact name OR phone number. Examples: 'Masoud Nayebi', 'Mom', '989123456789'",
    )
    message: str = Field(..., description="Message text to send")
    as_assistant: bool = Field(
        False,
        description="If true, sends as iDeep AI Assistant with name prefix",
    )


class SmartSearchRequest(BaseModel):
    """Search messages with natural language query."""
    query: str = Field(
        ...,
        description="What to search for (e.g., 'date with Masoud', 'meeting tomorrow')",
    )
    contact: Optional[str] = Field(
        None,
        description="Contact name to filter by (fuzzy matched). E.g., 'Masoud Nayebi'",
    )
    limit: int = Field(20, description="Max results")


class ContactResolveRequest(BaseModel):
    """Resolve a contact name to phone number."""
    name: str = Field(..., description="Contact name to look up (fuzzy matched)")


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/send")
async def smart_send_message(request: SmartSendRequest, user: dict = Depends(get_current_user)):
    """
    **Smart Send** - Send a message by contact name (fuzzy matched) or phone number.

    This is the main endpoint for Manus to send messages. It handles:
    - Fuzzy name matching: "Masoud Nayebi" → finds "Masoud Nayebi-Tech Assistant"
    - Direct phone numbers: "989123456789" → sends directly
    - Assistant mode: Prefixes message with iDeep AI assistant name

    **Examples:**
    - `{"to": "Masoud Nayebi", "message": "Hey, let's meet tomorrow at 9PM"}`
    - `{"to": "Mom", "message": "I'll be home late"}`
    - `{"to": "Masoud", "message": "Alireza will contact you soon", "as_assistant": true}`
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    # Step 1: Resolve the contact (allow-list first, then WhatsApp contacts)
    resolved = await _resolve_recipient(request.to)

    if not resolved:
        return {
            "success": False,
            "error": "contact_not_found",
            "message": f"Could not find a contact matching '{request.to}'. Try a more specific name or use a phone number directly.",
            "suggestion": "Use /api/v1/smart/resolve to search for the contact first.",
        }

    # Step 2: Prepare message
    phone = resolved["phone"]
    message = request.message

    if request.as_assistant:
        message = f"*{settings.ASSISTANT_NAME}*\n\n{message}"

    # Step 3: Enforce the allow-list before sending
    permission = await allowed_contacts_service.check_allowed(phone)
    if not permission["allowed"]:
        return {
            "success": False,
            "error": "recipient_not_allowed",
            "resolved_contact": {
                "name": resolved.get("name"),
                "phone": resolved.get("phone"),
                "jid": resolved.get("jid"),
                "match_score": resolved.get("match_score"),
            },
            "reason": permission["reason"],
            "message": (
                f"Cannot send to '{resolved.get('name') or phone}': "
                f"{permission['reason']}. Ask the user to add this contact "
                "to the allow-list in the Permissions tab, or disable enforcement."
            ),
        }

    # Step 4: Send
    result = await wa_client.send_message(phone, message)

    return {
        "success": result.get("success", False),
        "resolved_contact": {
            "name": resolved.get("name"),
            "phone": resolved.get("phone"),
            "jid": resolved.get("jid"),
            "match_score": resolved.get("match_score"),
        },
        "message_sent": message,
        "timestamp": result.get("timestamp"),
        "message_id": result.get("message_id"),
        "error": result.get("error"),
    }


@router.post("/search")
async def smart_search(request: SmartSearchRequest, user: dict = Depends(get_current_user)):
    """
    **Smart Search** - Search messages with optional fuzzy contact filtering.

    Designed for queries like:
    - "when was my date with Masoud Nayebi?"
    - "what did Mom say about dinner?"
    - "meeting notes from last week"

    The contact field is fuzzy-matched, so "Masoud" will match "Masoud Nayebi-Tech Assistant".
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    # Resolve contact name to actual identifier if provided
    contact_filter = None
    resolved_contact = None

    if request.contact:
        contacts = await wa_client.get_contacts()
        match = resolve_single_contact(request.contact, contacts, threshold=0.4)
        if match:
            resolved_contact = {
                "name": match.get("name"),
                "phone": match.get("phone"),
                "jid": match.get("jid"),
                "match_score": match.get("match_score"),
            }
            # Use the JID or name for filtering
            contact_filter = match.get("jid") or match.get("name")

    # Search messages
    results = await wa_client.search_messages(request.query, contact_filter)

    return {
        "query": request.query,
        "contact_filter": request.contact,
        "resolved_contact": resolved_contact,
        "results": results[:request.limit],
        "total_matches": len(results),
    }


@router.post("/resolve")
async def resolve_contact_name(request: ContactResolveRequest, user: dict = Depends(get_current_user)):
    """
    **Resolve Contact** - Find the best matching contact for a given name.

    Returns ranked matches with confidence scores. Use this when you need to
    confirm which contact the user means before sending a message.

    **Example:**
    - Input: "Masoud Nayebi"
    - Output: [{"name": "Masoud Nayebi-Tech Assistant", "phone": "989...", "match_score": 0.92}]
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    contacts = await wa_client.get_contacts()
    matches = resolve_contact(request.name, contacts, threshold=0.3, max_results=5)

    return {
        "query": request.name,
        "matches": matches,
        "best_match": matches[0] if matches else None,
        "total_matches": len(matches),
    }


@router.get("/recent")
async def smart_recent(
    count: int = Query(10, description="Number of recent messages"),
    user: dict = Depends(get_current_user),
):
    """
    **Recent Messages** - Get the most recent incoming messages.

    Quick way for Manus to check "what's new on WhatsApp?"
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    messages = [
        msg for msg in wa_client._message_store
        if not msg.get("is_from_me", False)
    ]
    recent = messages[-count:]
    recent.reverse()  # Most recent first

    return {
        "messages": recent,
        "count": len(recent),
    }


@router.post("/reply-to")
async def smart_reply_to(
    contact: str = Query(..., description="Contact name (fuzzy matched)"),
    message: str = Query(..., description="Reply message"),
    as_assistant: bool = Query(False, description="Send as iDeep AI Assistant"),
    user: dict = Depends(get_current_user),
):
    """
    **Smart Reply** - Reply to a contact by name.

    Convenience endpoint for quick replies. Same as /smart/send but via query params.
    """
    request = SmartSendRequest(to=contact, message=message, as_assistant=as_assistant)
    return await smart_send_message(request, user)


# ─── Internal Helpers ────────────────────────────────────────────────────────


async def _resolve_recipient(identifier: str) -> Optional[dict]:
    """
    Resolve a recipient identifier to a usable contact dict with phone number.
    Handles both phone numbers and fuzzy name matching.

    Resolution order:
      1. Phone numbers — used as-is.
      2. Allow-listed contacts (name / relation / tags / llm_friendly_names).
         Hitting here gives the LLM direct semantic access without needing a
         live WhatsApp contact-list lookup.
      3. WhatsApp contact list (fuzzy).
      4. Recently-seen senders from the message store.
    """
    import re

    # Check if it looks like a phone number (mostly digits)
    digits_only = re.sub(r'\D', '', identifier)
    if len(digits_only) >= 8:
        # It's likely a phone number
        return {
            "name": identifier,
            "phone": digits_only,
            "jid": f"{digits_only}@s.whatsapp.net",
            "match_score": 1.0,
        }

    # Try allow-list aliases first — these are the curated names the user
    # taught the assistant ("Mom", "masoud", "مسعود", "ideep CTO", ...).
    allow_match = _match_allow_list(
        identifier, await allowed_contacts_service.list_contacts()
    )
    if allow_match:
        return allow_match

    # It's a name - do fuzzy resolution
    contacts = await wa_client.get_contacts()
    match = resolve_single_contact(identifier, contacts, threshold=0.4)

    if match:
        # Extract phone from JID if not directly available
        phone = match.get("phone")
        if not phone and match.get("jid"):
            jid = match["jid"]
            phone = jid.split("@")[0] if "@" in jid else jid
        match["phone"] = phone
        return match

    # Also try searching stored messages for the name
    # (contact might have messaged but not be in contacts list)
    for msg in wa_client._message_store:
        sender_name = msg.get("sender_name", "")
        if sender_name and identifier.lower() in sender_name.lower():
            from_jid = msg.get("from", "") or msg.get("chat_jid", "")
            # from_jid may be a `<opaque>@lid` privacy address rather than a
            # phone number — prefer the resolved real phone when we have it,
            # so sending doesn't end up targeting the LID digits.
            resolved_phone = msg.get("from_phone")
            phone = resolved_phone or (from_jid.split("@")[0] if "@" in from_jid else from_jid)
            return {
                "name": sender_name,
                "phone": phone,
                "jid": from_jid,
                "match_score": 0.7,
            }

    return None


def _match_allow_list(identifier: str, entries: list) -> Optional[dict]:
    """Match a query against the allow-list's curated aliases/tags/relation."""
    if not identifier or not entries:
        return None

    q = identifier.strip().lower()
    if not q:
        return None

    def haystack(entry: dict) -> list[str]:
        parts = [
            entry.get("name") or "",
            entry.get("relation") or "",
            entry.get("notes") or "",
        ]
        parts.extend(entry.get("llm_friendly_names") or [])
        parts.extend(entry.get("tags") or [])
        for v in (entry.get("attributes") or {}).values():
            if isinstance(v, str):
                parts.append(v)
        return [p.lower() for p in parts if p]

    # Exact alias match (highest confidence)
    for entry in entries:
        for needle in haystack(entry):
            if needle == q:
                return _as_resolved(entry, score=1.0)

    # Substring containment in either direction
    for entry in entries:
        for needle in haystack(entry):
            if q in needle or needle in q:
                return _as_resolved(entry, score=0.85)

    return None


def _as_resolved(entry: dict, score: float) -> dict:
    phone = entry.get("phone") or ""
    return {
        "name": entry.get("name") or phone,
        "phone": phone,
        "jid": f"{phone}@s.whatsapp.net" if phone else None,
        "match_score": score,
        "from_allow_list": True,
        "allow_list_id": entry.get("id"),
    }
