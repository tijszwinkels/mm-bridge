"""Tests for `mm-bridge post`."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config


@dataclass
class FakeMM:
    posted: list = field(default_factory=list)
    uploaded: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    logged_in: bool = False
    login_raises: Exception | None = None
    upload_raises: Exception | None = None
    post_raises: Exception | None = None
    get_channel_raises: Exception | None = None
    max_file_size: int = 50 * 1024 * 1024
    next_file_id_counter: int = 0
    next_post_id_counter: int = 0

    def login(self) -> None:
        if self.login_raises:
            raise self.login_raises
        self.logged_in = True

    def upload_file(self, channel_id: str, path: Path) -> str:
        if self.upload_raises:
            raise self.upload_raises
        self.next_file_id_counter += 1
        fid = f"f-{self.next_file_id_counter}"
        self.uploaded.append((channel_id, str(path), fid))
        return fid

    def get_max_file_size(self) -> int:
        return self.max_file_size

    def get_channel(self, channel_id: str) -> dict:
        if self.get_channel_raises:
            raise self.get_channel_raises
        return self.channels.get(channel_id, {"id": channel_id, "name": ""})

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list | None = None,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        if self.post_raises:
            raise self.post_raises
        self.next_post_id_counter += 1
        pid = f"post-{self.next_post_id_counter}"
        self.posted.append({
            "channel_id": channel_id,
            "message": message,
            "file_ids": list(file_ids) if file_ids else [],
            "root_id": root_id,
            "props": dict(props) if props else None,
        })
        return {"id": pid}


class PostCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            allowed_attachment_roots=[self.tmp.name],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(
        self,
        fake_mm: FakeMM,
        argv: list[str],
        *,
        session_id: str | None = "my-sess",
        stdin: str = "",
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {}
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("sys.stdin", io.StringIO(stdin)), \
             patch.dict("os.environ", env, clear=False) as osenv:
            if session_id is None:
                osenv.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    # ---------- channel resolution ----------

    def test_explicit_channel_wins(self) -> None:
        mm = FakeMM()
        rc, out, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "explicit-chan", "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "post-1")
        self.assertEqual(mm.posted[0]["channel_id"], "explicit-chan")
        self.assertEqual(mm.posted[0]["message"], "hello")
        self.assertIsNone(mm.posted[0]["root_id"])

    def test_falls_back_to_session_sidecar(self) -> None:
        sidecar.write(self.sdir, "my-sess", "sidecar-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "sidecar-chan")
        self.assertIsNone(mm.posted[0]["root_id"])
        # Same-channel posts carry the marker AND the sender's own channel
        # id so the daemon recognises this as an own-channel echo and
        # drops it. This is the exact path that was looping
        # `mm-bridge post "MILESTONE: ..."` calls back into the author's
        # own session as a delayed user turn.
        self.assertEqual(
            mm.posted[0]["props"],
            {
                "from_bridge_cli": "post",
                "from_bridge_cli_channel": "sidecar-chan",
            },
        )

    def test_no_channel_and_no_sidecar_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "hi"], session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("channel", err.lower())

    # ---------- thread resolution ----------

    def test_thread_forked_sidecar_posts_to_thread_by_default(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["root_id"], "root-9")

    def test_no_thread_overrides_sidecar_root(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--no-thread", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(mm.posted[0]["root_id"])

    def test_thread_arg_overrides_sidecar(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--thread", "other", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["root_id"], "other")

    def test_thread_arg_with_channel_arg(self) -> None:
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "--thread", "r1", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "c1")
        self.assertEqual(mm.posted[0]["root_id"], "r1")

    # ---------- message / stdin ----------

    def test_stdin_message_with_dash(self) -> None:
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "-"],
            session_id=None,
            stdin="piped body\n",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["message"], "piped body")

    def test_empty_body_no_file_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "   "],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("empty", err.lower())

    def test_empty_body_with_file_ok(self) -> None:
        f = Path(self.tmp.name) / "attach.txt"
        f.write_text("hello")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "--file", str(f), ""],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["message"], "")
        self.assertEqual(mm.posted[0]["file_ids"], ["f-1"])

    # ---------- attachments ----------

    def test_file_uploaded_and_post_gets_file_id(self) -> None:
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.uploaded), 1)
        self.assertEqual(mm.uploaded[0][0], "c1")
        self.assertEqual(mm.posted[0]["file_ids"], ["f-1"])

    def test_relative_file_resolves_from_current_working_directory(self) -> None:
        f = Path(self.tmp.name) / "relative.txt"
        f.write_text("x")
        mm = FakeMM()
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            rc, _, _ = self._invoke(
                mm,
                ["mm-bridge", "post", "--channel", "c1",
                 "--file", "relative.txt", "hi"],
                session_id=None,
            )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.uploaded), 1)
        self.assertEqual(Path(mm.uploaded[0][1]), f)

    def test_more_than_10_files_exits_2_without_upload(self) -> None:
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        files_args = []
        for _ in range(11):
            files_args += ["--file", str(f)]
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", *files_args, "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(mm.uploaded, [])
        self.assertEqual(mm.posted, [])
        self.assertIn("10", err)

    def test_file_outside_allowed_roots_exits_2(self) -> None:
        # Force a root that excludes the temp dir.
        self.cfg.allowed_attachment_roots = ["/nonexistent/root"]
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("allowed_attachment_roots", err)
        self.assertEqual(mm.uploaded, [])
        self.assertEqual(mm.posted, [])

    def test_missing_file_exits_3(self) -> None:
        missing = Path(self.tmp.name) / "does-not-exist.txt"
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(missing), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 3)
        self.assertEqual(mm.posted, [])

    def test_file_too_big_exits_3(self) -> None:
        f = Path(self.tmp.name) / "big.bin"
        f.write_bytes(b"0" * 100)
        mm = FakeMM(max_file_size=50)
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 3)
        self.assertEqual(mm.posted, [])

    # ---------- errors ----------

    def test_missing_bot_token_exits_1(self) -> None:
        self.cfg.mm_bot_token = ""
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "c1", "hi"], session_id=None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("MM_BOT_TOKEN", err)

    def test_login_failure_exits_3(self) -> None:
        mm = FakeMM(login_raises=RuntimeError("boom"))
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "c1", "hi"], session_id=None,
        )
        self.assertEqual(rc, 3)

    def test_mutually_exclusive_thread_flags(self) -> None:
        mm = FakeMM()
        # argparse itself enforces this; exit code from argparse is 2.
        with patch("sys.argv", [
            "mm-bridge", "post", "--channel", "c1",
            "--thread", "r", "--no-thread", "hi",
        ]), patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=mm), \
             patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()


class CrossChannelMirrorTests(unittest.TestCase):
    """`mm-bridge post --channel <other>` mirrors body in sender's channel."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            allowed_attachment_roots=[self.tmp.name],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(
        self,
        fake_mm: FakeMM,
        argv: list[str],
        *,
        session_id: str | None = "my-sess",
        stdin: str = "",
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {}
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("sys.stdin", io.StringIO(stdin)), \
             patch.dict("os.environ", env, clear=False) as osenv:
            if session_id is None:
                osenv.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    def test_cross_channel_post_creates_mirror_in_self_channel(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"other-chan": {"id": "other-chan",
                                              "name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)

        original, mirror = mm.posted[0], mm.posted[1]
        self.assertEqual(original["channel_id"], "other-chan")
        self.assertEqual(original["message"], "hello")

        self.assertEqual(mirror["channel_id"], "self-chan")
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-slug~_",
        )
        # The mirror is the sender's own-channel echo: marker + the
        # sender's channel id (which equals the channel the mirror lands
        # in) → daemon drops it on the sender's own session.
        self.assertEqual(
            mirror["props"],
            {
                "from_bridge_cli": "cross-post-mirror",
                "from_bridge_cli_channel": "self-chan",
            },
        )
        self.assertEqual(mirror["file_ids"], [])
        self.assertIsNone(mirror["root_id"])

    def test_cross_channel_original_carries_sender_channel_id(self) -> None:
        """Cross-channel agentcom: when ``--channel <other>`` is given
        from inside a bridge session, the original post carries the
        marker AND the SENDER's own channel id. Because the post lands
        in `<other>` (which is NOT the sender's channel), the daemon's
        channel-scoped predicate forwards the post to the recipient
        session as a user turn. This is the regression that broke
        claude/codex cross-channel agentcom in PR #8."""
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "other-chan")
        self.assertEqual(
            mm.posted[0]["props"],
            {
                "from_bridge_cli": "post",
                "from_bridge_cli_channel": "self-chan",
            },
        )

    def test_post_without_session_omits_marker(self) -> None:
        """A `mm-bridge post --channel <X>` call from a shell that is
        NOT inside a bridge session has no own-channel echo to
        suppress, so the CLI must omit the marker entirely. The daemon
        then forwards the post normally to whatever session is linked
        to <X>."""
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "explicit-chan", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(mm.posted[0]["props"])

    def test_mirror_falls_back_to_channel_id_when_get_channel_raises(
        self,
    ) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(get_channel_raises=RuntimeError("boom"))
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        mirror = mm.posted[1]
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-chan~_",
        )

    def test_mirror_includes_attachment_count_but_no_file_ids(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        f1 = Path(self.tmp.name) / "a.txt"
        f1.write_text("x")
        f2 = Path(self.tmp.name) / "b.txt"
        f2.write_text("y")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm,
            [
                "mm-bridge", "post", "--channel", "other-chan",
                "--file", str(f1), "--file", str(f2), "hello",
            ],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        original, mirror = mm.posted[0], mm.posted[1]
        self.assertEqual(original["file_ids"], ["f-1", "f-2"])
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-slug~ with 2 attachment(s)_",
        )
        self.assertEqual(mirror["file_ids"], [])
        # No re-upload: still only 2 uploads total.
        self.assertEqual(len(mm.uploaded), 2)

    def test_no_mirror_when_channel_equals_self_id(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"self-chan": {"name": "self-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "self-chan", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)

    def test_no_mirror_when_no_channel_flag(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)
        self.assertEqual(mm.posted[0]["channel_id"], "self-chan")

    def test_mirror_lands_in_senders_thread_when_session_is_thread_forked(
        self,
    ) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan", "self-root")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        original, mirror = mm.posted[0], mm.posted[1]
        # The cross-channel original is unaffected by the sender's own
        # thread — it goes to other-chan at channel level (no --thread).
        self.assertEqual(original["channel_id"], "other-chan")
        self.assertIsNone(original["root_id"])
        # The mirror lands inside the sender's own thread so it shows up
        # in the same scrollback the human is watching.
        self.assertEqual(mirror["channel_id"], "self-chan")
        self.assertEqual(mirror["root_id"], "self-root")

    def test_no_mirror_when_no_sidecar(self) -> None:
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "other-chan", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)
        self.assertEqual(mm.posted[0]["channel_id"], "other-chan")


if __name__ == "__main__":
    unittest.main()
