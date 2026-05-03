"""
Authentication module for iDeep WhatsApp Bot API.
Supports API Key authentication (for Manus/external integrations)
and JWT tokens (for web UI sessions).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

logger = logging.getLogger(__name__)

# Security schemes
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def create_jwt_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT token for web UI authentication."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=settings.JWT_EXPIRATION_HOURS))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_jwt_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid JWT token: {e}")
        return None


def verify_api_key(api_key: str) -> bool:
    """Verify the API key."""
    return api_key == settings.API_KEY


async def get_current_user(
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
) -> dict:
    """
    Authenticate the request using either API Key or JWT Bearer token.
    Priority: API Key > Bearer Token
    """
    # Try API Key first
    if api_key:
        if verify_api_key(api_key):
            return {"auth_type": "api_key", "user": "manus_integration"}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Try Bearer token
    if bearer:
        payload = verify_jwt_token(bearer.credentials)
        if payload:
            return {"auth_type": "jwt", "user": payload.get("sub", "web_user"), **payload}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    # No authentication provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide API key via X-API-Key header or Bearer token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
) -> Optional[dict]:
    """Optional authentication - returns None if not authenticated."""
    try:
        return await get_current_user(api_key, bearer)
    except HTTPException:
        return None
