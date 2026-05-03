"""
Webhook service for pushing events to external systems (Manus, n8n, etc.)
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class WebhookService:
    """Manages outgoing webhooks to external services."""

    def __init__(self):
        self._webhooks: List[Dict[str, Any]] = []
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._is_running: bool = False
        self._task: Optional[asyncio.Task] = None

        # Register default webhook from settings
        if settings.WEBHOOK_URL:
            self.register_webhook(
                url=settings.WEBHOOK_URL,
                events=settings.WEBHOOK_EVENTS.split(","),
                secret=settings.WEBHOOK_SECRET,
                name="default",
            )

    def register_webhook(
        self,
        url: str,
        events: List[str],
        secret: Optional[str] = None,
        name: str = "custom",
    ) -> Dict[str, Any]:
        """Register a new webhook endpoint."""
        webhook = {
            "id": len(self._webhooks) + 1,
            "name": name,
            "url": url,
            "events": events,
            "secret": secret,
            "active": True,
            "created_at": datetime.utcnow().isoformat(),
            "last_triggered": None,
            "failure_count": 0,
        }
        self._webhooks.append(webhook)
        logger.info(f"Webhook registered: {name} -> {url} for events: {events}")
        return webhook

    def remove_webhook(self, webhook_id: int) -> bool:
        """Remove a webhook by ID."""
        self._webhooks = [w for w in self._webhooks if w["id"] != webhook_id]
        return True

    def list_webhooks(self) -> List[Dict[str, Any]]:
        """List all registered webhooks."""
        return self._webhooks

    def _sign_payload(self, payload: str, secret: str) -> str:
        """Generate HMAC signature for webhook payload."""
        return hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def emit(self, event_type: str, data: Dict[str, Any]):
        """Queue an event for webhook delivery."""
        await self._event_queue.put({"event": event_type, "data": data, "timestamp": datetime.utcnow().isoformat()})

    async def start(self):
        """Start the webhook delivery worker."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._delivery_worker())
        logger.info("Webhook delivery service started")

    async def stop(self):
        """Stop the webhook delivery worker."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Webhook delivery service stopped")

    async def _delivery_worker(self):
        """Background worker that delivers webhook events."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while self._is_running:
                try:
                    event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                    await self._deliver_event(client, event)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Webhook delivery error: {e}")

    async def _deliver_event(self, client: httpx.AsyncClient, event: Dict[str, Any]):
        """Deliver a single event to all matching webhooks."""
        event_type = event["event"]

        for webhook in self._webhooks:
            if not webhook["active"]:
                continue
            if event_type not in webhook["events"] and "*" not in webhook["events"]:
                continue

            try:
                payload = json.dumps(event)
                headers = {
                    "Content-Type": "application/json",
                    "X-Webhook-Event": event_type,
                    "X-Webhook-Timestamp": event["timestamp"],
                }

                if webhook.get("secret"):
                    signature = hmac.new(
                        webhook["secret"].encode(),
                        payload.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    headers["X-Webhook-Signature"] = f"sha256={signature}"

                response = await client.post(
                    webhook["url"],
                    content=payload,
                    headers=headers,
                )

                webhook["last_triggered"] = event["timestamp"]
                if response.status_code >= 400:
                    webhook["failure_count"] += 1
                    logger.warning(
                        f"Webhook {webhook['name']} returned {response.status_code}"
                    )
                else:
                    webhook["failure_count"] = 0

            except Exception as e:
                webhook["failure_count"] += 1
                logger.error(f"Webhook delivery failed for {webhook['name']}: {e}")

                # Disable after 10 consecutive failures
                if webhook["failure_count"] >= 10:
                    webhook["active"] = False
                    logger.warning(f"Webhook {webhook['name']} disabled after 10 failures")


# Singleton instance
webhook_service = WebhookService()
