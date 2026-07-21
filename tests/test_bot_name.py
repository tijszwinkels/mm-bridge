"""Bot-name de-hardcoding (D7): command + mention logic follows the runtime
bot username instead of a hardcoded ``@claude`` literal.

Two invariants:
  * For a bot actually named ``claude`` (the existing deployment) behaviour is
    identical to before — ``@claude`` / ``@Claude`` still match everywhere.
  * For any other handle (e.g. ``b3mo``) the same logic keys off *that* name,
    and a stray ``@claude`` no longer gets special treatment.
"""

from __future__ import annotations

import tempfile
import unittest

from mm_bridge import purpose
from mm_bridge.bridge import Bridge
from mm_bridge.config import Config

from doubles import FakeAgentHarnessClient, FakeMattermostClient


def _bridge(bot_username: str) -> Bridge:
    """A Bridge whose MM client reports ``bot_username`` (assigned post-init,
    matching how the real login sets it after construction)."""
    tmp = tempfile.mkdtemp()
    cfg = Config(
        mm_bot_token="t",
        state_file=f"{tmp}/state.json",
        sidecar_dir=f"{tmp}/sidecar",
    )
    bridge = Bridge(cfg)
    bridge.mm = FakeMattermostClient(bot_username=bot_username)
    bridge.harness = FakeAgentHarnessClient()
    return bridge


class BotNameRegexTests(unittest.TestCase):
    def test_catch_up_matches_runtime_handle(self) -> None:
        b = _bridge("b3mo")
        m = b._catch_up_re.match("@b3mo catch up 5")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "5")

    def test_catch_up_ignores_foreign_handle_for_nonclaude_bot(self) -> None:
        self.assertIsNone(_bridge("b3mo")._catch_up_re.match("@claude catch up"))

    def test_catch_up_unchanged_for_claude_bot(self) -> None:
        b = _bridge("claude")
        self.assertIsNotNone(b._catch_up_re.match("@claude catch up"))
        self.assertIsNotNone(b._catch_up_re.match("@Claude catch up"))  # IGNORECASE

    def test_leave_follows_runtime_handle(self) -> None:
        b = _bridge("b3mo")
        self.assertIsNotNone(b._leave_cmd_re.match("@b3mo leave"))
        self.assertIsNone(b._leave_cmd_re.match("@claude leave"))

    def test_stop_follows_runtime_handle_and_bare_still_works(self) -> None:
        b = _bridge("b3mo")
        self.assertIsNotNone(b._stop_cmd_re.match("@b3mo stop"))
        self.assertIsNotNone(b._stop_cmd_re.match("stop"))  # bare stop unchanged
        self.assertIsNone(b._stop_cmd_re.match("@claude stop"))


class BotNameMentionTests(unittest.TestCase):
    def test_mentions_runtime_handle(self) -> None:
        b = _bridge("b3mo")
        self.assertTrue(b._message_mentions_bot("hey @b3mo look"))
        self.assertTrue(b._message_mentions_bot("hey @B3MO look"))  # case-insens
        self.assertFalse(b._message_mentions_bot("hey @claude look"))

    def test_claude_bot_mention_behaviour_unchanged(self) -> None:
        b = _bridge("claude")
        self.assertTrue(b._message_mentions_bot("@claude hi"))
        self.assertTrue(b._message_mentions_bot("@Claude hi"))
        self.assertFalse(b._message_mentions_bot("no mention here"))

    def test_command_mentions_is_runtime_handle(self) -> None:
        self.assertEqual(_bridge("b3mo")._command_mentions(), ("b3mo",))
        self.assertEqual(_bridge("claude")._command_mentions(), ("claude",))


class BotNameWelcomeTests(unittest.TestCase):
    def test_welcome_hint_uses_runtime_handle(self) -> None:
        b = _bridge("b3mo")
        cfg = purpose.PurposeConfig(backend="claude", model=None)
        text = b._format_welcome(cfg, "/tmp/proj")
        self.assertIn("@b3mo catch up", text)
        self.assertNotIn("@claude", text)


if __name__ == "__main__":
    unittest.main()
