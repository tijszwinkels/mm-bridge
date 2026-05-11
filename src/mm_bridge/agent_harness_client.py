"""agent-harness HTTP + SSE client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


_BACKEND_WIRE: dict[str, str] = {"claude": "claude-code"}


def _wire_backend(name: str) -> str:
    return _BACKEND_WIRE.get(name.lower(), name)


class HarnessError(Exception):
    """Base class for typed agent-harness errors."""


class HarnessSessionNotFound(HarnessError):
    """The requested session does not exist."""


class HarnessRunNotFound(HarnessError):
    """The requested run does not exist or is already terminal."""


class HarnessResumeUnsupported(HarnessError):
    """The harness cannot create a run for this session."""


class HarnessInterruptUnsupported(HarnessError):
    """The harness cannot interrupt this run."""


class HarnessForkUnsupported(HarnessError):
    """The harness cannot fork this session/backend."""


class AgentHarnessClient:
    """Async client for agent-harness REST API and SSE event stream."""

    def __init__(
        self,
        base_url: str,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30,
            transport=_transport,
        )

    @classmethod
    def with_transport(
        cls,
        base_url: str,
        transport: httpx.AsyncBaseTransport,
    ) -> AgentHarnessClient:
        return cls(base_url, _transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def create_session(
        self,
        *,
        backend: str,
        model: str | None,
        cwd: str,
        title: str | None = None,
    ) -> dict:
        payload: dict = {
            "backend": _wire_backend(backend),
            "project": {"path": cwd, "name": Path(cwd).name},
        }
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title

        resp = await self._http.post("/v1/sessions", json=payload)
        self._raise_for_status(resp)
        return resp.json()

    async def create_run(self, session_id: str, message: str) -> dict:
        resp = await self._http.post(
            f"/v1/sessions/{session_id}/runs",
            json={"message": message},
        )
        if resp.status_code == 409:
            raise HarnessResumeUnsupported(_error_detail(resp))
        if resp.status_code == 404:
            raise HarnessSessionNotFound(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def fork_session(
        self,
        session_id: str,
        *,
        message: str | None,
        title: str | None = None,
    ) -> dict:
        payload: dict = {}
        if message is not None:
            payload["message"] = message
        if title is not None:
            payload["title"] = title

        resp = await self._http.post(f"/v1/sessions/{session_id}/forks", json=payload)
        if resp.status_code in (404, 409):
            raise HarnessForkUnsupported(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def interrupt_run(self, session_id: str, run_id: str) -> dict:
        resp = await self._http.delete(f"/v1/sessions/{session_id}/runs/{run_id}")
        if resp.status_code == 409:
            raise HarnessInterruptUnsupported(_error_detail(resp))
        if resp.status_code == 404:
            raise HarnessRunNotFound(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def get_session(self, session_id: str) -> dict | None:
        resp = await self._http.get(f"/v1/sessions/{session_id}")
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        return resp.json()

    async def list_sessions(self) -> list[dict]:
        resp = await self._http.get("/v1/sessions")
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def list_backend_models(self, backend: str) -> list[str]:
        try:
            resp = await self._http.get(f"/v1/backends/{_wire_backend(backend)}/models")
            if resp.status_code == 404:
                return []
            self._raise_for_status(resp)
            return resp.json().get("data", []) or []
        except httpx.HTTPError as exc:
            logger.warning("list_backend_models(%s) failed: %s", backend, exc)
            return []

    async def list_session_messages(self, session_id: str) -> list[dict]:
        resp = await self._http.get(f"/v1/sessions/{session_id}/messages")
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def health(self) -> dict:
        resp = await self._http.get("/v1/health")
        self._raise_for_status(resp)
        return resp.json()

    async def stream_events(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        after_sequence: int | None = None,
    ) -> None:
        last_sequence = after_sequence
        while True:
            try:
                last_sequence = await self._stream_once(on_event, last_sequence)
            except (
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.ConnectError,
            ) as exc:
                logger.warning("SSE connection lost (%s), reconnecting in 2s...", exc)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("SSE stream error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _stream_once(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        after_sequence: int | None,
    ) -> int | None:
        params = {"after": str(after_sequence)} if after_sequence is not None else None
        async with self._http.stream("GET", "/v1/events", params=params) as resp:
            resp.raise_for_status()
            event_type = ""
            data_buf = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_buf += line[5:].strip()
                elif line == "":
                    if data_buf:
                        after_sequence = await self._dispatch_sse_event(
                            event_type, data_buf, on_event, after_sequence,
                        )
                    event_type = ""
                    data_buf = ""
        return after_sequence

    async def _dispatch_sse_event(
        self,
        event_type: str,
        data_buf: str,
        on_event: Callable[[str, dict], Awaitable[None]],
        after_sequence: int | None,
    ) -> int | None:
        try:
            data = json.loads(data_buf)
        except json.JSONDecodeError:
            logger.warning("Bad JSON in SSE event %s: %s", event_type, data_buf[:200])
            return after_sequence

        sequence = data.get("sequence")
        if isinstance(sequence, int):
            after_sequence = max(after_sequence or sequence, sequence)

        await on_event(data.get("event") or event_type, data)
        return after_sequence

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if not resp.is_error:
            return
        detail = _error_detail(resp)
        raise httpx.HTTPStatusError(
            f"agent-harness {resp.request.method} {resp.request.url.path} "
            f"-> {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:500]

    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        detail = error.get("detail") or error.get("message")
        if code and detail:
            return f"{code}: {detail}"
        if code:
            return str(code)
        if detail:
            return str(detail)
    detail = body.get("detail")
    if detail:
        return str(detail)
    return str(body)[:500]
