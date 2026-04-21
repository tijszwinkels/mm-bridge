"""Sidecar file tests — mirror session_id → channel_id mapping to disk."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mm_bridge import sidecar
from mm_bridge.config import Anchor, ChannelMapping


class SidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_creates_file_with_channel_id(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1")
        path = self.dir / "sess-1"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(), "chan-1")

    def test_write_with_root_id_creates_two_line_file(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1", "root-42")
        self.assertEqual((self.dir / "sess-1").read_text(), "chan-1\nroot-42")

    def test_write_has_owner_only_perms(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1")
        file_mode = (self.dir / "sess-1").stat().st_mode & 0o777
        self.assertEqual(file_mode, 0o600)
        dir_mode = self.dir.stat().st_mode & 0o777
        self.assertEqual(dir_mode, 0o700)

    def test_write_overwrites_existing(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1")
        sidecar.write(self.dir, "sess-1", "chan-2")
        self.assertEqual((self.dir / "sess-1").read_text(), "chan-2")

    def test_write_downgrade_to_channel_only_removes_root_line(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1", "root-42")
        sidecar.write(self.dir, "sess-1", "chan-1")
        self.assertEqual((self.dir / "sess-1").read_text(), "chan-1")

    def test_write_noop_on_empty_ids(self) -> None:
        sidecar.write(self.dir, "", "chan-1")
        sidecar.write(self.dir, "sess-1", "")
        if self.dir.exists():
            self.assertEqual(list(self.dir.iterdir()), [])

    def test_delete_removes_file(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1")
        sidecar.delete(self.dir, "sess-1")
        self.assertFalse((self.dir / "sess-1").exists())

    def test_delete_missing_is_noop(self) -> None:
        sidecar.delete(self.dir, "sess-unknown")  # must not raise

    def test_read_missing_returns_none(self) -> None:
        self.assertIsNone(sidecar.read(self.dir, "sess-missing"))

    def test_read_single_line_returns_channel_only(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1")
        self.assertEqual(sidecar.read(self.dir, "sess-1"), ("chan-1", None))

    def test_read_two_lines_returns_channel_and_root(self) -> None:
        sidecar.write(self.dir, "sess-1", "chan-1", "root-42")
        self.assertEqual(sidecar.read(self.dir, "sess-1"), ("chan-1", "root-42"))

    def test_read_legacy_trailing_newline_reads_as_channel_only(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "sess-1").write_text("chan-1\n")
        self.assertEqual(sidecar.read(self.dir, "sess-1"), ("chan-1", None))

    def test_read_blank_second_line_is_channel_only(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "sess-1").write_text("chan-1\n\n")
        self.assertEqual(sidecar.read(self.dir, "sess-1"), ("chan-1", None))

    def test_reconcile_writes_missing_and_removes_stale(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "stale").write_text("old-chan")
        sidecar.reconcile(
            self.dir,
            {"sess-a": ("chan-a", None), "sess-b": ("chan-b", None)},
        )
        self.assertFalse((self.dir / "stale").exists())
        self.assertEqual((self.dir / "sess-a").read_text(), "chan-a")
        self.assertEqual((self.dir / "sess-b").read_text(), "chan-b")

    def test_reconcile_writes_thread_fork_sidecars_with_root_line(self) -> None:
        sidecar.reconcile(
            self.dir,
            {
                "sess-chan": ("chan-1", None),
                "sess-fork": ("chan-1", "root-42"),
            },
        )
        self.assertEqual((self.dir / "sess-chan").read_text(), "chan-1")
        self.assertEqual((self.dir / "sess-fork").read_text(), "chan-1\nroot-42")

    def test_reconcile_with_empty_mapping_clears_dir(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "a").write_text("x")
        (self.dir / "b").write_text("y")
        sidecar.reconcile(self.dir, {})
        self.assertEqual(list(self.dir.iterdir()), [])


class ChannelMappingSidecarTests(unittest.TestCase):
    """ChannelMapping writes/removes sidecars on link/unlink."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_link_writes_sidecar(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        self.assertEqual((self.sdir / "s1").read_text(), "c1")

    def test_link_thread_anchor_writes_two_line_sidecar(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        self.assertEqual((self.sdir / "s-fork").read_text(), "c1\nr1")

    def test_unlink_removes_sidecar(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.unlink(Anchor("c1"))
        self.assertFalse((self.sdir / "s1").exists())

    def test_unlink_thread_anchor_removes_sidecar(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        m.unlink(Anchor("c1", "r1"))
        self.assertFalse((self.sdir / "s-fork").exists())

    def test_load_reconciles_stale_sidecars(self) -> None:
        """A sidecar file for an unmapped session is removed on load."""
        self.sdir.mkdir(parents=True)
        (self.sdir / "ghost").write_text("c-ghost")

        ChannelMapping.load(self.state, self.sdir)

        self.assertFalse((self.sdir / "ghost").exists())

    def test_load_writes_missing_sidecars(self) -> None:
        """A session already in state.json gets a sidecar on load."""
        m1 = ChannelMapping.load(self.state, self.sdir)
        m1.link(Anchor("c1"), "s1")
        (self.sdir / "s1").unlink()

        ChannelMapping.load(self.state, self.sdir)

        self.assertEqual((self.sdir / "s1").read_text(), "c1")


if __name__ == "__main__":
    unittest.main()
