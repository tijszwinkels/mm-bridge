"""VibeDeck HTTP + SSE client."""

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class VibeDeckClient:
    """Async client for VibeDeck's REST API and SSE event stream."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def create_session(
        self,
        message: str,
        cwd: str,
        backend: str | None = None,
        model_index: int | None = None,
        source_session_id: str | None = None,
    ) -> dict:
        """Create a new VibeDeck session."""
        payload = {"message": message, "cwd": cwd}
        if backend:
            payload["backend"] = backend
        if model_index is not None:
            payload["model_index"] = model_index
        if source_session_id:
            payload["source_session_id"] = source_session_id

        resp = await self._http.post(
            "/sessions/new",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict:
        """Send a message to an existing session."""
        resp = await self._http.post(
            f"/sessions/{session_id}/send",
            json={"message": message},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_sessions(self) -> list[dict]:
        """List all tracked sessions."""
        resp = await self._http.get("/sessions")
        resp.raise_for_status()
        return resp.json().get("sessions", [])

    async def health(self) -> dict:
        """Health check."""
        resp = await self._http.get("/health")
        resp.raise_for_status()
        return resp.json()

    async def stream_events(
        self, on_event: Callable[[str, dict], Awaitable[None]]
    ) -> None:
        """Subscribe to /events/json SSE stream.

        Reconnects automatically on disconnect. Calls on_event(event_type, data)
        for each event.
        """
        while True:
            try:
                await self._stream_once(on_event)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                logger.warning("SSE connection lost (%s), reconnecting in 2s...", e)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("SSE stream error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _stream_once(
        self, on_event: Callable[[str, dict], Awaitable[None]]
    ) -> None:
        """Single SSE connection. Raises on disconnect."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=None) as client:
            async with client.stream("GET", "/events/json") as resp:
                resp.raise_for_status()
                event_type = ""
                data_buf = ""

                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf += line[5:].strip()
                    elif line == "":
                        # End of event
                        if event_type and data_buf:
                            try:
                                data = json.loads(data_buf)
                                await on_event(event_type, data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Bad JSON in SSE event %s: %s",
                                    event_type,
                                    data_buf[:200],
                                )
                            except Exception:
                                logger.exception(
                                    "Error handling SSE event %s", event_type
                                )
                        event_type = ""
                        data_buf = ""
