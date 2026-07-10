"""Cross-layer contract for self-post suppression.

The CLI stamps `from_bridge_cli_session`; the bridge compares it against the
session its anchor mapping stores. Those two ids MUST live in the same
namespace. A claude sub-session looks itself up by the dashed
`CLAUDE_SESSION_ID` UUID, but the harness (and thus the bridge mapping) uses
the canonical `ses_<32hex>` id — the dashed UUID is only a symlink alias.

The unit matrices in test_bridge.py used self-consistent fake ids ("s1"=="s1"),
so a cross-namespace mismatch never showed up there — it only surfaced in the
2026-07-10 live smoke (post fwb1oh…, CLAUDE UUID stamped vs ses_ mapped, so the
suppression never fired and the milestone looped back). This test runs the real
CLI stamping THROUGH the real bridge predicate so that class of bug is caught.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Anchor

from doubles import make_bridge
from test_cli_post import FakeMM


class SelfPostContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli_post(self, argv: list[str], *, session_id: str) -> dict:
        """Run `mm-bridge post` as a claude session; return the authored post."""
        from mm_bridge.config import Config
        cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            allowed_attachment_roots=[self.tmp.name],
        )
        mm = FakeMM()
        env = {"CLAUDE_SESSION_ID": session_id}
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=mm), \
             patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()), \
             patch("sys.stdin", io.StringIO("")), \
             patch.dict("os.environ", env, clear=False) as osenv:
            osenv.pop("MM_BRIDGE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            assert cm.exception.code == 0, "CLI post should succeed"
        return mm.posted[0]

    async def test_default_self_post_suppressed_end_to_end(self):
        # Harness wrote the canonical ses_ sidecar (+ dashed-UUID alias symlink),
        # exactly as production does for a claude session.
        harness_id = "ses_00112233445566778899aabbccddeeff"
        dashed = "00112233-4455-6677-8899-aabbccddeeff"
        sidecar.write(self.sdir, harness_id, "self-chan")
        self.assertTrue((self.sdir / dashed).is_symlink(), "alias fixture missing")

        # CLI: the claude session posts a default milestone into its own channel.
        post = self._cli_post(
            ["mm-bridge", "post", "MILESTONE: step done"], session_id=dashed,
        )
        # The stamped session id must be the HARNESS id, not the dashed UUID.
        self.assertEqual(post["props"]["from_bridge_cli_session"], harness_id)
        self.assertEqual(post["props"]["from_bridge_cli_target"], "self")

        # Bridge: the channel maps to the HARNESS id. Feed the CLI's own post in.
        bridge = make_bridge(self.tmp.name, echoing=False)
        bridge.mapping.link(Anchor("self-chan"), harness_id)
        await bridge._on_mm_posted({
            "id": "p-cli",
            "channel_id": post["channel_id"],
            "message": post["message"],
            "user_id": bridge.mm.bot_user_id,
            "type": "",
            "props": post["props"],
        })

        self.assertEqual(
            bridge.vd.sent, [],
            "a session's own default post must be suppressed end-to-end",
        )

    async def test_cross_session_post_still_forwarded_end_to_end(self):
        """Guard the other direction: an explicit `--channel` agentcom post
        from one session into another's channel still forwards."""
        sender_harness = "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sender_dashed = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        sidecar.write(self.sdir, sender_harness, "sender-chan")

        post = self._cli_post(
            ["mm-bridge", "post", "--channel", "recipient-chan", "ping"],
            session_id=sender_dashed,
        )
        self.assertEqual(post["props"]["from_bridge_cli_target"], "explicit")

        bridge = make_bridge(self.tmp.name, echoing=False)
        bridge.mapping.link(Anchor("recipient-chan"), "ses_recipient")
        await bridge._on_mm_posted({
            "id": "p-cli",
            "channel_id": "recipient-chan",
            "message": post["message"],
            "user_id": bridge.mm.bot_user_id,
            "type": "",
            "props": post["props"],
        })

        self.assertEqual(bridge.vd.sent, [("ses_recipient", "ping")])
