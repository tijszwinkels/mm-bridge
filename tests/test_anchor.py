"""Anchor model tests — the unified `(channel_id, Optional[root_id])` key.

Covers the `Anchor` value type and the rewritten `ChannelMapping` API built
around it (one forward map, one reverse map), plus the v2 → v3 JSON schema
migration path that reads legacy `channel_to_session` + `thread_mapping`
fields and re-emits them under the new `entries` key on first save.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mm_bridge.config import Anchor, ChannelMapping


class AnchorTypeTests(unittest.TestCase):
    """`Anchor` — a frozen, hashable `(channel_id, Optional[root_id])` tuple."""

    def test_channel_anchor_has_no_root(self) -> None:
        a = Anchor("chan-1")
        self.assertEqual(a.channel_id, "chan-1")
        self.assertIsNone(a.root_id)
        self.assertFalse(a.is_thread)

    def test_thread_anchor_carries_root(self) -> None:
        a = Anchor("chan-1", "root-42")
        self.assertEqual(a.channel_id, "chan-1")
        self.assertEqual(a.root_id, "root-42")
        self.assertTrue(a.is_thread)

    def test_anchors_are_hashable_and_usable_as_dict_keys(self) -> None:
        d = {Anchor("c1"): "s1", Anchor("c1", "r1"): "s2"}
        self.assertEqual(d[Anchor("c1")], "s1")
        self.assertEqual(d[Anchor("c1", "r1")], "s2")

    def test_channel_and_thread_anchors_with_same_channel_are_distinct(self) -> None:
        self.assertNotEqual(Anchor("c1"), Anchor("c1", "r1"))

    def test_equal_anchors_hash_identically(self) -> None:
        self.assertEqual(hash(Anchor("c1", "r1")), hash(Anchor("c1", "r1")))

    def test_empty_string_root_normalizes_to_none(self) -> None:
        """Passing `""` as root_id is treated as a channel anchor — no empty-string roots."""
        self.assertEqual(Anchor("c1", ""), Anchor("c1"))


class ChannelMappingAnchorAPITests(unittest.TestCase):
    """`ChannelMapping` exposes `link / unlink / get_session / get_anchor` over `Anchor`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_link_channel_anchor_roundtrips(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))

    def test_link_thread_anchor_roundtrips(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_channel_and_thread_anchors_coexist(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1", "r1"), "s-fork")
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_unlink_returns_and_removes_session(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        removed = m.unlink(Anchor("c1", "r1"))
        self.assertEqual(removed, "s-fork")
        self.assertIsNone(m.get_session(Anchor("c1", "r1")))
        self.assertIsNone(m.get_anchor("s-fork"))

    def test_unlink_missing_returns_none(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.unlink(Anchor("nope")))

    def test_link_overwrites_existing_session_for_same_anchor(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1"), "s2")
        self.assertEqual(m.get_session(Anchor("c1")), "s2")
        # Old reverse entry is gone (no dangling session_to_anchor row).
        self.assertIsNone(m.get_anchor("s1"))
        self.assertEqual(m.get_anchor("s2"), Anchor("c1"))

    def test_persistence_survives_reload(self) -> None:
        m1 = ChannelMapping.load(self.state, self.sdir)
        m1.link(Anchor("c1"), "s1")
        m1.link(Anchor("c1", "r1"), "s-fork")

        m2 = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m2.get_session(Anchor("c1")), "s1")
        self.assertEqual(m2.get_session(Anchor("c1", "r1")), "s-fork")


class ChannelMappingMigrationTests(unittest.TestCase):
    """Legacy v2 JSON state files are transparently upgraded to v3 on load."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_v2(self, data: dict) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps(data))

    def test_reads_legacy_v2_channel_only(self) -> None:
        self._write_v2({"channel_to_session": {"c1": "s1"}})
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))

    def test_reads_legacy_v2_with_thread_mapping(self) -> None:
        self._write_v2({
            "channel_to_session": {"c1": "s1"},
            "thread_mapping": {"c1:r1": "s-fork", "c2:r2": "s-fork-2"},
        })
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_session(Anchor("c2", "r2")), "s-fork-2")
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_save_emits_current_schema(self) -> None:
        from mm_bridge.config import STATE_SCHEMA_VERSION
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1", "r1"), "s-fork")

        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("version"), STATE_SCHEMA_VERSION)
        self.assertIn("entries", data)
        entries = data["entries"]
        self.assertIsInstance(entries, list)
        # Each entry is a dict with channel_id, root_id, session_id.
        by_session = {e["session_id"]: e for e in entries}
        self.assertEqual(by_session["s1"]["channel_id"], "c1")
        self.assertIsNone(by_session["s1"].get("root_id"))
        self.assertEqual(by_session["s-fork"]["channel_id"], "c1")
        self.assertEqual(by_session["s-fork"]["root_id"], "r1")

    def test_legacy_v2_file_is_rewritten_as_current_schema_on_first_save(self) -> None:
        from mm_bridge.config import STATE_SCHEMA_VERSION
        self._write_v2({
            "channel_to_session": {"c1": "s1"},
            "thread_mapping": {"c1:r1": "s-fork"},
        })
        m = ChannelMapping.load(self.state, self.sdir)
        # Trigger a save (link is a no-op write if nothing changes, so just
        # re-link the same pair — save() is called unconditionally).
        m.link(Anchor("c1"), "s1")
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("version"), STATE_SCHEMA_VERSION)
        self.assertNotIn("channel_to_session", data)
        self.assertNotIn("thread_mapping", data)


class LastEventSeqTests(unittest.TestCase):
    """``last_event_seq`` is the SSE cursor checkpoint for restart safety."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_none_for_fresh_file(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.last_event_seq)

    def test_save_emits_last_event_seq_field(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.set_event_seq(42)
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("last_event_seq"), 42)

    def test_set_event_seq_is_monotonic(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.set_event_seq(100)
        m.set_event_seq(50)  # stale event on reconnect — must not rewind
        self.assertEqual(m.last_event_seq, 100)

    def test_v3_state_loads_with_seq_none(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 3,
            "entries": [
                {"channel_id": "c1", "root_id": None, "session_id": "s1"},
            ],
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.last_event_seq)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")

    def test_v4_state_roundtrips_seq(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 4,
            "entries": [],
            "last_event_seq": 7259,
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.last_event_seq, 7259)


if __name__ == "__main__":
    unittest.main()
