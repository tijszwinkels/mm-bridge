"""VibeDeck HTTP + SSE client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


# VD accepts mixed names for /sessions/new. Our purpose tokens are lowercase
# ("claude", "codex", ...). VD does not recognise the bare token "claude" —
# it wants the CLI-backend name ("claude-code" / "Claude Code"). For the
# others, lowercase works as-is.
_BACKEND_WIRE_ALIAS: dict[str, str] = {
    "claude": "claude-code",
}


def _to_wire_backend(name: str) -> str:
    return _BACKEND_WIRE_ALIAS.get(name.lower(), name)


def canon_backend(name: str | None) -> str | None:
    """Canonical form for comparing backend strings across bridge/VD.

    Collapses case, whitespace, hyphens, and underscores so that the
    purpose token ``claude``, the wire name ``claude-code``, and VD's SSE
    display name ``Claude Code`` all hash to the same value.
    """
    if not name:
        return None
    s = "".join(c for c in name.lower() if c.isalnum())
    if s == "claude":
        s = "claudecode"
    return s


class VibeDeckClient:
    """Async client for VibeDeck's REST API and SSE event stream."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30)
        self._model_cache: dict[str, list[str]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ----- session lifecycle -----

    async def create_session(
        self,
        message: str,
        cwd: str,
        backend: str | None = None,
        model_index: int | None = None,
        source_session_id: str | None = None,
    ) -> dict:
        payload: dict = {"message": message, "cwd": cwd}
        if backend:
            payload["backend"] = _to_wire_backend(backend)
        if model_index is not None:
            payload["model_index"] = model_index
        if source_session_id:
            payload["source_session_id"] = source_session_id

        resp = await self._http.post("/sessions/new", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict:
        resp = await self._http.post(
            f"/sessions/{session_id}/send",
            json={"message": message},
        )
        resp.raise_for_status()
        return resp.json()

    async def interrupt_session(self, session_id: str) -> dict:
        resp = await self._http.post(f"/sessions/{session_id}/interrupt")
        resp.raise_for_status()
        return resp.json()

    async def fork_session(self, session_id: str, message: str) -> dict:
        """Fork a session. Returns the VibeDeck response plus a `status` hint.

        Success → {"status": "forking", "session_id": <parent>}; the new
        session_id arrives via SSE `session_added`.
        Fork disabled → {"status": "fork_unavailable", "reason": ..., "http_status": 403}.
        Unsupported backend → same shape with http_status=501.
        """
        resp = await self._http.post(
            f"/sessions/{session_id}/fork",
            json={"message": message},
        )
        if resp.status_code in (403, 501):
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                pass
            return {
                "status": "fork_unavailable",
                "reason": detail or f"HTTP {resp.status_code}",
                "http_status": resp.status_code,
            }
        resp.raise_for_status()
        return resp.json()

    async def list_sessions(self) -> list[dict]:
        resp = await self._http.get("/sessions")
        resp.raise_for_status()
        return resp.json().get("sessions", [])

    async def get_session_meta(self, session_id: str) -> dict:
        """Look up one session's metadata from /sessions; {} if not found."""
        for s in await self.list_sessions():
            if s.get("id") == session_id:
                return s
        return {}

    async def set_session_title(self, session_id: str, title: str | None) -> None:
        resp = await self._http.post(
            "/api/session-titles/set",
            json={"session_id": session_id, "title": title},
        )
        resp.raise_for_status()

    async def list_models(self, backend: str) -> list[str]:
        """Return model names for a backend. Cached in-process.

        Uses the same wire-alias mapping as `create_session` so the
        purpose token ``claude`` reaches VD as ``claude-code``.
        """
        if backend in self._model_cache:
            return self._model_cache[backend]
        wire = _to_wire_backend(backend)
        try:
            resp = await self._http.get(f"/backends/{wire}/models")
            resp.raise_for_status()
            models = resp.json().get("models", []) or []
        except httpx.HTTPError as exc:
            logger.warning("list_models(%s) failed: %s", backend, exc)
            models = []
        self._model_cache[backend] = models
        return models

    async def health(self) -> dict:
        resp = await self._http.get("/health")
        resp.raise_for_status()
        return resp.json()

    # ----- SSE -----

    async def stream_events(
        self, on_event: Callable[[str, dict], Awaitable[None]]
    ) -> None:
        """Subscribe to `/events/json`. Reconnects on disconnect."""
        while True:
            try:
                await self._stream_once(on_event)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                logger.warning("SSE connection lost (%s), reconnecting in 2s...", exc)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("SSE stream error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _stream_once(
        self, on_event: Callable[[str, dict], Awaitable[None]]
    ) -> None:
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
                        if event_type and data_buf:
                            try:
                                data = json.loads(data_buf)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Bad JSON in SSE event %s: %s",
                                    event_type, data_buf[:200],
                                )
                                data = None
                            if data is not None:
                                try:
                                    await on_event(event_type, data)
                                except Exception:
                                    logger.exception(
                                        "Error handling SSE event %s", event_type
                                    )
                        event_type = ""
                        data_buf = ""
