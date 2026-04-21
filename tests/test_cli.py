"""CLI subcommand tests — invite / channel / serve / spawn dispatch."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Anchor, Config


@dataclass
class FakeMM:
    users_by_username: dict = field(default_factory=dict)
    channels_by_id: dict = field(default_factory=dict)
    invited: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    renames: list = field(default_factory=list)
    headers: list = field(default_factory=list)
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

    def get_channel(self, channel_id: str) -> dict:
        return self.channels_by_id[channel_id]

    def post_message(self, channel_id: str, message: str) -> dict:
        self.posts.append(
            {"channel_id": channel_id, "message": message, "root_id": None},
        )
        return {"id": f"post-{len(self.posts)}"}

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list | None = None,
        root_id: str | None = None,
    ) -> dict:
        self.posts.append(
            {"channel_id": channel_id, "message": message, "root_id": root_id},
        )
        return {"id": f"post-{len(self.posts)}"}

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self.renames.append((channel_id, display_name))

    def set_channel_header(self, channel_id: str, header: str) -> None:
        self.headers.append((channel_id, header))


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


class AnchorLookupTests(unittest.TestCase):
    """`cli._resolve_anchor_from_session` — session_id → Anchor."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_single_line_sidecar_resolves_to_channel_anchor(self) -> None:
        sidecar.write(self.sdir, "sess-1", "chan-42")
        self.assertEqual(
            cli._resolve_anchor_from_session(self.sdir, "sess-1"),
            Anchor("chan-42"),
        )

    def test_two_line_sidecar_resolves_to_thread_anchor(self) -> None:
        sidecar.write(self.sdir, "sess-fork", "chan-42", "root-7")
        self.assertEqual(
            cli._resolve_anchor_from_session(self.sdir, "sess-fork"),
            Anchor("chan-42", "root-7"),
        )

    def test_raises_when_sidecar_missing(self) -> None:
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_anchor_from_session(self.sdir, "sess-unknown")

    def test_raises_when_sidecar_empty(self) -> None:
        self.sdir.mkdir(parents=True)
        (self.sdir / "sess-empty").write_text("")
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_anchor_from_session(self.sdir, "sess-empty")


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

    def test_invite_from_thread_fork_session_invites_to_channel(self) -> None:
        """The bug the anchor refactor fixes: inviting from a thread-fork
        session must succeed and invite to the fork's channel."""
        sidecar.write(self.sdir, "fork-sess", "fork-chan", "root-9")
        with patch("sys.argv", ["mm-bridge", "invite", "tijs"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "fork-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.fake_mm.invited, [("fork-chan", "u-tijs")])


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

    def test_channel_from_thread_fork_session_prints_channel_id(self) -> None:
        """Thread-fork sessions must self-identify too (was broken before)."""
        import io
        sidecar.write(self.sdir, "fork-sess", "fork-chan", "root-9")
        buf = io.StringIO()
        with patch("sys.argv", ["mm-bridge", "channel"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "fork-sess"}), \
             patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "fork-chan")


class WaitForNewSidecarTests(unittest.TestCase):
    """`_wait_for_new_sidecar` — the polling helper used by `cmd_spawn`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.sdir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_clock(self, ticks: list[float]):
        """Return a clock() that yields successive values from `ticks`."""
        it = iter(ticks)
        return lambda: next(it)

    def test_returns_new_sidecar_on_first_poll(self) -> None:
        sidecar.write(self.sdir, "new-sess", "new-chan")
        sid, cid = cli._wait_for_new_sidecar(
            self.sdir, before=set(),
            timeout=1.0, interval=0.01,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        )
        self.assertEqual(sid, "new-sess")
        self.assertEqual(cid, "new-chan")

    def test_ignores_sidecars_present_in_before(self) -> None:
        sidecar.write(self.sdir, "existing", "existing-chan")
        sidecar.write(self.sdir, "fresh", "fresh-chan")
        sid, cid = cli._wait_for_new_sidecar(
            self.sdir, before={"existing"},
            timeout=1.0, interval=0.01,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        )
        self.assertEqual(sid, "fresh")
        self.assertEqual(cid, "fresh-chan")

    def test_timeout_when_no_new_sidecar(self) -> None:
        # Clock advances past deadline immediately after the first poll.
        clock = self._fake_clock([0.0, 0.0, 2.0])  # start, loop 1, loop 2
        with self.assertRaises(TimeoutError):
            cli._wait_for_new_sidecar(
                self.sdir, before=set(),
                timeout=1.0, interval=0.01,
                clock=clock,
                sleep=lambda _: None,
            )

    def test_empty_sidecar_raises(self) -> None:
        (self.sdir / "bad-sess").write_text("")
        with self.assertRaises(RuntimeError):
            cli._wait_for_new_sidecar(
                self.sdir, before=set(),
                timeout=1.0, interval=0.01,
                clock=lambda: 0.0,
                sleep=lambda _: None,
            )

    def test_missing_dir_times_out(self) -> None:
        missing = Path(self.tmp.name) / "does-not-exist"
        clock = self._fake_clock([0.0, 0.0, 2.0])
        with self.assertRaises(TimeoutError):
            cli._wait_for_new_sidecar(
                missing, before=set(),
                timeout=1.0, interval=0.01,
                clock=clock,
                sleep=lambda _: None,
            )


class SpawnCommandTests(unittest.TestCase):
    """End-to-end dispatch of `mm-bridge spawn` with mocked MM/VD/sidecar."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        # Parent session's sidecar.
        sidecar.write(self.sdir, "parent-sess", "parent-chan")

        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            vd_url="http://vd.invalid",
            default_cwd="/tmp",
            default_backend="claude",
        )

        self.fake_mm = FakeMM(
            users_by_username={"alice": {"id": "u-alice"}},
            channels_by_id={
                "parent-chan": {"id": "parent-chan", "name": "parent-slug"},
                "new-chan": {
                    "id": "new-chan", "name": "s-abc",
                    "display_name": "Auto-derived Title",
                },
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _simulate_daemon_creates_sidecar(
        self, sess_id: str = "new-sess", chan_id: str = "new-chan",
    ):
        """Return an async stub that mimics VD → daemon → sidecar appearance."""
        async def _stub(vd_url, message, cwd, backend):
            sidecar.write(self.sdir, sess_id, chan_id)
            return {"status": "started"}
        return _stub

    def _invoke(self, argv):
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "parent-sess"},
             ), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch(
                 "mm_bridge.cli._vd_create_session",
                 side_effect=self._simulate_daemon_creates_sidecar(),
             ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code

    def test_spawn_happy_path(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "fix the bug",
            "--title", "Bug Fix", "--cwd", "/repo", "--backend", "claude",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(self.fake_mm.logged_in)
        # Rename + header applied to the new channel.
        self.assertEqual(
            self.fake_mm.renames, [("new-chan", "Bug Fix")],
        )
        self.assertEqual(
            self.fake_mm.headers, [("new-chan", "Parent: ~parent-slug~")],
        )
        # Two posts: kickoff in sub-channel, announcement in parent.
        self.assertEqual(len(self.fake_mm.posts), 2)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("new-chan", posts_by_chan)
        self.assertIn("parent-chan", posts_by_chan)
        sub_body = posts_by_chan["new-chan"]["message"]
        parent_body = posts_by_chan["parent-chan"]["message"]
        self.assertIn("Spawned from ~parent-slug~", sub_body)
        self.assertIn("> fix the bug", sub_body)
        self.assertIn("Spawned **Bug Fix** in ~s-abc~", parent_body)
        self.assertIn("> fix the bug", parent_body)
        # Spawning from a channel-level session: announcement is not threaded.
        self.assertIsNone(posts_by_chan["parent-chan"]["root_id"])

    def test_spawn_without_title_does_not_rename(self) -> None:
        rc = self._invoke(["mm-bridge", "spawn", "ad hoc"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.renames, [])
        # Header still set.
        self.assertEqual(len(self.fake_mm.headers), 1)
        # Parent post uses daemon-derived display_name.
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn(
            "**Auto-derived Title**", posts_by_chan["parent-chan"]["message"],
        )

    def test_spawn_no_forward_prompt_skips_all_posts(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "quiet one", "--no-forward-prompt",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.posts, [])

    def test_spawn_with_invite_adds_user_to_new_channel(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "need help", "--invite", "alice",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.invited, [("new-chan", "u-alice")])

    def test_spawn_without_session_env_exits_2(self) -> None:
        with patch("sys.argv", ["mm-bridge", "spawn", "x"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)

    def test_spawn_from_thread_fork_creates_sibling_channel(self) -> None:
        """Spawning from a thread-fork session must succeed. The sub-session
        lives in a fresh sibling channel (not nested thread) and the parent
        announcement goes into the fork's thread (not the channel root)."""
        sidecar.write(self.sdir, "fork-sess", "parent-chan", "root-9")
        with patch("sys.argv", ["mm-bridge", "spawn", "carry on"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "fork-sess"},
             ), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch(
                 "mm_bridge.cli._vd_create_session",
                 side_effect=self._simulate_daemon_creates_sidecar(),
             ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("new-chan", posts_by_chan)
        self.assertIn("parent-chan", posts_by_chan)
        # Kickoff in the sub-channel is never threaded.
        self.assertIsNone(posts_by_chan["new-chan"]["root_id"])
        # Announcement lands in the parent thread, not at the channel root.
        self.assertEqual(posts_by_chan["parent-chan"]["root_id"], "root-9")

    def test_spawn_vd_failure_exits_3(self) -> None:
        async def _boom(vd_url, message, cwd, backend):
            raise RuntimeError("VD down")
        with patch("sys.argv", ["mm-bridge", "spawn", "x"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "parent-sess"},
             ), \
             patch(
                 "mm_bridge.cli._make_mm_client", return_value=self.fake_mm,
             ), \
             patch("mm_bridge.cli._vd_create_session", side_effect=_boom):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
