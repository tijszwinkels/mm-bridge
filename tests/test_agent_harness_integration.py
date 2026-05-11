from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest


pytestmark = pytest.mark.asyncio


HARNESS_LIVE_URL = os.environ.get("HARNESS_LIVE_URL")


@pytest.mark.skipif(
    not HARNESS_LIVE_URL,
    reason="set HARNESS_LIVE_URL to run the live agent-harness smoke test",
)
async def test_live_harness_create_run_interrupt_smoke() -> None:
    assert HARNESS_LIVE_URL is not None

    timeout = httpx.Timeout(10.0, read=None)
    async with httpx.AsyncClient(
        base_url=HARNESS_LIVE_URL.rstrip("/"),
        timeout=timeout,
    ) as client:
        sessions = await _list_sessions(client)
        external = [s for s in sessions if s.get("origin") == "external"]
        assert external, "expected at least one observed external session"

        template = _select_template_session(external)
        with tempfile.TemporaryDirectory(prefix="mm-bridge-harness-smoke-") as tmp:
            session_id: str | None = None
            run_id: str | None = None
            try:
                session = await _create_session(client, template, Path(tmp))
                session_id = session["id"]

                run = await _create_run(client, session_id)
                run_id = run["run_id"]

                await _wait_for_event(client, session_id, "run.started", run_id, 10.0)

                interrupt = await client.delete(
                    f"/v1/sessions/{session_id}/runs/{run_id}",
                )
                assert interrupt.status_code == 200, interrupt.text

                await _wait_for_event(
                    client,
                    session_id,
                    "run.interrupted",
                    run_id,
                    5.0,
                )
            finally:
                if session_id is not None:
                    await client.delete(f"/v1/sessions/{session_id}")


async def _list_sessions(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get("/v1/sessions")
    response.raise_for_status()
    data = response.json()["data"]
    assert isinstance(data, list)
    return data


def _select_template_session(sessions: list[dict]) -> dict:
    for session in sessions:
        if session.get("backend") in {"codex", "claude-code"} and session.get("model"):
            return session
    pytest.fail("expected an external codex or claude-code session with a model")


async def _create_session(
    client: httpx.AsyncClient,
    template: dict,
    project_path: Path,
) -> dict:
    response = await client.post(
        "/v1/sessions",
        json={
            "backend": template["backend"],
            "model": template["model"],
            "project": {
                "path": str(project_path),
                "name": project_path.name,
            },
            "title": "mm-bridge live integration smoke",
        },
    )
    response.raise_for_status()
    return response.json()


async def _create_run(client: httpx.AsyncClient, session_id: str) -> dict:
    response = await client.post(
        f"/v1/sessions/{session_id}/runs",
        json={
            "message": (
                "Live integration smoke test. Wait for 30 seconds, then reply "
                "with 'integration smoke complete'. Do not edit files."
            ),
        },
    )
    response.raise_for_status()
    return response.json()


async def _wait_for_event(
    client: httpx.AsyncClient,
    session_id: str,
    event_name: str,
    run_id: str,
    timeout_seconds: float,
) -> dict:
    async with asyncio.timeout(timeout_seconds):
        async with client.stream(
            "GET",
            f"/v1/sessions/{session_id}/events",
            params={"from": "beginning"},
        ) as response:
            response.raise_for_status()
            data_buf = ""
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_buf += line[5:].strip()
                elif line == "" and data_buf:
                    payload = json.loads(data_buf)
                    data_buf = ""
                    if (
                        payload.get("event") == event_name
                        and payload.get("run_id") == run_id
                    ):
                        return payload
    raise AssertionError(f"timed out waiting for {event_name} for {run_id}")
