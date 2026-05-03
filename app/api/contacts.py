"""
Contacts & Groups API endpoints.
Handles contact retrieval, profile lookup, phone verification, and group management.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.whatsapp_client import wa_client
from app.models.schemas import (
    ContactInfo,
    GroupInfo,
    PhoneCheckRequest,
    PhoneCheckResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/contacts", tags=["Contacts & Groups"])


@router.get("/", response_model=None)
async def get_contacts(
    limit: int = Query(100, description="Maximum contacts to return"),
    user: dict = Depends(get_current_user),
):
    """
    Get all WhatsApp contacts.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    contacts = await wa_client.get_contacts()
    return {"contacts": contacts[:limit], "total": len(contacts)}


@router.get("/search")
async def search_contacts(
    query: str = Query(..., description="Search query (name or phone)"),
    user: dict = Depends(get_current_user),
):
    """
    Search contacts by name or phone number.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    contacts = await wa_client.get_contacts()
    query_lower = query.lower()
    results = [
        c for c in contacts
        if query_lower in (c.get("name", "") or "").lower()
        or query_lower in (c.get("phone", "") or "")
        or query_lower in (c.get("jid", "") or "")
    ]
    return {"query": query, "results": results, "count": len(results)}


@router.get("/profile/{phone}")
async def get_contact_profile(phone: str, user: dict = Depends(get_current_user)):
    """
    Get profile information for a specific contact.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    profile = await wa_client.get_profile(phone)
    return profile


@router.post("/check", response_model=None)
async def check_phones_registered(
    request: PhoneCheckRequest,
    user: dict = Depends(get_current_user),
):
    """
    Check if phone numbers are registered on WhatsApp.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    results = await wa_client.check_phone_registered(request.phones)
    return {"results": results, "total_checked": len(request.phones)}


@router.get("/groups")
async def get_groups(user: dict = Depends(get_current_user)):
    """
    Get all WhatsApp groups the user is a member of.
    """
    if not wa_client.is_connected:
        raise HTTPException(status_code=503, detail="WhatsApp is not connected")

    groups = await wa_client.get_groups()
    return {"groups": groups, "total": len(groups)}
