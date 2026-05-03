"""
LLM client with provider abstraction. Talks to OpenAI or Anthropic over httpx
without pulling in either SDK.

Configured by .env:
    LLM_PROVIDER     openai | anthropic | none
    LLM_MODEL        provider-specific model id
    LLM_API_KEY      provider API key
    LLM_BASE_URL     optional override (proxy / azure / openrouter)
    LLM_SYSTEM_PROMPT
    LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_HISTORY_SIZE, LLM_TIMEOUT_SECONDS
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    """Stateless wrapper. One instance per process is fine."""

    def __init__(self) -> None:
        self.provider = (settings.LLM_PROVIDER or "none").lower().strip()
        self.model = settings.LLM_MODEL
        self.api_key = settings.LLM_API_KEY
        self.base_url = settings.LLM_BASE_URL or self._default_base_url()
        self.timeout = settings.LLM_TIMEOUT_SECONDS

    def _default_base_url(self) -> str:
        if self.provider == "openai":
            return "https://api.openai.com/v1"
        if self.provider == "anthropic":
            return "https://api.anthropic.com/v1"
        return ""

    @property
    def is_configured(self) -> bool:
        return self.provider in {"openai", "anthropic"} and bool(self.api_key)

    def info(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": self.is_configured,
            "has_api_key": bool(self.api_key),
            "base_url": self.base_url,
        }

    async def generate_reply(
        self,
        system_prompt: str,
        history: List[Dict[str, str]],
        user_message: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Generate an assistant reply.

        history: list of {"role": "user"|"assistant", "content": str}, oldest first.
        user_message: the latest incoming message to reply to.
        """
        if not self.is_configured:
            raise LLMError(f"LLM provider '{self.provider}' is not configured")

        max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        temperature = temperature if temperature is not None else settings.LLM_TEMPERATURE

        if self.provider == "openai":
            return await self._call_openai(system_prompt, history, user_message, max_tokens, temperature)
        if self.provider == "anthropic":
            return await self._call_anthropic(system_prompt, history, user_message, max_tokens, temperature)
        raise LLMError(f"Unknown provider: {self.provider}")

    async def _call_openai(
        self,
        system_prompt: str,
        history: List[Dict[str, str]],
        user_message: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for m in history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(f"OpenAI {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("OpenAI returned no choices")
            text = (choices[0].get("message") or {}).get("content") or ""
            return text.strip()
        except httpx.HTTPError as e:
            raise LLMError(f"OpenAI HTTP error: {e}") from e

    async def _call_anthropic(
        self,
        system_prompt: str,
        history: List[Dict[str, str]],
        user_message: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        url = f"{self.base_url.rstrip('/')}/messages"
        messages: List[Dict[str, str]] = []
        for m in history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(f"Anthropic {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            blocks = data.get("content") or []
            text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            text = "".join(text_parts).strip()
            if not text:
                raise LLMError("Anthropic returned no text content")
            return text
        except httpx.HTTPError as e:
            raise LLMError(f"Anthropic HTTP error: {e}") from e


llm_client = LLMClient()
