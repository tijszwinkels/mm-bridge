"""CLI subcommand tests — invite / channel / serve dispatch."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config


@dataclass
class FakeMM:
    users_by_username: dict = field(default_factory=dict)
    invited: list = field(default_factory=list)
    logged_in: bool = False
    missing_users: set = field(default_factory=set)
    invite_failures: set = field(default_factory=set)

    def login(self) -> None:
        self.logged_in = True

    def get_user_by_username(self, username: str) -> dict:
        if username in self.missing_users:
            raise RuntimeError(f"no such user: {username}")
        return self.users_by_username[username]

    def invite_user(self, channel_id: str, user_id: str) -> None:
        if user_id in self.invite_failures:
            raise RuntimeError(f"invite failed for {user_id}")
        self.invited.append((channel_id, user_id))


class InviteHelperTests(unittest.TestCase):
    """`cli._invite_to_channel` — mockable core of the invite subcommand."""

    def test_resolves_username_and_calls_invite(self) -> None:
        mm = FakeMM(users_by_username={"tijs": {"id": "u-tijs"}})
        cli._invite_to_channel(mm, "c1", "tijs")
        self.assertEqual(mm.invited, [("c1", "u-tijs")])

    def test_strips_at_prefix(self) -> None:
        mm = FakeMM(users_by_username={"tijs": {"id": "u-tijs"}})
        cli._invite_to_channel(mm, "c1", "@tijs")
        self.assertEqual(mm.invited, [("c1", "u-tijs")])

    def test_unknown_user_raises(self) -> None:
        mm = FakeMM(missing_users={"nobody"})
        with self.assertRaises(RuntimeError):
            cli._invite_to_channel(mm, "c1", "nobody")


class SidecarLookupTests(unittest.TestCase):
    """`cli._resolve_channel_from_session` — session_id → channel_id."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_channel_id_from_sidecar(self) -> None:
        sidecar.write(self.sdir, "sess-1", "chan-42")
        self.assertEqual(
            cli._resolve_channel_from_session(self.sdir, "sess-1"),
            "chan-42",
        )

    def test_raises_when_sidecar_missing(self) -> None:
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_channel_from_session(self.sdir, "sess-unknown")

    def test_raises_when_sidecar_empty(self) -> None:
        self.sdir.mkdir(parents=True)
        (self.sdir / "sess-empty").write_text("")
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_channel_from_session(self.sdir, "sess-empty")


class BareInvocationTests(unittest.TestCase):
    """`mm-bridge` with no subcommand prints help and exits 1."""

    def test_bare_invocation_exits_1(self) -> None:
        with patch("sys.argv", ["mm-bridge"]):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 1)


class InviteCommandTests(unittest.TestCase):
    """End-to-end dispatch of `mm-bridge invite <username>` with mocked MM."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        sidecar.write(self.sdir, "my-session", "my-channel")

        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
        )
        self.fake_mm = FakeMM(users_by_username={"tijs": {"id": "u-tijs"}})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_invite_dispatch_calls_mm_with_resolved_ids(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "tijs"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "my-session"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertTrue(self.fake_mm.logged_in)
        self.assertEqual(self.fake_mm.invited, [("my-channel", "u-tijs")])

    def test_invite_without_session_env_exits_nonzero(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "tijs"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_invite_without_sidecar_exits_nonzero(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "tijs"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "unknown-sess"}):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertNotEqual(cm.exception.code, 0)


class ChannelCommandTests(unittest.TestCase):
    """`mm-bridge channel` prints the channel_id for the current session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        sidecar.write(self.sdir, "my-session", "my-channel")
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_channel_prints_channel_id(self) -> None:
        import io
        buf = io.StringIO()
        with patch("sys.argv", ["mm-bridge", "channel"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "my-session"}), \
             patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "my-channel")


if __name__ == "__main__":
    unittest.main()
