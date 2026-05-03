"""
Configuration management for iDeep WhatsApp Bot API.
Loads settings from environment variables with sensible defaults.
"""

import os
import secrets
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "iDeep WhatsApp Bot API"
    APP_VERSION: str = "1.0.0"
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

    # Auto-Reply
    AUTO_REPLY_ENABLED: bool = False
    AUTO_REPLY_MESSAGE: str = (
        "Hi, I am iDeep AI Assistant. "
        "Alireza is not available right now, but he would be available soon and will get back to you."
    )
    ASSISTANT_NAME: str = "iDeep AI"

    # Message Store
    MESSAGE_STORE_ENABLED: bool = True
    MESSAGE_STORE_DB: str = "/app/data/messages.db"
    MAX_STORED_MESSAGES: int = 10000

    # Webhooks (for external integrations like n8n/Manus)
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
