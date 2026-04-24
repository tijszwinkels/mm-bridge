"""Tests for `mm-bridge read`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config


def _mk_post(
    pid: str, *,
    create_at: int,
    user_id: str = "u1",
    message: str = "",
    type: str = "",
    root_id: str = "",
    file_ids: list[str] | None = None,
) -> dict:
    return {
        "id": pid,
        "create_at": create_at,
        "user_id": user_id,
        "message": message,
        "type": type,
        "root_id": root_id,
        "file_ids": file_ids or [],
    }


@dataclass
class FakeMM:
    bot_user_id: str = "bot-user"
    posts_by_channel: dict = field(default_factory=dict)
    thread_posts: dict = field(default_factory=dict)
    since_posts: dict = field(default_factory=dict)  # (cid, since) -> list
    users: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    logged_in: bool = False
    calls: list = field(default_factory=list)

    def login(self) -> None:
        self.logged_in = True

    def get_posts(self, channel_id: str, limit: int) -> list[dict]:
        self.calls.append(("get_posts", channel_id, limit))
        return list(self.posts_by_channel.get(channel_id, []))[:limit]

    def get_posts_since(
        self, channel_id: str, since_ms: int, per_page: int = 200,
    ) -> list[dict]:
        self.calls.append(("get_posts_since", channel_id, since_ms))
        return list(self.since_posts.get(channel_id, []))

    def get_thread_posts(self, root_id: str) -> list[dict]:
        self.calls.append(("get_thread_posts", root_id))
        return list(self.thread_posts.get(root_id, []))

    def get_user(self, user_id: str) -> dict:
        if user_id in self.users:
            return self.users[user_id]
        raise RuntimeError(f"no such user {user_id}")

    def get_file_info(self, file_id: str) -> dict:
        return self.files.get(file_id, {"id": file_id})


class ParseSinceTests(unittest.TestCase):
    def test_relative_hours(self) -> None:
        now = 10_000_000
        self.assertEqual(
            cli._parse_since("2h", now), now - 2 * 3600 * 1000,
        )

    def test_relative_minutes(self) -> None:
        now = 10_000_000
        self.assertEqual(
            cli._parse_since("30m", now), now - 30 * 60 * 1000,
        )

    def test_relative_days(self) -> None:
        now = 10_000_000
        self.assertEqual(
            cli._parse_since("1d", now), now - 86_400_000,
        )

    def test_iso_8601_utc(self) -> None:
        from datetime import datetime, timezone
        want = int(
            datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(cli._parse_since("2026-04-22T10:00:00Z", 0), want)

    def test_iso_8601_naive_is_utc(self) -> None:
        from datetime import datetime, timezone
        want = int(
            datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(cli._parse_since("2026-04-22T10:00:00", 0), want)

    def test_bare_digits_parsed_as_ms_epoch(self) -> None:
        self.assertEqual(cli._parse_since("1776808800000", 999), 1776808800000)

    def test_bad_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            cli._parse_since("nope", 0)


class ReadCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            catch_up_max_n=500,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(
        self, fake_mm: FakeMM, argv: list[str], *, session_id: str | None = None,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {}
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch.dict("os.environ", env, clear=False) as osenv:
            if session_id is None:
                osenv.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    # ---------- channel resolution ----------

    def test_no_channel_and_no_sidecar_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(mm, ["mm-bridge", "read"])
        self.assertEqual(rc, 2)
        self.assertIn("channel", err.lower())

    def test_missing_bot_token_exits_1(self) -> None:
        self.cfg.mm_bot_token = ""
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 1)
        self.assertIn("MM_BOT_TOKEN", err)

    # ---------- api selection ----------

    def test_thread_forked_sidecar_calls_get_thread_posts(self) -> None:
        sidecar.write(self.sdir, "sess", "chan", "root-9")
        mm = FakeMM()
        mm.thread_posts["root-9"] = [_mk_post(
            "p1", create_at=100, user_id="u1", message="hi",
        )]
        mm.users["u1"] = {"username": "alice"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read"], session_id="sess",
        )
        self.assertEqual(rc, 0)
        calls = [c for c in mm.calls if c[0] != "get_user"]
        self.assertEqual(calls[0][0], "get_thread_posts")
        self.assertEqual(calls[0][1], "root-9")

    def test_no_thread_with_thread_sidecar_calls_get_posts(self) -> None:
        sidecar.write(self.sdir, "sess", "chan", "root-9")
        mm = FakeMM()
        mm.posts_by_channel["chan"] = [_mk_post(
            "p1", create_at=100, user_id="u1", message="hi",
        )]
        mm.users["u1"] = {"username": "alice"}
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "read", "--no-thread"], session_id="sess",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.calls[0][0], "get_posts")

    def test_since_without_thread_uses_get_posts_since(self) -> None:
        mm = FakeMM()
        mm.since_posts["c1"] = [_mk_post(
            "p1", create_at=1_000_000_000_000, user_id="u1", message="x",
        )]
        mm.users["u1"] = {"username": "a"}
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "--since", "2h"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.calls[0][0], "get_posts_since")

    # ---------- filtering / ordering ----------

    def test_system_posts_excluded(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=100, user_id="u1", message="hi"),
            _mk_post("p2", create_at=200, user_id="u1", message="join",
                     type="system_join_channel"),
        ]
        mm.users["u1"] = {"username": "alice"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("hi", out)
        self.assertNotIn("join", out)

    def test_no_bot_excludes_bot_posts(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=100, user_id="u1", message="human"),
            _mk_post("p2", create_at=200, user_id=mm.bot_user_id,
                     message="botmsg"),
        ]
        mm.users["u1"] = {"username": "alice"}
        mm.users[mm.bot_user_id] = {"username": "claude"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "--no-bot"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("human", out)
        self.assertNotIn("botmsg", out)

    def test_output_ordered_oldest_first(self) -> None:
        mm = FakeMM()
        # Server-returned order is arbitrary; oldest should print first.
        mm.posts_by_channel["c1"] = [
            _mk_post("p2", create_at=200, user_id="u1", message="second"),
            _mk_post("p1", create_at=100, user_id="u1", message="first"),
        ]
        mm.users["u1"] = {"username": "alice"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertLess(out.index("first"), out.index("second"))

    # ---------- formats ----------

    def test_text_format_has_timestamp_and_username(self) -> None:
        mm = FakeMM()
        # create_at = 2026-04-22T10:00:00 UTC epoch ms.
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=1776808800000, user_id="u1",
                     message="hello world"),
        ]
        mm.users["u1"] = {"username": "alice"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("alice", out)
        # Year appears in local-time bracket.
        self.assertIn("2026-", out)
        self.assertIn("hello world", out)

    def test_unknown_user_renders_fallback(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=1776808800000, user_id="u-ghost-xyz",
                     message="ping"),
        ]
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("user:", out)

    def test_json_format_shape(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=1776808800000, user_id="u1",
                     message="msg", file_ids=["f1"]),
        ]
        mm.users["u1"] = {"username": "alice"}
        mm.files["f1"] = {
            "id": "f1", "name": "a.txt", "size": 10, "mime_type": "text/plain",
        }
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "--format", "json"],
        )
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        p = data[0]
        self.assertEqual(p["id"], "p1")
        self.assertEqual(p["create_at"], 1776808800000)
        self.assertEqual(p["user_id"], "u1")
        self.assertEqual(p["username"], "alice")
        self.assertIn("is_bot", p)
        self.assertEqual(p["message"], "msg")
        self.assertEqual(len(p["files"]), 1)
        self.assertEqual(p["files"][0]["name"], "a.txt")

    def test_jsonl_format_one_object_per_line(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=100, user_id="u1", message="a"),
            _mk_post("p2", create_at=200, user_id="u1", message="b"),
        ]
        mm.users["u1"] = {"username": "alice"}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "--format", "jsonl"],
        )
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["message"], "a")

    # ---------- misc ----------

    def test_bad_since_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "--since", "bad"],
        )
        self.assertEqual(rc, 2)
        self.assertIn("since", err.lower())

    def test_n_over_cap_silently_clamped(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post(f"p{i}", create_at=i, user_id="u1", message=str(i))
            for i in range(10)
        ]
        mm.users["u1"] = {"username": "a"}
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1", "-n", "5000"],
        )
        self.assertEqual(rc, 0)
        # get_posts is called with the cap (500), not 5000.
        call = [c for c in mm.calls if c[0] == "get_posts"][0]
        self.assertEqual(call[2], 500)

    def test_attachment_line_appears_in_text(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = [
            _mk_post("p1", create_at=1776808800000, user_id="u1",
                     message="", file_ids=["f1"]),
        ]
        mm.users["u1"] = {"username": "alice"}
        mm.files["f1"] = {"name": "notes.md", "size": 12345}
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("notes.md", out)

    def test_thread_filter_applied_on_client_side(self) -> None:
        """`-n` against a thread fetch returns the most recent N."""
        mm = FakeMM()
        # Thread has 5 posts, we want last 3.
        mm.thread_posts["r1"] = [
            _mk_post(f"p{i}", create_at=i * 100, user_id="u1",
                     message=f"m{i}")
            for i in range(5)
        ]
        mm.users["u1"] = {"username": "a"}
        rc, out, _ = self._invoke(
            mm,
            ["mm-bridge", "read", "--channel", "c1",
             "--thread", "r1", "-n", "3"],
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("m0", out)
        self.assertNotIn("m1", out)
        self.assertIn("m2", out)
        self.assertIn("m4", out)

    def test_no_posts_still_exits_0(self) -> None:
        mm = FakeMM()
        mm.posts_by_channel["c1"] = []
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "read", "--channel", "c1"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
