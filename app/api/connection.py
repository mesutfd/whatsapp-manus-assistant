"""
Connection & Authentication API endpoints.
Handles WhatsApp login flow (QR code, pair code), connection status, and auth tokens.
"""

import asyncio
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import create_jwt_token, get_current_user
from app.core.config import settings
from app.core.whatsapp_client import wa_client
from app.models.schemas import (
    ConnectionStatus,
    LoginRequest,
    LoginResponse,
    PairCodeRequest,
    PairCodeResponse,
    QRCodeResponse,
    TokenInfo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/connection", tags=["Connection & Auth"])


@router.get("/status", response_model=ConnectionStatus)
async def get_connection_status(user: dict = Depends(get_current_user)):
    """Get current WhatsApp connection status."""
    return wa_client.get_status()


@router.get("/probe")
async def probe_session(user: dict = Depends(get_current_user)):
    """
    Authoritative session check. Asks whatsmeow whether the device is still
    logged in. Use this if you suspect a stale session — if logged_in=False
    while the cached state still says connected, this will flip the state to
    LOGGED_OUT and emit a session_expired event.
    """
    return await wa_client.probe_session()


@router.post("/connect")
async def connect_whatsapp(user: dict = Depends(get_current_user)):
    """
    Initiate WhatsApp connection.
    After calling this, poll /qr endpoint to get the QR code for scanning.
    """
    if wa_client.is_connected:
        return {"status": "already_connected", "message": "WhatsApp is already connected"}

    try:
        # Start connection in background
        asyncio.create_task(wa_client.connect())
        return {
            "status": "connecting",
            "message": "Connection initiated. Poll /api/v1/connection/qr for QR code.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {str(e)}")


@router.post("/disconnect")
async def disconnect_whatsapp(user: dict = Depends(get_current_user)):
    """Disconnect WhatsApp session."""
    await wa_client.disconnect()
    return {"status": "disconnected", "message": "WhatsApp disconnected successfully"}


@router.post("/logout")
async def logout_whatsapp(user: dict = Depends(get_current_user)):
    """
    Log out of WhatsApp completely and wipe the local session.
    The next call to /connect will require scanning a fresh QR code.
    Use this to switch to a different WhatsApp account.
    """
    info = await wa_client.logout()
    return {
        "status": "logged_out",
        "message": "Session wiped. Call /connect to start a fresh login.",
        "wiped": info.get("wiped", []),
    }


@router.get("/qr", response_model=QRCodeResponse)
async def get_qr_code(user: dict = Depends(get_current_user)):
    """
    Get the current QR code for WhatsApp Web login.
    Returns base64-encoded QR image and raw QR data.
    """
    state = wa_client.state

    if wa_client.is_connected:
        return QRCodeResponse(
            state=state,
            message=(
                "Already linked to WhatsApp - no QR code needed. "
                "Use /connection/logout to wipe the session and link a different account."
            ),
        )

    if wa_client.qr_base64:
        return QRCodeResponse(
            state=state,
            qr_data=wa_client.qr_data,
            qr_base64=wa_client.qr_base64,
            message="Scan this QR code with WhatsApp on your phone",
        )

    return QRCodeResponse(
        state=state,
        message="QR code not yet available. Call /connect first and wait a few seconds.",
    )


@router.post("/pair-code", response_model=PairCodeResponse)
async def get_pair_code(request: PairCodeRequest, user: dict = Depends(get_current_user)):
    """
    Get a pair code for linking via phone number.
    Alternative to QR code scanning - enter this code on your phone.
    """
    phone = request.phone_number.replace("+", "").replace(" ", "").replace("-", "")

    try:
        code = await wa_client.get_pair_code(phone)
        if code:
            return PairCodeResponse(
                success=True,
                pair_code=code,
                phone_number=phone,
                message=f"Enter this code on your phone: {code}",
            )
        return PairCodeResponse(
            success=False,
            phone_number=phone,
            message="Failed to generate pair code. Try again.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pair code generation failed: {str(e)}")


@router.post("/token", response_model=LoginResponse)
async def create_access_token(request: LoginRequest):
    """
    Create a JWT access token for web UI authentication.
    Uses the API_KEY as the password for simplicity.
    """
    if request.password != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = create_jwt_token(
        data={"sub": "admin", "role": "admin"},
        expires_delta=timedelta(hours=settings.JWT_EXPIRATION_HOURS),
    )

    return LoginResponse(
        access_token=token,
        expires_in=settings.JWT_EXPIRATION_HOURS * 3600,
    )


@router.get("/token/verify", response_model=TokenInfo)
async def verify_token(user: dict = Depends(get_current_user)):
    """Verify the current authentication token/API key."""
    return TokenInfo(
        valid=True,
        user=user.get("user"),
        auth_type=user.get("auth_type"),
    )
