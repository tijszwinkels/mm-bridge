"""Sidecar file tests — mirror session_id → channel_id mapping to disk."""

from __future__ import annotations

import os
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

    def test_read_dashed_uuid_falls_back_to_canonical_ses_form(self) -> None:
        """Claude sub-sessions see ``CLAUDE_SESSION_ID`` as a dashed UUID
        (8-4-4-4-12), but the harness writes sidecars under the canonical
        ``ses_<32hex>`` form. The read path must try the canonical form
        when the literal lookup misses, otherwise ``mm-bridge channel``
        and friends report 'no sidecar' from inside a spawned sub-session.
        """
        sidecar.write(self.dir, "ses_e6cc19e7a0b94e7896a6ea5b51e7fa24", "chan-X")
        self.assertEqual(
            sidecar.read(self.dir, "e6cc19e7-a0b9-4e78-96a6-ea5b51e7fa24"),
            ("chan-X", None),
        )

    def test_read_raw_32_hex_falls_back_to_canonical_ses_form(self) -> None:
        """Same canonicalization should also catch a 32-hex id without
        dashes — defensive against callers that already stripped them."""
        sidecar.write(self.dir, "ses_e6cc19e7a0b94e7896a6ea5b51e7fa24", "chan-Y")
        self.assertEqual(
            sidecar.read(self.dir, "e6cc19e7a0b94e7896a6ea5b51e7fa24"),
            ("chan-Y", None),
        )

    def test_read_canonical_ses_form_does_not_double_prefix(self) -> None:
        """An input already in ``ses_<hex>`` form must not be rewritten
        to ``ses_ses_<hex>``."""
        sidecar.write(self.dir, "ses_e6cc19e7a0b94e7896a6ea5b51e7fa24", "chan-Z")
        # And there is no second file at the doubly-prefixed name.
        self.assertEqual(
            sidecar.read(self.dir, "ses_e6cc19e7a0b94e7896a6ea5b51e7fa24"),
            ("chan-Z", None),
        )
        self.assertFalse(
            (self.dir / "ses_ses_e6cc19e7a0b94e7896a6ea5b51e7fa24").exists(),
        )

    def test_read_literal_match_wins_over_canonical_fallback(self) -> None:
        """A sidecar at the literal lookup path beats the canonical
        fallback — so codex / MM_BRIDGE_SESSION_ID ids that already
        match their on-disk filename round-trip unchanged.

        The two ids here intentionally do *not* canonicalize to each
        other (different hex). The original PR #18 test paired a dashed
        UUID with its own canonical ses_<hex>, but those refer to the
        same logical session under the new symlink-alias semantics —
        writing the canonical now refreshes the dashed symlink, so the
        collision is no longer representable. The invariant being tested
        (literal lookup wins for codex-shaped ids that don't follow the
        ses_<hex> convention) is preserved with the new id pair.
        """
        sidecar.write(self.dir, "e6cc19e7-a0b9-4e78-96a6-ea5b51e7fa24", "chan-literal")
        sidecar.write(self.dir, "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "chan-canonical")
        self.assertEqual(
            sidecar.read(self.dir, "e6cc19e7-a0b9-4e78-96a6-ea5b51e7fa24"),
            ("chan-literal", None),
        )

    def test_read_unknown_dashed_uuid_still_returns_none(self) -> None:
        """A dashed UUID with no sidecar in either form returns None
        cleanly — no spurious matches from the fallback."""
        self.dir.mkdir(parents=True)
        self.assertIsNone(
            sidecar.read(self.dir, "deadbeef-dead-beef-dead-beefdeadbeef"),
        )

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


class DashedAliasTests(unittest.TestCase):
    """The dashed-UUID symlink alias makes the literal
    ``test -f ~/.mm-bridge/sessions/$CLAUDE_SESSION_ID`` check pass from
    inside a spawned Claude Code sub-session, where ``$CLAUDE_SESSION_ID``
    is the dashed UUID rather than the canonical ``ses_<32hex>`` filename.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_canonical_id_resolves_dashed_alias_to_ses_id(self) -> None:
        """write() of a ses_<32hex> id creates a dashed-UUID alias symlink;
        canonical_id(dashed) must follow it to the ses_ id (the harness id the
        bridge stores in its anchor mapping)."""
        harness_id = "ses_00112233445566778899aabbccddeeff"
        dashed = "00112233-4455-6677-8899-aabbccddeeff"
        sidecar.write(self.dir, harness_id, "c1")
        self.assertEqual(sidecar.canonical_id(self.dir, dashed), harness_id)

    def test_canonical_id_real_file_returned_verbatim(self) -> None:
        """Codex / spawned shape: a real file at the id (no alias) IS the
        harness id → returned verbatim."""
        sidecar.write(self.dir, "codex_abc", "c1")
        self.assertEqual(sidecar.canonical_id(self.dir, "codex_abc"), "codex_abc")

    def test_canonical_id_ses_id_returned_verbatim(self) -> None:
        harness_id = "ses_00112233445566778899aabbccddeeff"
        sidecar.write(self.dir, harness_id, "c1")
        self.assertEqual(sidecar.canonical_id(self.dir, harness_id), harness_id)

    def test_canonical_id_dashed_without_alias_reconstructs_ses(self) -> None:
        """If the alias symlink is absent but the real ses_<hex> file exists
        (best-effort alias write failed), canonical_id still reconstructs it —
        matching read()'s dashed→canonical fallback."""
        harness_id = "ses_00112233445566778899aabbccddeeff"
        dashed = "00112233-4455-6677-8899-aabbccddeeff"
        sidecar.write(self.dir, harness_id, "c1")
        (self.dir / dashed).unlink()  # drop the alias
        self.assertEqual(sidecar.canonical_id(self.dir, dashed), harness_id)

    def test_canonical_id_unresolvable_returns_none(self) -> None:
        self.assertIsNone(sidecar.canonical_id(self.dir, "nope-nothing-here"))
        self.assertIsNone(sidecar.canonical_id(self.dir, ""))

    def test_dashed_alias_helper(self) -> None:
        """The pure helper accepts canonical ses_<32hex> only."""
        cases = [
            # valid canonical → dashed UUID
            (
                "ses_e7c23a68538d4393a1b0fa3d75a34b2a",
                "e7c23a68-538d-4393-a1b0-fa3d75a34b2a",
            ),
            # too few hex chars
            ("ses_e7c23a68538d4393a1b0fa3d75a34b2", None),
            # too many hex chars
            ("ses_e7c23a68538d4393a1b0fa3d75a34b2af", None),
            # non-hex char ('G')
            ("ses_e7c23a68538d4393a1b0fa3d75a34b2G", None),
            # uppercase hex isn't canonical
            ("ses_E7C23A68538D4393A1B0FA3D75A34B2A", None),
            # claude-style dashed UUID isn't a canonical id
            ("claude_e7c23a68-538d-4393-a1b0-fa3d75a34b2a", None),
            # bare dashed UUID
            ("e7c23a68-538d-4393-a1b0-fa3d75a34b2a", None),
            # empty
            ("", None),
            # bare prefix
            ("ses_", None),
            # codex-style id is left alone
            ("codex_019ef2a3b4c5d6e7f80123456789abcd", None),
        ]
        for session_id, expected in cases:
            with self.subTest(session_id=session_id):
                self.assertEqual(sidecar._dashed_alias(session_id), expected)

    def test_write_creates_dashed_alias(self) -> None:
        sid = "ses_e7c23a68538d4393a1b0fa3d75a34b2a"
        sidecar.write(self.dir, sid, "chan-1")
        alias = self.dir / "e7c23a68-538d-4393-a1b0-fa3d75a34b2a"
        self.assertTrue(alias.is_symlink())
        # Relative target — just the canonical filename, not an absolute path.
        self.assertEqual(os.readlink(str(alias)), sid)
        # Following the symlink yields the canonical channel data.
        self.assertEqual(alias.read_text(), "chan-1")

    def test_write_no_alias_for_claude_prefix(self) -> None:
        """Claude harness sessions with a dashed-UUID id already match
        the on-disk filename — no alias needed."""
        sidecar.write(
            self.dir,
            "claude_e7c23a68-538d-4393-a1b0-fa3d75a34b2a",
            "chan-1",
        )
        symlinks = [p for p in self.dir.iterdir() if p.is_symlink()]
        self.assertEqual(symlinks, [])

    def test_write_no_alias_for_codex_prefix(self) -> None:
        """Codex uuid7 ids already include a unique suffix."""
        sidecar.write(
            self.dir,
            "codex_019ef2a3b4c5d6e7f80123456789abcd",
            "chan-1",
        )
        symlinks = [p for p in self.dir.iterdir() if p.is_symlink()]
        self.assertEqual(symlinks, [])

    def test_write_alias_idempotent(self) -> None:
        sid = "ses_e7c23a68538d4393a1b0fa3d75a34b2a"
        sidecar.write(self.dir, sid, "chan-1")
        sidecar.write(self.dir, sid, "chan-2")  # update channel, same session
        alias = self.dir / "e7c23a68-538d-4393-a1b0-fa3d75a34b2a"
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(str(alias)), sid)
        self.assertEqual(alias.read_text(), "chan-2")

    def test_write_alias_replaces_wrong_symlink(self) -> None:
        """A pre-existing alias pointing somewhere else is corrected."""
        self.dir.mkdir(parents=True)
        alias = self.dir / "e7c23a68-538d-4393-a1b0-fa3d75a34b2a"
        os.symlink("some-other-target", str(alias))
        sid = "ses_e7c23a68538d4393a1b0fa3d75a34b2a"
        sidecar.write(self.dir, sid, "chan-1")
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(str(alias)), sid)

    def test_write_alias_replaces_regular_file_at_alias_path(self) -> None:
        """A stale regular file at the alias path is replaced with a symlink."""
        self.dir.mkdir(parents=True)
        alias = self.dir / "e7c23a68-538d-4393-a1b0-fa3d75a34b2a"
        alias.write_text("hand-edited junk")
        sid = "ses_e7c23a68538d4393a1b0fa3d75a34b2a"
        sidecar.write(self.dir, sid, "chan-1")
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(str(alias)), sid)
        self.assertEqual(alias.read_text(), "chan-1")

    def test_delete_removes_alias(self) -> None:
        sid = "ses_e7c23a68538d4393a1b0fa3d75a34b2a"
        sidecar.write(self.dir, sid, "chan-1")
        alias = self.dir / "e7c23a68-538d-4393-a1b0-fa3d75a34b2a"
        self.assertTrue(alias.is_symlink())
        sidecar.delete(self.dir, sid)
        self.assertFalse((self.dir / sid).exists())
        # is_symlink() catches dangling symlinks too; exists() follows.
        self.assertFalse(alias.is_symlink())
        self.assertFalse(alias.exists())

    def test_reconcile_preserves_aliases_for_kept_sessions(self) -> None:
        sid_a = "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sid_b = "ses_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        sidecar.write(self.dir, sid_a, "chan-a")
        sidecar.write(self.dir, sid_b, "chan-b")
        sidecar.reconcile(
            self.dir,
            {sid_a: ("chan-a", None), sid_b: ("chan-b", None)},
        )
        # Both canonicals survive.
        self.assertEqual((self.dir / sid_a).read_text(), "chan-a")
        self.assertEqual((self.dir / sid_b).read_text(), "chan-b")
        # Both aliases survive and point correctly.
        alias_a = self.dir / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        alias_b = self.dir / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.assertTrue(alias_a.is_symlink())
        self.assertTrue(alias_b.is_symlink())
        self.assertEqual(os.readlink(str(alias_a)), sid_a)
        self.assertEqual(os.readlink(str(alias_b)), sid_b)

    def test_reconcile_removes_alias_when_session_removed(self) -> None:
        sid_a = "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sid_b = "ses_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        sidecar.write(self.dir, sid_a, "chan-a")
        sidecar.write(self.dir, sid_b, "chan-b")
        sidecar.reconcile(self.dir, {sid_a: ("chan-a", None)})
        # Kept session — canonical + alias both survive.
        self.assertEqual((self.dir / sid_a).read_text(), "chan-a")
        alias_a = self.dir / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.assertTrue(alias_a.is_symlink())
        self.assertEqual(os.readlink(str(alias_a)), sid_a)
        # Dropped session — canonical + alias both gone.
        self.assertFalse((self.dir / sid_b).exists())
        alias_b = self.dir / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.assertFalse(alias_b.is_symlink())
        self.assertFalse(alias_b.exists())


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
