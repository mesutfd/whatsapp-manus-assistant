"""
Configuration management for iDeep WhatsApp Bot API.
Loads settings from environment variables with sensible defaults.
"""

import os
import secrets
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "iDeep WhatsApp Bot API"
    APP_VERSION: str = "1.2.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Security
    API_KEY: str = os.getenv("API_KEY", secrets.token_urlsafe(32))
    API_KEY_HEADER: str = "X-API-Key"
    JWT_SECRET: str = os.getenv("JWT_SECRET", secrets.token_urlsafe(64))
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 720  # 30 days

    # WhatsApp
    WA_SESSION_NAME: str = "ideep_whatsapp"
    WA_DATABASE_PATH: str = "/app/data/whatsapp.db"
    WA_STORE_PATH: str = "/app/data"

    # App-level persistence (rules, scheduled sends, personas, quiet hours)
    APP_DB_PATH: str = "/app/data/app.db"

    # Auto-Reply (defaults seeded into DB on first run; live values come from DB)
    AUTO_REPLY_ENABLED: bool = False
    AUTO_REPLY_MESSAGE: str = (
        "Hi, I am iDeep AI Assistant. "
        "Alireza is not available right now, but he would be available soon and will get back to you."
    )
    ASSISTANT_NAME: str = "iDeep AI"

    # LLM (provider-agnostic; chosen via LLM_PROVIDER)
    LLM_PROVIDER: str = "none"  # one of: none | openai | anthropic
    LLM_MODEL: str = "gpt-4o-mini"  # default for openai; for anthropic use e.g. claude-haiku-4-5
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: Optional[str] = None  # optional override (e.g. azure/openrouter proxy)
    LLM_SYSTEM_PROMPT: str = (
        "You are an AI assistant on WhatsApp, replying automatically on behalf of the owner of "
        "this account, who is currently unavailable. Every user message you receive is a real "
        "incoming WhatsApp message from someone trying to reach the owner — your job is NOT to "
        "ask the owner what to do, it is to reply to the sender directly, in a warm, courteous, "
        "concise tone, as a polite stand-in. Match the language of the incoming message. "
        "If you don't know a personal fact about the owner, say you'll pass the message along. "
        "Only mention you are an AI assistant if the sender asks who you are.\n\n"
        "شما یک دستیار هوشمند روی واتس‌اپ هستید که به جای صاحب این حساب — که الان در دسترس "
        "نیست — به‌طور خودکار پاسخ می‌دهید. هر پیامی که می‌بینید، پیامی واقعی از کسی است که "
        "می‌خواهد با صاحب حساب در ارتباط باشد؛ پس وظیفه‌تان این است که مستقیم به فرستنده "
        "پاسخ دهید، نه این‌که از صاحب حساب سؤال کنید چه بگویید. لحن گرم، مؤدب و کوتاه نگه "
        "دارید و به همان زبانی که پیام آمده پاسخ بدهید. اگر چیزی شخصی درباره‌ی صاحب حساب "
        "نمی‌دانید، بگویید پیام را به ایشان می‌رسانید. فقط در صورت پرسش مستقیم بگویید که "
        "دستیار هوشمند صاحب حساب هستید."
    )
    LLM_MAX_TOKENS: int = 300
    LLM_TEMPERATURE: float = 0.7
    LLM_HISTORY_SIZE: int = 8  # how many recent messages from this chat to include
    LLM_TIMEOUT_SECONDS: float = 20.0

    # Scheduler
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_POLL_SECONDS: int = 20

    # Quiet hours (defaults seeded into DB; live values come from DB)
    QUIET_HOURS_TIMEZONE: str = "UTC"

    # Message Store
    MESSAGE_STORE_ENABLED: bool = True
    MESSAGE_STORE_DB: str = "/app/data/messages.db"
    MAX_STORED_MESSAGES: int = 10000

    # Webhooks
    WEBHOOK_URL: Optional[str] = None
    WEBHOOK_SECRET: Optional[str] = None
    WEBHOOK_EVENTS: str = "message,status,connection"

    # Rate Limiting
    RATE_LIMIT_MESSAGES_PER_MINUTE: int = 30
    RATE_LIMIT_ENABLED: bool = True

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "/app/data/logs/app.log"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()
