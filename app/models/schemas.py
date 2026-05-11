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
    """Response after sending a message.

    `message` is Optional because failure paths in `wa_client.send_message`
    (session-stale, invalid phone, not on WhatsApp, etc.) return a dict
    without the message body — only success/error/to. Treating it as required
    masked the real error behind a 500 ValidationError.
    """
    success: bool
    to: str
    message: Optional[str] = None
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
    match_mode: str = Field("contains", description="contains | exact | starts_with | regex")
    message: str = Field("", description="Reply message (leave empty to use LLM only)")
    use_llm: bool = Field(False, description="Generate reply via LLM instead of static text")
    cooldown_seconds: int = Field(0, description="Minimum seconds between firings per contact")
    enabled: bool = True
    priority: int = Field(100, description="Lower runs first")


class AutoReplyRuleUpdate(BaseModel):
    """Partial update payload for a rule."""
    contact: Optional[str] = None
    keyword: Optional[str] = None
    match_mode: Optional[str] = None
    message: Optional[str] = None
    use_llm: Optional[bool] = None
    cooldown_seconds: Optional[int] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class QuietHoursConfig(BaseModel):
    """Quiet-hours window."""
    enabled: bool = False
    start: str = Field("22:00", description="HH:MM (24h)")
    end: str = Field("08:00", description="HH:MM (24h); window may cross midnight")
    timezone: str = Field("UTC", description="IANA timezone, e.g. Europe/Istanbul")
    message: str = Field("", description="Optional away reply during quiet hours; blank = silent")
    defer_scheduled: bool = Field(True, description="Hold scheduled sends until window closes")


class AutoReplyConfig(BaseModel):
    """Auto-reply configuration update payload."""
    enabled: Optional[bool] = None
    message: Optional[str] = Field(None, description="Default auto-reply message")
    assistant_name: Optional[str] = None
    llm_enabled: Optional[bool] = None
    llm_system_prompt: Optional[str] = None
    quiet_hours: Optional[QuietHoursConfig] = None


class AutoReplyStatus(BaseModel):
    """Current auto-reply status snapshot."""
    enabled: bool
    message: str
    assistant_name: str
    llm_enabled: bool = False
    llm_system_prompt: str = ""
    quiet_hours: Dict[str, Any] = {}
    rules: List[Dict[str, Any]] = []


class LLMInfo(BaseModel):
    """Read-only view of the configured LLM provider."""
    provider: str
    model: str
    configured: bool
    has_api_key: bool
    base_url: str = ""


# ─── Contact persona ─────────────────────────────────────────────────────────


class ContactPersona(BaseModel):
    """Per-contact context fed into the LLM system prompt."""
    contact: str = Field(..., description="JID or phone digits")
    display_name: Optional[str] = None
    notes: Optional[str] = Field(None, description="Free-form context about this contact")
    system_prompt_override: Optional[str] = Field(None, description="Full override of system prompt")
    use_llm: bool = True


# ─── Scheduled sends ─────────────────────────────────────────────────────────


class ScheduledSendCreate(BaseModel):
    """Schedule a message for future delivery."""
    phone: str = Field(..., description="Phone with country code, no '+'")
    message: str
    scheduled_at: str = Field(..., description="ISO 8601 datetime; treated as UTC if no offset")


class ScheduledSendInfo(BaseModel):
    """A scheduled send record."""
    id: int
    phone: str
    message: str
    scheduled_at: str
    status: str
    sent_at: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None


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


# ─── Allowed Contacts (Send Permissions) ─────────────────────────────────────


class AllowedContactBase(BaseModel):
    """Shared fields for an allow-listed contact the assistant may message."""
    name: str = Field(..., description="Canonical name shown in the panel")
    phone: str = Field(..., description="Phone number with country code, no '+'")
    relation: Optional[str] = Field(
        None,
        description="Relationship to the user (e.g. 'daughter', 'CTO at iDeep')",
    )
    llm_friendly_names: List[str] = Field(
        default_factory=list,
        description=(
            "Aliases the LLM should recognize for this contact. "
            "Examples: ['masoud', 'مسعود', 'ideep company CTO']"
        ),
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Free-form tags (e.g. ['family', 'work', 'urgent-ok'])",
    )
    notes: Optional[str] = Field(
        None,
        description="Free-form notes for the LLM (tone, language, do/don't, etc.)",
    )
    attributes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extra structured attributes (role, company, language, ...)",
    )
    enabled: bool = Field(
        True,
        description="If false, this contact is in the list but currently blocked",
    )


class AllowedContactCreate(AllowedContactBase):
    """Payload to create a new allowed contact."""
    pass


class AllowedContactUpdate(BaseModel):
    """Partial update payload — all fields optional."""
    name: Optional[str] = None
    phone: Optional[str] = None
    relation: Optional[str] = None
    llm_friendly_names: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class AllowedContact(AllowedContactBase):
    """A stored allowed contact (includes server-managed fields)."""
    id: str
    created_at: str
    updated_at: str


class PermissionsConfig(BaseModel):
    """Global toggle + the full allow-list."""
    enabled: bool = Field(
        ...,
        description=(
            "Master switch. When true, sends are restricted to allow-listed "
            "contacts. When false, all sends pass through (no restriction)."
        ),
    )
    contacts: List[AllowedContact] = Field(default_factory=list)


class PermissionsToggle(BaseModel):
    """Body for toggling the master switch."""
    enabled: bool


class PermissionsCheckResponse(BaseModel):
    """Result of an allow-list check for a phone number."""
    phone: str
    allowed: bool
    enforced: bool = Field(
        ...,
        description="Whether the master switch is on (i.e. checks are enforced)",
    )
    contact: Optional[AllowedContact] = None
    reason: Optional[str] = None
