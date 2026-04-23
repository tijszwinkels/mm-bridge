"""Tests for mm_bridge.mm_client.MattermostClient."""

from __future__ import annotations

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
class FakeDriver:
    client: FakeDriverClient = field(default_factory=FakeDriverClient)


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
