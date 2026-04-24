"""Tests for mm_bridge.mm_client.MattermostClient."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from mm_bridge.mm_client import MattermostClient


@dataclass
class FakeHttpxResponse:
    """Stand-in for httpx.Response — only exposes .content."""

    content: bytes


@dataclass
class FakeDriverClient:
    """Stand-in for mattermostautodriver.client.Client."""

    responses: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def make_request(self, method: str, endpoint: str, **_: Any) -> FakeHttpxResponse:
        self.calls.append((method, endpoint))
        return FakeHttpxResponse(content=self.responses.get(endpoint, b""))


@dataclass
class FakePostsApi:
    """Stand-in for Driver.posts — records create/update calls and
    returns the id callers hand back via ``next_id``."""
    created: list[dict] = field(default_factory=list)
    updated: list[tuple[str, dict]] = field(default_factory=list)
    channel_responses: list[dict] = field(default_factory=list)
    channel_calls: list[tuple[str, dict]] = field(default_factory=list)
    next_id: str = "new-id"

    def create_post(self, options):
        self.created.append(options)
        return {"id": self.next_id, **options}

    def update_post(self, post_id, options):
        self.updated.append((post_id, options))
        return {"id": post_id, **options}

    def get_posts_for_channel(self, channel_id, params=None):
        self.channel_calls.append((channel_id, params or {}))
        idx = len(self.channel_calls) - 1
        if idx >= len(self.channel_responses):
            raise AssertionError("get_posts_since did not stop after repeated page")
        return self.channel_responses[idx]


@dataclass
class FakeDriver:
    client: FakeDriverClient = field(default_factory=FakeDriverClient)
    posts: FakePostsApi = field(default_factory=FakePostsApi)


def _make_client_with_driver(driver: FakeDriver) -> MattermostClient:
    """Build a MattermostClient wired to a fake driver (skips real login)."""
    with patch("mm_bridge.mm_client.Driver", return_value=driver):
        return MattermostClient(
            url="mm.example", port=443, scheme="https",
            token="t", team_name="team",
        )


def test_download_file_returns_raw_bytes_for_json_attachment():
    """Regression: JSON attachments must round-trip byte-for-byte.

    The underlying mattermostautodriver.Client.get() auto-parses any
    application/json response into a dict, which would mangle JSON file
    downloads. download_file() must bypass that path.
    """
    gcp_key = (
        b'{\n  "type": "service_account",\n'
        b'  "project_id": "plenny-poc",\n'
        b'  "private_key_id": "cd77b561f2e4abc"\n}\n'
    )
    driver = FakeDriver()
    driver.client.responses["/api/v4/files/fid123"] = gcp_key
    client = _make_client_with_driver(driver)

    data = client.download_file("fid123")

    assert data == gcp_key
    assert driver.client.calls == [("get", "/api/v4/files/fid123")]


def test_download_file_returns_raw_bytes_for_binary_attachment():
    """Non-JSON content types (PDFs, images) still work."""
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nbinary-content"
    driver = FakeDriver()
    driver.client.responses["/api/v4/files/pdf99"] = pdf_bytes
    client = _make_client_with_driver(driver)

    assert client.download_file("pdf99") == pdf_bytes


def test_get_posts_since_breaks_when_pagination_repeats_page():
    """Regression: repeated full pages must not loop forever."""
    page = {
        "order": ["p1", "p2"],
        "posts": {
            "p1": {"id": "p1", "create_at": 100},
            "p2": {"id": "p2", "create_at": 200},
        },
    }
    driver = FakeDriver()
    driver.posts.channel_responses = [page, page]
    client = _make_client_with_driver(driver)

    posts = client.get_posts_since("c1", 123, per_page=2)

    assert [p["id"] for p in posts] == ["p1", "p2"]
    assert [params["page"] for _, params in driver.posts.channel_calls] == [0, 1]


# ───────────────────── Own-post tracking ──────────────────────────────────


def test_post_records_returned_id_as_own():
    driver = FakeDriver()
    driver.posts.next_id = "own-123"
    client = _make_client_with_driver(driver)

    client.post("c1", "hi")

    assert client.is_own_post("own-123") is True
    assert client.is_own_post("") is False
    assert client.is_own_post("someone-else") is False


def test_post_message_records_returned_id_as_own():
    driver = FakeDriver()
    driver.posts.next_id = "own-456"
    client = _make_client_with_driver(driver)

    client.post_message("c1", "hello")

    assert client.is_own_post("own-456") is True


def test_update_post_records_post_id_as_own():
    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    client.update_post("edited-789", "new body")

    assert client.is_own_post("edited-789") is True


def test_own_post_history_evicts_oldest_when_full():
    """The tracker is a bounded LRU-ish deque — once full, the oldest id
    ages out so the lookup set doesn't grow without bound."""
    import mm_bridge.mm_client as mc

    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    first = "first-id"
    client._record_own_post(first)
    for i in range(mc._OWN_POST_HISTORY_MAX):
        client._record_own_post(f"pid-{i}")

    assert client.is_own_post(first) is False
    assert client.is_own_post(f"pid-{mc._OWN_POST_HISTORY_MAX - 1}") is True


# ───────────────────── WS dispatch echo suppression ───────────────────────


def test_ws_posted_suppresses_our_own_echo():
    """A ``posted`` WS event whose post id matches one this process
    just authored is filtered before dispatch — that's how we swallow
    the MM-side echo of our own posts without help from a user_id
    check."""
    import json as _json

    driver = FakeDriver()
    driver.posts.next_id = "echo-1"
    client = _make_client_with_driver(driver)
    client.post("c1", "outbound")  # records "echo-1" as own

    captured: list[dict] = []

    async def handler(post):
        captured.append(post)

    echo_event = {
        "event": "posted",
        "data": {"post": _json.dumps({"id": "echo-1", "user_id": "bot", "message": "outbound"})},
    }
    asyncio.run(client._dispatch_event(echo_event, {"posted": handler}))

    assert captured == []


def test_ws_posted_forwards_cross_identity_bot_post():
    """Regression: posts by another actor sharing the bot identity
    (sibling mm-bridge sessions, other scripts using MM_BOT_TOKEN,
    humans posting as ``@claude``) reach the handler — they are *not*
    echoes of our own posts."""
    import json as _json

    driver = FakeDriver()
    driver.posts.next_id = "own-1"
    client = _make_client_with_driver(driver)
    client.post("c1", "ours")  # tracks "own-1"

    captured: list[dict] = []

    async def handler(post):
        captured.append(post)

    event = {
        "event": "posted",
        "data": {"post": _json.dumps({
            "id": "foreign-1", "user_id": "bot",
            "message": "from another session using the same token",
        })},
    }
    asyncio.run(client._dispatch_event(event, {"posted": handler}))

    assert len(captured) == 1
    assert captured[0]["id"] == "foreign-1"
