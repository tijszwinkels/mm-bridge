"""Tests for `mm-bridge post`."""

from __future__ import annotations

import io
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
    logged_in: bool = False
    login_raises: Exception | None = None
    upload_raises: Exception | None = None
    post_raises: Exception | None = None
    max_file_size: int = 50 * 1024 * 1024
    next_post_id: str = "post-1"
    next_file_id_counter: int = 0

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

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list | None = None,
        root_id: str | None = None,
    ) -> dict:
        if self.post_raises:
            raise self.post_raises
        self.posted.append({
            "channel_id": channel_id,
            "message": message,
            "file_ids": list(file_ids) if file_ids else [],
            "root_id": root_id,
        })
        return {"id": self.next_post_id}


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


if __name__ == "__main__":
    unittest.main()
