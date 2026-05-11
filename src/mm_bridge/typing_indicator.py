"""Per-session typing-indicator refresh loops."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TypingIndicator:
    """Publish 'bot is typing' to Mattermost every `refresh_s` while a session
    is active. Caller drives start/stop from backend lifecycle/activity events;
    a watchdog in the bridge can call `stop` if the event stream goes silent.
    """

    def __init__(self, mm_client, refresh_s: float):
        self._mm = mm_client
        self._refresh_s = refresh_s
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(
        self, session_id: str, channel_id: str, parent_id: str | None = None
    ) -> None:
        await self.stop(session_id)
        task = asyncio.create_task(self._loop(channel_id, parent_id))
        self._tasks[session_id] = task

    async def stop(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        if not task:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        results = await asyncio.gather(
            *self._tasks.values(), return_exceptions=True
        )
        self._tasks.clear()
        del results

    def running_sessions(self) -> list[str]:
        return [sid for sid, t in self._tasks.items() if not t.done()]

    async def _loop(self, channel_id: str, parent_id: str | None) -> None:
        try:
            while True:
                try:
                    self._mm.publish_user_typing(channel_id, parent_id)
                except Exception:
                    logger.debug("publish_user_typing failed", exc_info=True)
                await asyncio.sleep(self._refresh_s)
        except asyncio.CancelledError:
            pass
