"""
Pydantic models for request/response validation.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─── Enums ───────────────────────────────────────────────────────────────────


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    QR_READY = "qr_ready"
    PAIR_CODE_READY = "pair_code_ready"
    CONNECTED = "connected"
    LOGGED_OUT = "logged_out"


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"


# ─── Auth Models ─────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    """Login request for web UI."""
    password: str = Field(..., description="Admin password for web UI access")


class LoginResponse(BaseModel):
    """Login response with JWT token."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token expiration in seconds")


class TokenInfo(BaseModel):
    """Token information."""
    valid: bool
    user: Optional[str] = None
    auth_type: Optional[str] = None
    expires_at: Optional[str] = None


# ─── Connection Models ───────────────────────────────────────────────────────


class ConnectionStatus(BaseModel):
    """WhatsApp connection status."""
    state: ConnectionState
    is_connected: bool
    connected_at: Optional[str] = None
    device_info: Optional[Dict[str, Any]] = None
    stored_messages_count: int = 0
    auto_reply_enabled: bool = False
    contacts_cached: int = 0
    last_sent_at: Optional[float] = None
    last_receipt_at: Optional[float] = None
    sends_without_receipt: int = 0


class QRCodeResponse(BaseModel):
    """QR code data for WhatsApp login."""
    state: ConnectionState
    qr_data: Optional[str] = None
    qr_base64: Optional[str] = None
    message: str = ""


class PairCodeRequest(BaseModel):
    """Request pair code for phone number linking."""
    phone_number: str = Field(..., description="Phone number with country code (e.g., +989123456789)")


class PairCodeResponse(BaseModel):
    """Pair code response."""
    success: bool
    pair_code: Optional[str] = None
    phone_number: str
    message: str = ""


# ─── Message Models ──────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    """Request to send a message."""
    phone: str = Field(..., description="Phone number (with country code, no +)")
    message: str = Field(..., description="Message text to send")
    reply_to: Optional[str] = Field(None, description="Message ID to reply to")


class SendMessageResponse(BaseModel):
    """Response after sending a message."""
    success: bool
    to: str
    message: str
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    error: Optional[str] = None


class BulkMessageRequest(BaseModel):
    """Request to send messages to multiple recipients."""
    phones: List[str] = Field(..., description="List of phone numbers")
    message: str = Field(..., description="Message text to send")
    delay_seconds: float = Field(2.0, description="Delay between messages (anti-spam)")


class BulkMessageResponse(BaseModel):
    """Response for bulk message sending."""
    total: int
    sent: int
    failed: int
    results: List[SendMessageResponse]


class MessageRecord(BaseModel):
    """A stored message record."""
    id: str
    from_jid: Optional[str] = Field(None, alias="from")
    chat_jid: Optional[str] = None
    sender_name: Optional[str] = None
    text: Optional[str] = None
    timestamp: Optional[str] = None
    is_group: bool = False
    is_from_me: bool = False
    type: str = "text"

    class Config:
        populate_by_name = True


class ChatInfo(BaseModel):
    """Chat/conversation information."""
    chat_jid: str
    name: Optional[str] = None
    last_message: Optional[str] = None
    last_timestamp: Optional[str] = None
    unread_count: int = 0
    messages: List[Dict[str, Any]] = []


class SearchRequest(BaseModel):
    """Search messages request."""
    query: str = Field(..., description="Search query text")
    contact: Optional[str] = Field(None, description="Filter by contact name or phone")
    limit: int = Field(50, description="Maximum results to return")


# ─── Contact Models ──────────────────────────────────────────────────────────


class ContactInfo(BaseModel):
    """Contact information."""
    jid: str
    name: Optional[str] = None
    phone: Optional[str] = None
    profile_picture_url: Optional[str] = None


class PhoneCheckRequest(BaseModel):
    """Request to check if phones are on WhatsApp."""
    phones: List[str] = Field(..., description="List of phone numbers to check")


class PhoneCheckResult(BaseModel):
    """Result of phone number check."""
    phone: str
    is_registered: bool
    jid: Optional[str] = None


# ─── Group Models ────────────────────────────────────────────────────────────


class GroupInfo(BaseModel):
    """Group information."""
    jid: str
    name: Optional[str] = None
    participant_count: int = 0


# ─── Auto-Reply Models ───────────────────────────────────────────────────────


class AutoReplyRule(BaseModel):
    """A single auto-reply rule."""
    contact: Optional[str] = Field(None, description="Contact JID or phone to match")
    keyword: Optional[str] = Field(None, description="Keyword trigger in message")
    message: str = Field(..., description="Reply message to send")
    enabled: bool = True


class AutoReplyConfig(BaseModel):
    """Auto-reply configuration."""
    enabled: bool = Field(..., description="Enable/disable auto-reply globally")
    message: Optional[str] = Field(None, description="Default auto-reply message")
    assistant_name: Optional[str] = Field(None, description="Assistant name in replies")
    rules: Optional[List[AutoReplyRule]] = Field(None, description="Custom reply rules")


class AutoReplyStatus(BaseModel):
    """Current auto-reply status."""
    enabled: bool
    message: str
    assistant_name: str
    rules: List[Dict[str, Any]] = []


# ─── Webhook Models ──────────────────────────────────────────────────────────


class WebhookRegisterRequest(BaseModel):
    """Register a new webhook."""
    url: str = Field(..., description="Webhook URL to receive events")
    events: List[str] = Field(
        default=["message", "connection", "status"],
        description="Events to subscribe to",
    )
    secret: Optional[str] = Field(None, description="Secret for HMAC signature verification")
    name: str = Field("custom", description="Webhook name for identification")


class WebhookInfo(BaseModel):
    """Webhook information."""
    id: int
    name: str
    url: str
    events: List[str]
    active: bool
    created_at: Optional[str] = None
    last_triggered: Optional[str] = None
    failure_count: int = 0


# ─── Health & System Models ──────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    version: str
    uptime: Optional[str] = None
    whatsapp_connected: bool = False
    timestamp: str


class APIInfoResponse(BaseModel):
    """API information response."""
    name: str
    version: str
    description: str
    docs_url: str
    endpoints: Dict[str, str]
