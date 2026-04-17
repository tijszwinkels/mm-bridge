import tempfile
import unittest

from mm_bridge.bridge import Bridge
from mm_bridge.config import Config
from mm_bridge.mm_client import MattermostClient


class FakeMattermostClient:
    def __init__(self):
        self.listen_args = None
        self.joined_channels = []
        self.join_all_calls = 0
        self.created_channels = []
        self.posted_messages = []

    async def listen_websocket(self, on_message, on_channel_created=None):
        self.listen_args = (on_message, on_channel_created)

    def join_channel(self, channel_id: str) -> None:
        self.joined_channels.append(channel_id)

    def join_all_team_channels(self) -> int:
        self.join_all_calls += 1
        return 0

    def create_channel(self, name: str, display_name: str, purpose: str = "") -> dict:
        self.created_channels.append((name, display_name, purpose))
        return {"id": "created-channel"}

    def post_message(self, channel_id: str, message: str) -> dict:
        self.posted_messages.append((channel_id, message))
        return {"id": "post-1"}


class FakeVibeDeckClient:
    def __init__(self):
        self.sent_messages = []
        self.created_sessions = []

    async def send_message(self, session_id: str, message: str) -> dict:
        self.sent_messages.append((session_id, message))
        return {"status": "sent"}

    async def create_session(
        self,
        message: str,
        cwd: str,
        backend: str | None = None,
        model_index: int | None = None,
        source_session_id: str | None = None,
    ) -> dict:
        self.created_sessions.append(
            (message, cwd, backend, model_index, source_session_id)
        )
        return {"status": "started", "cwd": cwd}


class FakeChannelsApi:
    def __init__(self):
        self.add_channel_member_calls = []

    def add_channel_member(self, channel_id, options):
        self.add_channel_member_calls.append((channel_id, options))


class FakeDriver:
    def __init__(self, channels_api):
        self.channels = channels_api
        self.client = None


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = Config(
            mm_bot_token="test-token",
            state_file=f"{self.temp_dir.name}/state.json",
            vd_default_cwd="/tmp/mm-bridge-tests",
            vd_new_session_backend="opencode",
            vd_new_session_model_index=2,
        )
        self.bridge = Bridge(self.config)
        self.bridge.mm = FakeMattermostClient()
        self.bridge.vd = FakeVibeDeckClient()
        self.bridge.mapping.link("channel-1", "session-1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_run_mm_listener_registers_channel_created_callback(self):
        await self.bridge._run_mm_listener()

        _, on_channel_created = self.bridge.mm.listen_args
        self.assertIsNotNone(on_channel_created)

    async def test_on_mm_message_ignores_system_posts(self):
        await self.bridge._on_mm_message({
            "channel_id": "channel-1",
            "message": "admin archived the channel.",
            "type": "system_channel_deleted",
        })

        self.assertEqual(self.bridge.vd.sent_messages, [])

    async def test_on_mm_message_forwards_regular_posts(self):
        await self.bridge._on_mm_message({
            "channel_id": "channel-1",
            "message": "hello from mattermost",
            "type": "",
        })

        self.assertEqual(
            self.bridge.vd.sent_messages,
            [("session-1", "hello from mattermost")],
        )

    async def test_on_mm_message_starts_new_session_for_unmapped_channel(self):
        await self.bridge._on_mm_message({
            "channel_id": "channel-new",
            "message": "start a new session",
            "type": "",
        })

        self.assertEqual(
            self.bridge.vd.created_sessions,
            [("start a new session", "/tmp/mm-bridge-tests", "opencode", 2, None)],
        )

    async def test_follow_up_messages_queue_until_session_added(self):
        await self.bridge._on_mm_message({
            "channel_id": "channel-new",
            "message": "start a new session",
            "type": "",
        })
        await self.bridge._on_mm_message({
            "channel_id": "channel-new",
            "message": "second message",
            "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created_sessions), 1)
        self.assertEqual(
            self.bridge.pending_mm_channels["channel-new"].queued_messages,
            ["second message"],
        )

    async def test_session_added_claims_pending_channel_and_flushes_queue(self):
        await self.bridge._on_mm_message({
            "channel_id": "channel-new",
            "message": "start a new session",
            "type": "",
        })
        await self.bridge._on_mm_message({
            "channel_id": "channel-new",
            "message": "second message",
            "type": "",
        })

        await self.bridge._on_vd_event("session_added", {
            "id": "session-new",
            "projectPath": "/tmp/mm-bridge-tests",
            "backend": "opencode",
            "firstMessage": "start a new session",
        })

        self.assertEqual(
            self.bridge.mapping.get_session("channel-new"),
            "session-new",
        )
        self.assertEqual(
            self.bridge.vd.sent_messages,
            [("session-new", "second message")],
        )
        self.assertEqual(self.bridge.mm.created_channels, [])

    async def test_on_mm_channel_created_joins_channel(self):
        await self.bridge._on_mm_channel_created("channel-2")

        self.assertEqual(self.bridge.mm.joined_channels, ["channel-2"])

    def test_reconcile_mm_channel_membership_once(self):
        joined = self.bridge._reconcile_mm_channel_membership_once()

        self.assertEqual(joined, 0)
        self.assertEqual(self.bridge.mm.join_all_calls, 1)


class MattermostClientTests(unittest.TestCase):
    def test_join_channel_uses_add_channel_member(self):
        channels_api = FakeChannelsApi()
        client = MattermostClient.__new__(MattermostClient)
        client._driver = FakeDriver(channels_api)
        client._bot_user_id = "bot-user"

        client.join_channel("channel-123")

        self.assertEqual(
            channels_api.add_channel_member_calls,
            [("channel-123", {"user_id": "bot-user"})],
        )
