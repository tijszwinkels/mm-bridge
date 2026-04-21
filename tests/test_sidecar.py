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

    def test_reconcile_writes_missing_and_removes_stale(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "stale").write_text("old-chan")
        sidecar.reconcile(self.dir, {"sess-a": "chan-a", "sess-b": "chan-b"})
        self.assertFalse((self.dir / "stale").exists())
        self.assertEqual((self.dir / "sess-a").read_text(), "chan-a")
        self.assertEqual((self.dir / "sess-b").read_text(), "chan-b")

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

    def test_unlink_removes_sidecar(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.unlink(Anchor("c1"))
        self.assertFalse((self.sdir / "s1").exists())

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
