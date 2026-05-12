from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mm_bridge.agent_harness_client import (
    AgentHarnessClient,
    HarnessForkUnsupported,
    HarnessInterruptUnsupported,
    HarnessResumeUnsupported,
    HarnessRunNotFound,
)


pytestmark = pytest.mark.asyncio


def _client(handler) -> AgentHarnessClient:
    return AgentHarnessClient.with_transport(
        "http://harness.test",
        httpx.MockTransport(handler),
    )


async def test_create_session_request_shape_aliases_backend_and_derives_project_name():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": "claude_abc",
                "backend": "claude-code",
                "model": "opus",
                "project": {"path": "/tmp/project", "name": "project"},
                "title": None,
                "origin": "harness",
                "status": "idle",
            },
        )

    client = _client(handler)
    session = await client.create_session(
        backend="claude",
        model="opus",
        cwd="/tmp/project",
    )

    assert seen == {
        "method": "POST",
        "path": "/v1/sessions",
        "body": {
            "backend": "claude-code",
            "model": "opus",
            "project": {"path": "/tmp/project", "name": "project"},
        },
    }
    assert session["id"] == "claude_abc"


async def test_create_session_drops_none_model_and_includes_title_when_present():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "codex_abc"})

    client = _client(handler)
    await client.create_session(
        backend="codex",
        model=None,
        cwd="/tmp/project",
        title="My session",
    )

    assert seen["body"] == {
        "backend": "codex",
        "project": {"path": "/tmp/project", "name": "project"},
        "title": "My session",
    }


async def test_create_run_posts_message_only_and_tracks_accepted_response():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            202,
            json={"session_id": "codex_abc", "run_id": "run_123"},
        )

    client = _client(handler)
    run = await client.create_run("codex_abc", "hello")

    assert seen == {
        "method": "POST",
        "path": "/v1/sessions/codex_abc/runs",
        "body": {"message": "hello"},
    }
    assert run == {"session_id": "codex_abc", "run_id": "run_123"}


async def test_create_run_409_maps_to_resume_unsupported():
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "external_resume_unsupported", "detail": "nope"}},
        )

    client = _client(handler)

    with pytest.raises(HarnessResumeUnsupported):
        await client.create_run("codex_abc", "hello")


async def test_fork_session_unwraps_session_and_optional_run():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "session": {"id": "codex_child"},
                "run": {"id": "run_child"},
            },
        )

    client = _client(handler)
    response = await client.fork_session("codex_parent", message="continue")

    assert seen == {
        "path": "/v1/sessions/codex_parent/forks",
        "body": {"message": "continue"},
    }
    assert response["session"]["id"] == "codex_child"
    assert response["run"]["id"] == "run_child"


@pytest.mark.parametrize("status", [404, 409])
async def test_fork_session_unsupported_errors_are_typed(status: int):
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": "fork_unsupported", "detail": "no fork"}},
        )

    client = _client(handler)

    with pytest.raises(HarnessForkUnsupported):
        await client.fork_session("codex_parent", message="continue")


