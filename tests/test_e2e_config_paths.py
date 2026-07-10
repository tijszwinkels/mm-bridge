"""End-to-end config-path checks for the drop-first-message-config work.

Drives the REAL :class:`~mm_bridge.bridge.Bridge` through the active
:class:`~doubles.EventEchoingMattermostClient` (which echoes every Channel
Purpose write back as a ``channel_updated`` event, exactly like the live MM
WS loop) plus an echo-harness double that mirrors the live claude-code
backend (empty model catalog). Covers the four scenarios verified by hand
during the change, now as a committed regression module:

  A. welcome text advertises dot-commands + Channel Purpose, not "first message"
  B. a config-looking first message is forwarded verbatim, never intercepted
  C. `.model` restart is quiet and posts no duplicate purpose notice
  D. `.backend` restart drops the carried model and is quiet
"""
from __future__ import annotations

import tempfile
import unittest

from mm_bridge.bridge import INVITE_PLACEHOLDER
from mm_bridge.config import Anchor
from mm_bridge.purpose import PurposeConfig

from doubles import make_bridge


class E2EConfigPathsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # type: ignore[override]
        self.tmp = tempfile.TemporaryDirectory()
        # Echoing MM double + empty model catalog (mirrors live claude-code).
        self.bridge = make_bridge(self.tmp.name)

    async def asyncTearDown(self):  # type: ignore[override]
        self.tmp.cleanup()

    def _notices(self) -> list:
        return [
            p for p in self.bridge.mm.posted
            if "takes effect only for new sessions" in p.message
        ]

    def _sent(self) -> list[str]:
        return [m for (_sid, m) in self.bridge.harness.sent]

    async def test_A_welcome_text(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        welcomes = [
            p for p in self.bridge.mm.posted
            if p.props and p.props.get("from_bridge") == "welcome"
        ]
        self.assertEqual(len(welcomes), 1)
        join = welcomes[0].message
        start = next(
            (p.message for p in self.bridge.mm.posted if "Session started" in p.message), "",
        )
        self.assertIn(".model", join)
        self.assertIn(".backend", join)
        self.assertIn("Channel Purpose", join)
        self.assertNotIn("first message", join)
        self.assertIn(".model", start)
        self.assertIn(".backend", start)
        self.assertNotIn("First message", start)

    async def test_B_first_message_forwarded_verbatim(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        sid = self.bridge.mapping.get_session(Anchor("c1"))
        self.bridge.harness.sent.clear()
        self.bridge.harness.created.clear()
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "claude, sonnet", "user_id": "u1", "type": "",
        })

        fwd = self._sent()
        self.assertEqual(len(fwd), 1)
        self.assertTrue(fwd[0].endswith("claude, sonnet"))
        self.assertTrue(any("Running inside Mattermost" in m for m in fwd))
        self.assertEqual(self.bridge.harness.created, [])
        self.assertFalse(any("Config applied" in p.message for p in self.bridge.mm.posted))
        self.assertNotIn("c1", self.bridge._awaiting_first_forward)
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), sid)

    async def test_C_dot_model_quiet_no_dup_notice(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus", "display_name": "Chan"}
        self.bridge.last_channel_state["c1"] = {"display_name": "Chan", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet", "user_id": "u1", "type": "",
        })
        await self.bridge.mm.deliver_ws_events(self.bridge)

        confs = [p for p in self.bridge.mm.posted if "Model set to" in p.message]
        self.assertTrue(self.bridge.harness.created)
        self.assertEqual(self.bridge.harness.created[-1]["model"], "claude-sonnet")
        self.assertEqual(len(confs), 1)
        self.assertNotIn(INVITE_PLACEHOLDER, self._sent())
        self.assertEqual(self._notices(), [])

    async def test_D_dot_backend_drops_model_quiet(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus", "display_name": "Chan"}
        self.bridge.last_channel_state["c1"] = {"display_name": "Chan", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex", "user_id": "u1", "type": "",
        })
        await self.bridge.mm.deliver_ws_events(self.bridge)

        created = self.bridge.harness.created[-1] if self.bridge.harness.created else {}
        confs = [p for p in self.bridge.mm.posted if "Backend set to" in p.message]
        self.assertEqual(created.get("backend"), "codex")
        self.assertEqual(created.get("model"), "gpt-5.5")  # carried opus dropped → codex default
        self.assertIn("codex", self.bridge.mm.channels["c1"]["purpose"])
        self.assertNotIn("opus", self.bridge.mm.channels["c1"]["purpose"])
        self.assertEqual(len(confs), 1)
        self.assertNotIn(INVITE_PLACEHOLDER, self._sent())
        self.assertEqual(self._notices(), [])

    async def test_D_unknown_backend_rejected_inline(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend frobnicate", "user_id": "u1", "type": "",
        })
        self.assertEqual(self.bridge.harness.created, [])
        self.assertTrue(any("Unknown backend" in p.message for p in self.bridge.mm.posted))
