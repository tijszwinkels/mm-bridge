"""Pure-helper tests for `mm-bridge spawn`."""

from __future__ import annotations

import unittest

from mm_bridge import spawn


class FormatParentHeaderTests(unittest.TestCase):
    def test_wraps_channel_name_in_mention_syntax(self) -> None:
        self.assertEqual(
            spawn.format_parent_header("my-parent"),
            "Parent: ~my-parent~",
        )

    def test_appends_thread_permalink_when_forked(self) -> None:
        self.assertEqual(
            spawn.format_parent_header(
                "my-parent",
                thread_permalink="http://localhost:8065/workspace/pl/root-9",
            ),
            "Parent: ~my-parent~ "
            "([thread](http://localhost:8065/workspace/pl/root-9))",
        )

    def test_ignores_empty_permalink(self) -> None:
        self.assertEqual(
            spawn.format_parent_header("my-parent", thread_permalink=""),
            "Parent: ~my-parent~",
        )


class FormatPostPermalinkTests(unittest.TestCase):
    def test_standard_url(self) -> None:
        self.assertEqual(
            spawn.format_post_permalink(
                "http://localhost:8065", "workspace", "abc123",
            ),
            "http://localhost:8065/workspace/pl/abc123",
        )

    def test_trailing_slash_on_base_is_stripped(self) -> None:
        self.assertEqual(
            spawn.format_post_permalink(
                "https://mm.example.com/", "team", "abc",
            ),
            "https://mm.example.com/team/pl/abc",
        )


class BuildMmBaseUrlTests(unittest.TestCase):
    def test_standard_url(self) -> None:
        self.assertEqual(
            spawn.build_mm_base_url("http", "localhost", 8065),
            "http://localhost:8065",
        )

    def test_omits_default_http_port(self) -> None:
        self.assertEqual(
            spawn.build_mm_base_url("http", "mm.example.com", 80),
            "http://mm.example.com",
        )

    def test_omits_default_https_port(self) -> None:
        self.assertEqual(
            spawn.build_mm_base_url("https", "mm.example.com", 443),
            "https://mm.example.com",
        )


class FormatSpawnAnnouncementTests(unittest.TestCase):
    def test_header_line_uses_title_and_channel_name(self) -> None:
        out = spawn.format_spawn_announcement(
            "Hello", "sub-abc", "fix the bug",
        )
        self.assertIn("Spawned **Hello** in ~sub-abc~", out)

    def test_single_line_prompt_quoted(self) -> None:
        out = spawn.format_spawn_announcement(
            "T", "c", "fix the bug",
        )
        self.assertIn("> fix the bug", out)

    def test_multi_line_prompt_quoted_per_line(self) -> None:
        out = spawn.format_spawn_announcement(
            "T", "c", "line 1\nline 2\nline 3",
        )
        self.assertIn("> line 1\n> line 2\n> line 3", out)

    def test_empty_prompt_omits_quote_block(self) -> None:
        out = spawn.format_spawn_announcement("T", "c", "")
        self.assertNotIn("\n>", out)
        self.assertFalse(out.endswith(">"))

    def test_whitespace_only_prompt_treated_as_empty(self) -> None:
        out = spawn.format_spawn_announcement("T", "c", "   \n  ")
        self.assertNotIn(">", out)


class FormatSpawnKickoffTests(unittest.TestCase):
    def test_header_line_uses_parent_channel_name(self) -> None:
        out = spawn.format_spawn_kickoff("my-parent", "fix the bug")
        self.assertIn("Spawned from ~my-parent~", out)

    def test_single_line_prompt_quoted(self) -> None:
        out = spawn.format_spawn_kickoff("p", "fix the bug")
        self.assertIn("> fix the bug", out)

    def test_multi_line_prompt_quoted_per_line(self) -> None:
        out = spawn.format_spawn_kickoff("p", "line 1\nline 2")
        self.assertIn("> line 1\n> line 2", out)

    def test_empty_prompt_omits_quote_block(self) -> None:
        out = spawn.format_spawn_kickoff("p", "")
        self.assertNotIn(">", out)

    def test_whitespace_only_prompt_treated_as_empty(self) -> None:
        out = spawn.format_spawn_kickoff("p", "   \n  ")
        self.assertNotIn(">", out)


class DeriveDisplayNameTests(unittest.TestCase):
    def test_uses_title_when_given(self) -> None:
        self.assertEqual(
            spawn.derive_display_name("My Task", "fallback"), "My Task",
        )

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(
            spawn.derive_display_name("  My Task  ", "fallback"), "My Task",
        )

    def test_falls_back_when_title_none(self) -> None:
        self.assertEqual(
            spawn.derive_display_name(None, "fallback"), "fallback",
        )

    def test_falls_back_when_title_blank(self) -> None:
        self.assertEqual(
            spawn.derive_display_name("   ", "fallback"), "fallback",
        )

    def test_truncates_long_title(self) -> None:
        out = spawn.derive_display_name("x" * 200, "fb")
        self.assertEqual(len(out), spawn.MM_DISPLAY_NAME_MAX)


if __name__ == "__main__":
    unittest.main()
