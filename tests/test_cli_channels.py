"""Tests for `mm-bridge channels`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli
from mm_bridge.config import Anchor, ChannelMapping, Config


@dataclass
class FakeMM:
    _channels: list = field(default_factory=list)
    logged_in: bool = False

    def login(self) -> None:
        self.logged_in = True

    def list_bot_channels(self) -> list[dict]:
        return list(self._channels)


def _mk(
    cid: str,
    *,
    name: str = "",
    display_name: str = "",
    last_post_at: int = 0,
    create_at: int = 0,
    purpose: str = "",
    header: str = "",
    type: str = "O",
) -> dict:
    return {
        "id": cid,
        "name": name or f"slug-{cid}",
        "display_name": display_name,
        "last_post_at": last_post_at,
        "create_at": create_at,
        "purpose": purpose,
        "header": header,
        "type": type,
    }


class ChannelsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=f"{self.tmp.name}/sessions",
            state_file=f"{self.tmp.name}/state.json",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(self, fake_mm: FakeMM, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    def test_text_format_tab_separated_sorted_by_recency(self) -> None:
        mm = FakeMM(_channels=[
            _mk("c1", display_name="old", last_post_at=100, create_at=10),
            _mk("c2", display_name="new", last_post_at=300, create_at=20),
            _mk("c3", display_name="mid", last_post_at=200, create_at=15),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        self.assertTrue(mm.logged_in)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        ids = [ln.split("\t")[0] for ln in lines]
        self.assertEqual(ids, ["c2", "c3", "c1"])
        # Each row has at least id + display_name.
        for ln in lines:
            parts = ln.split("\t")
            self.assertGreaterEqual(len(parts), 2)

    def test_title_filter_matches_display_name_and_name_case_insensitive(self) -> None:
        mm = FakeMM(_channels=[
            _mk("c1", display_name="Mattermost Migration", name="mm-migration",
                last_post_at=100),
            _mk("c2", display_name="general", name="general", last_post_at=200),
            _mk("c3", display_name="unrelated", name="something-mm-side",
                last_post_at=50),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels", "--title", "MM"])
        self.assertEqual(rc, 0)
        ids = {ln.split("\t")[0] for ln in out.splitlines() if ln.strip()}
        self.assertEqual(ids, {"c1", "c3"})

    def test_dms_excluded(self) -> None:
        mm = FakeMM(_channels=[
            _mk("c1", display_name="open", last_post_at=100),
            _mk("dm", display_name="dm", last_post_at=200, type="D"),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        ids = {ln.split("\t")[0] for ln in out.splitlines() if ln.strip()}
        self.assertEqual(ids, {"c1"})

    def test_tiebreak_on_create_at_when_no_posts(self) -> None:
        mm = FakeMM(_channels=[
            _mk("old", display_name="old", last_post_at=0, create_at=10),
            _mk("new", display_name="new", last_post_at=0, create_at=20),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        ids = [ln.split("\t")[0] for ln in out.splitlines() if ln.strip()]
        self.assertEqual(ids, ["new", "old"])

    def test_session_badge_appears_when_channel_linked(self) -> None:
        # Seed a channel mapping.
        mapping = ChannelMapping.load(
            self.cfg.state_file, sidecar_dir=self.cfg.sidecar_dir,
        )
        mapping.link(Anchor("c1"), "sess-1")

        mm = FakeMM(_channels=[
            _mk("c1", display_name="linked", last_post_at=100),
            _mk("c2", display_name="unlinked", last_post_at=50),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        rows = {ln.split("\t")[0]: ln for ln in out.splitlines() if ln.strip()}
        self.assertIn("[session]", rows["c1"])
        self.assertNotIn("[session]", rows["c2"])

    def test_purpose_badge_truncated_and_sanitised(self) -> None:
        long_purpose = "x" * 80 + "\tembedded\tnewline\n"
        mm = FakeMM(_channels=[
            _mk("c1", display_name="d", last_post_at=100, purpose=long_purpose),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        line = out.splitlines()[0]
        self.assertIn("[purpose:", line)
        # Tab / newline in the badge would break text splitting.
        badge = line.split("\t", 2)[2]
        self.assertNotIn("\n", badge)
        # Only one tab-break (between id/display_name/badges); no bonus tabs.
        self.assertEqual(line.count("\t"), 2)

    def test_display_name_falls_back_to_name_when_empty(self) -> None:
        mm = FakeMM(_channels=[
            _mk("c1", display_name="", name="the-slug", last_post_at=100),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        line = out.splitlines()[0]
        self.assertIn("the-slug", line)

    def test_cap_default_20(self) -> None:
        chans = [
            _mk(f"c{i}", display_name=f"d{i}", last_post_at=i) for i in range(30)
        ]
        mm = FakeMM(_channels=chans)
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 20)

    def test_n_zero_disables_cap(self) -> None:
        chans = [
            _mk(f"c{i}", display_name=f"d{i}", last_post_at=i) for i in range(30)
        ]
        mm = FakeMM(_channels=chans)
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels", "-n", "0"])
        self.assertEqual(rc, 0)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 30)

    def test_json_format_shape(self) -> None:
        mapping = ChannelMapping.load(
            self.cfg.state_file, sidecar_dir=self.cfg.sidecar_dir,
        )
        mapping.link(Anchor("c1"), "sess-1")
        mm = FakeMM(_channels=[
            _mk("c1", name="slug", display_name="d1", last_post_at=100,
                create_at=10, purpose="p", header="h"),
            _mk("c2", display_name="d2", last_post_at=50),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels", "--format", "json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["id"], "c1")
        self.assertEqual(data[0]["name"], "slug")
        self.assertEqual(data[0]["display_name"], "d1")
        self.assertEqual(data[0]["last_post_at"], 100)
        self.assertEqual(data[0]["create_at"], 10)
        self.assertEqual(data[0]["purpose"], "p")
        self.assertEqual(data[0]["header"], "h")
        self.assertEqual(data[0]["session_id"], "sess-1")
        self.assertIsNone(data[1]["session_id"])

    def test_no_matches_text_prints_nothing(self) -> None:
        mm = FakeMM(_channels=[
            _mk("c1", display_name="zzz", last_post_at=100),
        ])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels", "--title", "nope"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_no_matches_json_prints_empty_array(self) -> None:
        mm = FakeMM(_channels=[])
        rc, out, _ = self._invoke(mm, ["mm-bridge", "channels", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

    def test_missing_bot_token_exits_1(self) -> None:
        self.cfg.mm_bot_token = ""
        mm = FakeMM()
        rc, _, err = self._invoke(mm, ["mm-bridge", "channels"])
        self.assertEqual(rc, 1)
        self.assertIn("MM_BOT_TOKEN", err)


if __name__ == "__main__":
    unittest.main()
