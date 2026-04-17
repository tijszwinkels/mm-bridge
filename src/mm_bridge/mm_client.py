"""Mattermost REST + WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from mattermostautodriver import Driver

logger = logging.getLogger(__name__)


# WebSocket events the bridge dispatches on.
_HANDLED_EVENTS = {"posted", "user_added", "user_removed", "channel_updated"}


class MattermostClient:
    """Mattermost client: sync Driver for REST, raw aiohttp for WebSocket."""

    def __init__(
        self,
        url: str,
        port: int,
        scheme: str,
        token: str,
        team_name: str,
    ):
        self._driver = Driver({
            "url": url,
            "port": port,
            "scheme": scheme,
            "token": token,
            "verify": False,
        })
        self._url = url
        self._port = port
        self._scheme = scheme
        self._token = token
        self._team_name = team_name
        self._team_id: str = ""
        self._bot_user_id: str = ""
        self._bot_username: str = ""

    # ----- setup -----

    def login(self) -> None:
        self._driver.login()
        me = self._driver.users.get_user("me")
        self._bot_user_id = me["id"]
        self._bot_username = me["username"]
        team = self._driver.teams.get_team_by_name(self._team_name)
        self._team_id = team["id"]
        logger.info("Logged in as %s (team=%s)", me["username"], self._team_name)

    @property
    def bot_user_id(self) -> str:
        return self._bot_user_id

    @property
    def bot_username(self) -> str:
        return self._bot_username

    @property
    def team_id(self) -> str:
        return self._team_id

    # ----- posts -----

    def post_message(self, channel_id: str, message: str) -> dict:
        return self._driver.posts.create_post(options={
            "channel_id": channel_id,
            "message": message,
        })

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list[str] | None = None,
        root_id: str | None = None,
    ) -> dict:
        options: dict = {"channel_id": channel_id, "message": message}
        if file_ids:
            options["file_ids"] = file_ids
        if root_id:
            options["root_id"] = root_id
        return self._driver.posts.create_post(options=options)

    def update_post(self, post_id: str, message: str) -> dict:
        return self._driver.posts.update_post(post_id, options={
            "id": post_id,
            "message": message,
        })

    def get_posts(self, channel_id: str, limit: int) -> list[dict]:
        """Most-recent N posts, returned oldest-first."""
        resp = self._driver.posts.get_posts_for_channel(
            channel_id, params={"per_page": limit},
        )
        order = resp.get("order", [])
        posts = resp.get("posts", {})
        return [posts[pid] for pid in reversed(order) if pid in posts]

    def get_post(self, post_id: str) -> dict:
        return self._driver.posts.get_post(post_id)

    # ----- channels -----

    def create_channel(
        self, name: str, display_name: str, purpose: str = ""
    ) -> dict:
        return self._driver.channels.create_channel(options={
            "team_id": self._team_id,
            "name": name,
            "display_name": display_name,
            "purpose": purpose,
            "type": "O",
        })

    def set_channel_header(self, channel_id: str, header: str) -> None:
        self._driver.channels.patch_channel(channel_id, options={"header": header})

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self._driver.channels.patch_channel(channel_id, options={
            "display_name": display_name,
        })

    def get_channel(self, channel_id: str) -> dict:
        return self._driver.channels.get_channel(channel_id)

    def remove_self_from_channel(self, channel_id: str) -> None:
        """Remove the bot from a channel."""
        self._driver.channels.remove_channel_member(channel_id, self._bot_user_id)

    # ----- users -----

    def get_user(self, user_id: str) -> dict:
        return self._driver.users.get_user(user_id)

    def publish_user_typing(
        self, channel_id: str, parent_id: str | None = None
    ) -> None:
        """Publish a 'user is typing' event from the bot to the channel."""
        options: dict = {}
        if parent_id:
            options["parent_id"] = parent_id
        self._driver.users.publish_user_typing(
            self._bot_user_id, channel_id, options=options
        )

    # ----- files -----

    def upload_file(self, channel_id: str, path: Path) -> str:
        """Upload a file to a channel; return its file_id."""
        with path.open("rb") as fh:
            resp = self._driver.files.upload_file(
                files={"files": (path.name, fh)},
                data={"channel_id": channel_id},
            )
        infos = resp.get("file_infos") or []
        if not infos:
            raise RuntimeError(f"upload_file returned no file_infos: {resp!r}")
        return infos[0]["id"]

    def download_file(self, file_id: str) -> bytes:
        """Fetch the raw bytes of an uploaded file from Mattermost."""
        resp = self._driver.files.get_file(file_id)
        if isinstance(resp, (bytes, bytearray)):
            return bytes(resp)
        content = getattr(resp, "content", None)
        if content is None:
            raise RuntimeError(f"Unexpected get_file response: {type(resp).__name__}")
        return content

    def get_max_file_size(self) -> int:
        """Read MaxFileSize from the MM server config; 50MB fallback."""
        try:
            cfg = self._driver.system.get_client_configuration(params={"format": "old"})
            return int(cfg.get("MaxFileSize", 50 * 1024 * 1024))
        except Exception:
            logger.debug("get_max_file_size failed, using 50MB fallback", exc_info=True)
            return 50 * 1024 * 1024

    # ----- WebSocket -----

    async def listen_websocket(self, handlers: dict[str, Callable[..., Awaitable[None]]]) -> None:
        """Listen on the MM WS; dispatch events to named handlers.

        Expected keys in `handlers` (all optional):
          - "posted"          → fn(post: dict)
          - "user_added"      → fn(channel_id: str, user_id: str)
          - "user_removed"    → fn(channel_id: str, user_id: str)
          - "channel_updated" → fn(channel: dict)

        The bot's own `posted` events are filtered before dispatch.
        """
        ws_scheme = "wss" if self._scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{self._url}:{self._port}/api/v4/websocket"

        while True:
            try:
                await self._ws_connect(ws_url, handlers)
            except (aiohttp.ClientError, ConnectionError) as exc:
                logger.warning("MM WebSocket lost (%s), reconnecting in 2s...", exc)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("MM WebSocket error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _ws_connect(
        self,
        url: str,
        handlers: dict[str, Callable[..., Awaitable[None]]],
    ) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                await ws.send_json({
                    "seq": 1,
                    "action": "authentication_challenge",
                    "data": {"token": self._token},
                })
                logger.info("MM WebSocket connected and authenticated")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        await self._dispatch_event(event, handlers)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break

    async def _dispatch_event(
        self,
        event: dict,
        handlers: dict[str, Callable[..., Awaitable[None]]],
    ) -> None:
        event_type = event.get("event")
        if event_type not in _HANDLED_EVENTS:
            return

        data = event.get("data", {}) or {}
        broadcast = event.get("broadcast", {}) or {}

        try:
            if event_type == "posted":
                handler = handlers.get("posted")
                if not handler:
                    return
                post_json = data.get("post")
                if not post_json:
                    return
                try:
                    post = json.loads(post_json)
                except json.JSONDecodeError:
                    return
                if post.get("user_id") == self._bot_user_id:
                    return
                await handler(post)

            elif event_type == "user_added":
                handler = handlers.get("user_added")
                if not handler:
                    return
                user_id = data.get("user_id")
                channel_id = broadcast.get("channel_id") or data.get("channel_id")
                if user_id and channel_id:
                    await handler(channel_id, user_id)

            elif event_type == "user_removed":
                handler = handlers.get("user_removed")
                if not handler:
                    return
                user_id = data.get("user_id")
                channel_id = broadcast.get("channel_id") or data.get("channel_id")
                if user_id and channel_id:
                    await handler(channel_id, user_id)

            elif event_type == "channel_updated":
                handler = handlers.get("channel_updated")
                if not handler:
                    return
                channel_json = data.get("channel")
                if not channel_json:
                    return
                try:
                    channel = json.loads(channel_json)
                except (json.JSONDecodeError, TypeError):
                    channel = channel_json if isinstance(channel_json, dict) else None
                if channel:
                    await handler(channel)
        except Exception:
            logger.exception("handler for %s failed", event_type)