async def test_interrupt_run_maps_terminal_errors():
    responses = [
        httpx.Response(409, json={"error": {"code": "external_interrupt_unsupported"}}),
        httpx.Response(404, json={"error": {"code": "run_not_found"}}),
    ]

    async def handler(req: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = _client(handler)

    with pytest.raises(HarnessInterruptUnsupported):
        await client.interrupt_run("codex_abc", "run_123")
    with pytest.raises(HarnessRunNotFound):
        await client.interrupt_run("codex_abc", "run_123")


async def test_get_session_list_sessions_models_messages_and_health():
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/sessions/missing":
            return httpx.Response(404, json={"error": {"code": "session_not_found"}})
        if req.url.path == "/v1/sessions/codex_abc":
            return httpx.Response(200, json={"id": "codex_abc"})
        if req.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": [{"id": "codex_abc"}]})
        if req.url.path == "/v1/backends/claude-code/models":
            return httpx.Response(200, json={"data": ["opus"]})
        if req.url.path == "/v1/backends/unknown/models":
            return httpx.Response(404, json={"error": {"code": "backend_not_found"}})
        if req.url.path == "/v1/sessions/codex_abc/messages":
            return httpx.Response(200, json={"data": [{"id": "msg_1"}]})
        if req.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {req.url}")

    client = _client(handler)

    assert await client.get_session("missing") is None
    assert await client.get_session("codex_abc") == {"id": "codex_abc"}
    assert await client.list_sessions() == [{"id": "codex_abc"}]
    assert await client.list_backend_models("claude") == ["opus"]
    assert await client.list_backend_models("unknown") == []
    assert await client.list_session_messages("codex_abc") == [{"id": "msg_1"}]
    assert await client.health() == {"status": "ok"}


async def test_stream_events_dispatches_parsed_events_and_reconnects_after_sequence():
    requests: list[str] = []
    events: list[tuple[str, dict]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        if len(requests) == 1:
            payload = {"sequence": 41, "event": "session.updated", "data": {"x": 1}}
        else:
            assert req.url.params.get("after") == "41"
            payload = {"sequence": 42, "event": "message", "data": {"x": 2}}
        body = (
            f"event: {payload['event']}\n"
            f"data: {json.dumps(payload)}\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(event_name: str, data: dict) -> None:
        events.append((event_name, data))
        if len(events) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event)

    assert [name for name, _ in events] == ["session.updated", "message"]
    assert requests[0] == "http://harness.test/v1/events"
    assert requests[1] == "http://harness.test/v1/events?after=41"


async def test_stream_events_starts_from_after_sequence_when_provided():
    seen_params: list[str | None] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen_params.append(req.url.params.get("after"))
        payload = {"sequence": 101, "event": "ping", "data": {}}
        body = (f"event: ping\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)
    stopped = asyncio.Event()

    async def on_event(_name: str, _data: dict) -> None:
        stopped.set()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, after_sequence=100)

    assert seen_params[0] == "100", "cold-start cursor must be forwarded to harness"


async def test_stream_events_fires_on_progress_per_event():
    """on_progress is the bridge's hook to persist last_event_seq."""
    progress: list[int] = []
    events_seen = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal events_seen
        events_seen += 1
        seq = 50 + events_seen
        payload = {"sequence": seq, "event": "message", "data": {}}
        body = (f"event: message\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(_name: str, _data: dict) -> None:
        if len(progress) >= 2:
            raise asyncio.CancelledError

    async def on_progress(seq: int) -> None:
        progress.append(seq)

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, on_progress=on_progress)

    assert progress == [51, 52]


async def test_probe_current_sequence_returns_highest_seq_and_stops_idle():
    """Probe drains the SSE replay quickly and returns the highest seq."""
    async def handler(req: httpx.Request) -> httpx.Response:
        body = (
            "event: session.updated\n"
            "data: " + json.dumps({"sequence": 7, "event": "session.updated"}) + "\n\n"
            "event: message\n"
            "data: " + json.dumps({"sequence": 12, "event": "message"}) + "\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    client = _client(handler)
    seq = await client.probe_current_sequence(idle_window=0.05, hard_timeout=1.0)
    assert seq == 12


async def test_probe_current_sequence_returns_zero_on_empty_stream():
    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = _client(handler)
    seq = await client.probe_current_sequence(idle_window=0.05, hard_timeout=0.5)
    assert seq == 0


async def test_stream_events_preserves_cursor_across_reconnects():
    """Reconnect after a mid-stream error must continue from the latest seq
    observed — NOT from the original ``after_sequence``. Regression for the
    2026-05-12 cutover #3 bug where every 30s SSE ReadTimeout caused a
    reconnect with the stale cold-start cursor, re-streaming every event
    in between and re-mirroring user messages."""
    requests: list[str] = []
    events_seen: list[int] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        if len(requests) == 1:
            # First connection: emit two events, then close.
            payload_a = {"sequence": 101, "event": "message", "data": {}}
            payload_b = {"sequence": 102, "event": "message", "data": {}}
            body = (
                f"event: message\ndata: {json.dumps(payload_a)}\n\n"
                f"event: message\ndata: {json.dumps(payload_b)}\n\n"
            ).encode()
            return httpx.Response(200, content=body)
        # Second connection: must come with after=102, not the original 100.
        assert req.url.params.get("after") == "102", (
            f"expected after=102 on reconnect, got after="
            f"{req.url.params.get('after')!r}"
        )
        payload = {"sequence": 103, "event": "message", "data": {}}
        body = (f"event: message\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(_name: str, data: dict) -> None:
        events_seen.append(data.get("sequence"))
        if len(events_seen) >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, after_sequence=100)

    assert events_seen == [101, 102, 103]
    assert requests[0].endswith("after=100")
    assert "after=102" in requests[1]
