"""Unit tests for the shared ``-`` → stdin helper (``cli._read_stdin_arg``).

The helper backs the ``-`` positional convention across free-text
subcommands (``post`` message, ``spawn`` prompt). Non-``-`` values pass
through verbatim; ``-`` reads all of stdin and strips a trailing newline
run, matching ``post``'s long-standing behaviour. It guards two
foot-guns: a TTY stdin (would hang forever) and — unless the caller opts
into ``allow_empty`` — an empty/whitespace body.
"""

from __future__ import annotations

import io
import unittest

from mm_bridge import cli


class _FakeTTY(io.StringIO):
    """A StringIO that reports itself as an interactive terminal."""

    def isatty(self) -> bool:  # noqa: D401 - trivial override
        return True


class ReadStdinArgTests(unittest.TestCase):
    def test_non_dash_value_returned_verbatim(self) -> None:
        # Must NOT touch stdin at all — pass a TTY to prove it's untouched.
        out = cli._read_stdin_arg(
            "literal body", label="message", stdin=_FakeTTY("junk"),
        )
        self.assertEqual(out, "literal body")

    def test_dash_reads_stdin_and_strips_single_trailing_newline(self) -> None:
        out = cli._read_stdin_arg("-", label="message", stdin=io.StringIO("body\n"))
        self.assertEqual(out, "body")

    def test_dash_strips_all_trailing_newlines_like_post(self) -> None:
        # post uses ``rstrip("\n")`` — match it exactly (strips a run).
        out = cli._read_stdin_arg(
            "-", label="message", stdin=io.StringIO("body\n\n\n"),
        )
        self.assertEqual(out, "body")

    def test_dash_preserves_internal_newlines(self) -> None:
        out = cli._read_stdin_arg(
            "-", label="prompt", stdin=io.StringIO("line 1\nline 2\n"),
        )
        self.assertEqual(out, "line 1\nline 2")

    def test_dash_empty_stdin_raises_when_not_allow_empty(self) -> None:
        with self.assertRaises(cli.StdinError):
            cli._read_stdin_arg("-", label="prompt", stdin=io.StringIO(""))

    def test_dash_whitespace_only_stdin_raises_when_not_allow_empty(self) -> None:
        with self.assertRaises(cli.StdinError):
            cli._read_stdin_arg("-", label="prompt", stdin=io.StringIO("  \n \t\n"))

    def test_dash_empty_stdin_allowed_when_allow_empty(self) -> None:
        out = cli._read_stdin_arg(
            "-", label="message", stdin=io.StringIO(""), allow_empty=True,
        )
        self.assertEqual(out, "")

    def test_dash_tty_stdin_raises_rather_than_hanging(self) -> None:
        with self.assertRaises(cli.StdinError) as cm:
            cli._read_stdin_arg("-", label="prompt", stdin=_FakeTTY("x"))
        self.assertIn("terminal", str(cm.exception).lower())

    def test_error_message_mentions_the_label(self) -> None:
        with self.assertRaises(cli.StdinError) as cm:
            cli._read_stdin_arg("-", label="prompt", stdin=io.StringIO(""))
        self.assertIn("prompt", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
