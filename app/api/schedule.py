"""
Scheduled-send API. Persisted in SQLite and dispatched by app.core.scheduler.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.database import db
from app.models.schemas import ScheduledSendCreate, ScheduledSendInfo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/schedule", tags=["Scheduled Sends"])


def _to_utc_iso(value: str) -> str:
    """Accept a flexible ISO-ish string and return canonical UTC ISO."""
    s = value.strip()
    if not s:
        raise ValueError("empty datetime")
    # Allow trailing 'Z' as UTC
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Treat naive as UTC (matches the .env-default timezone)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/")
async def list_scheduled(
    status: Optional[str] = Query(None, description="pending | sent | failed | cancelled"),
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    items = await db.list_scheduled(status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.post("/", response_model=ScheduledSendInfo)
async def create_scheduled(
    payload: ScheduledSendCreate,
    user: dict = Depends(get_current_user),
):
    try:
        when_iso = _to_utc_iso(payload.scheduled_at)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Bad scheduled_at: {e}")

    if not payload.phone.strip() or not payload.message.strip():
        raise HTTPException(status_code=400, detail="phone and message are required")

    row = await db.add_scheduled(
        phone=payload.phone.strip(),
        message=payload.message,
        scheduled_at_iso=when_iso,
    )
    return ScheduledSendInfo(**row)


@router.delete("/{send_id}")
async def cancel_scheduled(send_id: int, user: dict = Depends(get_current_user)):
    ok = await db.cancel_scheduled(send_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pending send not found")
    return {"success": True, "id": send_id}
