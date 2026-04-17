"""Mattermost REST + WebSocket client."""

import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp
from mattermostautodriver import Driver

logger = logging.getLogger(__name__)


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

    def login(self) -> None:
        """Login and resolve team/bot IDs."""
        self._driver.login()
        me = self._driver.users.get_user("me")
        self._bot_user_id = me["id"]
        team = self._driver.teams.get_team_by_name(self._team_name)
        self._team_id = team["id"]
        logger.info(
            "Logged in as %s (team=%s)", me["username"], self._team_name
        )

    @property
    def bot_user_id(self) -> str:
        return self._bot_user_id

    @property
    def team_id(self) -> str:
        return self._team_id

    def post_message(self, channel_id: str, message: str) -> dict:
        """Post a message to a channel."""
        return self._driver.posts.create_post(options={
            "channel_id": channel_id,
            "message": message,
        })

    def update_post(self, post_id: str, message: str) -> dict:
        """Update an existing post."""
        return self._driver.posts.update_post(post_id, options={
            "id": post_id,
            "message": message,
        })

    def create_channel(
        self, name: str, display_name: str, purpose: str = ""
    ) -> dict:
        """Create a public channel and add the bot to it."""
        return self._driver.channels.create_channel(options={
            "team_id": self._team_id,
            "name": name,
            "display_name": display_name,
            "purpose": purpose,
            "type": "O",
        })

    def set_channel_header(self, channel_id: str, header: str) -> None:
        """Update a channel's header (shown below channel name)."""
        self._driver.channels.patch_channel(channel_id, options={
            "header": header,
        })

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        """Rename a channel's display name."""
        self._driver.channels.patch_channel(channel_id, options={
            "display_name": display_name,
        })

    def get_channel(self, channel_id: str) -> dict:
        """Get channel info."""
        return self._driver.channels.get_channel(channel_id)

    def get_channels_for_bot(self) -> list[dict]:
        """List channels the bot is a member of."""
        return self._driver.channels.get_channels_for_user(
            self._bot_user_id, self._team_id
        )

    def join_all_team_channels(self) -> int:
        """Join all public channels in the team. Returns count of newly joined."""
        all_channels = self._driver.client.get(
            f"/api/v4/teams/{self._team_id}/channels"
        )
        bot_channels = {
            c["id"] for c in self.get_channels_for_bot()
        }
        joined = 0
        for ch in all_channels:
            if ch["id"] not in bot_channels:
                try:
                    self._driver.channels.add_channel_member(ch["id"], options={
                        "user_id": self._bot_user_id,
                    })
                    logger.info("Joined channel: %s", ch["display_name"])
                    joined += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to join channel %s: %s",
                        ch["display_name"],
                        exc,
                    )
        return joined

    def join_channel(self, channel_id: str) -> None:
        """Join a specific channel."""
        self._driver.channels.add_channel_member(channel_id, options={
            "user_id": self._bot_user_id,
        })

    async def listen_websocket(
        self,
        on_message: Callable[[dict], Awaitable[None]],
        on_channel_created: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Listen for posted messages via native aiohttp WebSocket.

        Reconnects automatically on disconnect.
        """
        ws_scheme = "wss" if self._scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{self._url}:{self._port}/api/v4/websocket"

        while True:
            try:
                await self._ws_connect(ws_url, on_message, on_channel_created)
            except (aiohttp.ClientError, ConnectionError) as e:
                logger.warning("MM WebSocket lost (%s), reconnecting in 2s...", e)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("MM WebSocket error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _ws_connect(
        self,
        url: str,
        on_message: Callable[[dict], Awaitable[None]],
        on_channel_created: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Single WebSocket connection lifecycle."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                # Authenticate
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

                        event_type = event.get("event")

                        # Auto-join newly created channels
                        if event_type == "channel_created" and on_channel_created:
                            channel_id = event.get("data", {}).get("channel_id", "")
                            if channel_id:
                                await on_channel_created(channel_id)
                            continue

                        if event_type != "posted":
                            continue

                        post_json = event.get("data", {}).get("post")
                        if not post_json:
                            continue

                        try:
                            post = json.loads(post_json)
                        except json.JSONDecodeError:
                            continue

                        # Ignore bot's own messages
                        if post.get("user_id") == self._bot_user_id:
                            continue

                        await on_message(post)

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
