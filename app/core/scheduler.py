"""
Scheduled-send service. Polls the SQLite scheduled_sends table on an interval
and dispatches due messages through the WhatsApp client.

Quiet hours: when active and `quiet_hours_defer_scheduled` is set, due sends
remain pending until the window closes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.database import db
from app.core.quiet_hours import is_quiet_now

logger = logging.getLogger(__name__)


class ScheduledSendService:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False

    async def start(self) -> None:
        if not settings.SCHEDULER_ENABLED:
            logger.info("Scheduler disabled by SCHEDULER_ENABLED=false")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler-loop")
        logger.info("Scheduler started (poll every %ds)", settings.SCHEDULER_POLL_SECONDS)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # never let the loop die
                logger.error("Scheduler tick failed: %s", e)
            await asyncio.sleep(settings.SCHEDULER_POLL_SECONDS)

    async def _tick(self) -> None:
        # Imported lazily to avoid a circular import at module load.
        from app.core.whatsapp_client import wa_client

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        due = await db.claim_due_scheduled(now_iso)
        if not due:
            return

        config = await db.get_config()
        if is_quiet_now(config) and config.get("quiet_hours_defer_scheduled"):
            logger.debug("Quiet hours active — deferring %d scheduled send(s)", len(due))
            return

        if not wa_client.is_connected:
            logger.debug("WhatsApp not connected — leaving %d due send(s) pending", len(due))
            return

        for send in due:
            try:
                result = await wa_client.send_message(send["phone"], send["message"])
                if result.get("success"):
                    await db.mark_scheduled(
                        send["id"],
                        status="sent",
                        sent_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    logger.info("Sent scheduled message #%d to %s", send["id"], send["phone"])
                else:
                    err = result.get("error") or "send returned success=false"
                    await db.mark_scheduled(send["id"], status="failed", error=str(err))
                    logger.warning("Scheduled send #%d failed: %s", send["id"], err)
            except Exception as e:
                await db.mark_scheduled(send["id"], status="failed", error=str(e))
                logger.error("Scheduled send #%d crashed: %s", send["id"], e)


scheduler = ScheduledSendService()
