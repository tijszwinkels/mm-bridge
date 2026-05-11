"""Tests for channel-purpose resume command helpers."""

from __future__ import annotations

import pytest

from mm_bridge.resume_header import (
    format_resume_block,
    format_resume_command,
    merge_into_purpose,
    normalize_backend,
)


# ---------------------------------------------------------------------------
# format_resume_command — bare CLI command, optionally prefixed with `cd`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "cwd", "dangerous", "expected"),
    [
        (
            "claude", "/home/foo", False,
            "cd /home/foo && claude --resume sess-123",
        ),
        (
            "claude", "/home/foo", True,
            "cd /home/foo && claude --resume sess-123 "
            "--dangerously-skip-permissions",
        ),
        (
            "codex", "/srv/proj", False,
            "cd /srv/proj && codex resume sess-123",
        ),
        (
            "codex", "/srv/proj", True,
            "cd /srv/proj && codex resume sess-123 "
            "--yolo",
        ),
        # No cwd → no `cd …`, just the bare backend command.
        ("claude", None, False, "claude --resume sess-123"),
        ("claude", "", False, "claude --resume sess-123"),
        ("codex", None, True, "codex resume sess-123 "
                              "--yolo"),
        # Paths with spaces are shell-quoted so the `cd` line still works.
        (
            "claude", "/home/foo/my project", False,
            "cd '/home/foo/my project' && claude --resume sess-123",
        ),
    ],
)
def test_format_resume_command(
    backend: str, cwd, dangerous: bool, expected: str,
) -> None:
    assert format_resume_command(
        backend, "sess-123", cwd, dangerous=dangerous,
    ) == expected


@pytest.mark.parametrize("backend", ["pi", "opencode", "unknown", ""])
def test_format_resume_command_unsupported_returns_none(backend: str) -> None:
    assert format_resume_command(
        backend, "sess-123", "/tmp", dangerous=False,
    ) is None


def test_format_resume_command_empty_session_returns_none() -> None:
    assert format_resume_command(
        "claude", "", "/tmp", dangerous=True,
    ) is None


@pytest.mark.parametrize(
    ("backend", "session_id", "expected"),
    [
        ("codex", "codex_019e18ae-1445-7911-83bc-d28b0a13d705",
         "codex resume 019e18ae-1445-7911-83bc-d28b0a13d705"),
        ("claude", "claude_019e18ae-1445-7911-83bc-d28b0a13d705",
         "claude --resume 019e18ae-1445-7911-83bc-d28b0a13d705"),
    ],
)
def test_format_resume_command_strips_harness_external_prefix(
    backend: str,
    session_id: str,
    expected: str,
) -> None:
    assert format_resume_command(
        backend, session_id, None, dangerous=False,
    ) == expected


# ---------------------------------------------------------------------------
# format_resume_block — fenced multi-line block ready for Channel Purpose
# ---------------------------------------------------------------------------


def test_format_resume_block_wraps_command_in_code_fence() -> None:
    block = format_resume_block(
        "claude", "sess-abc", "/home/foo", dangerous=False,
    )
    assert block == (
        "Resume:\n"
        "```\n"
        "cd /home/foo && claude --resume sess-abc\n"
        "```"
    )


def test_format_resume_block_includes_dangerous_flag() -> None:
    block = format_resume_block(
        "codex", "sess-xyz", "/srv", dangerous=True,
    )
    assert block == (
        "Resume:\n"
        "```\n"
        "cd /srv && codex resume sess-xyz "
        "--yolo\n"
        "```"
    )


def test_format_resume_block_unsupported_returns_none() -> None:
    assert format_resume_block(
        "pi", "sess-pi", "/srv", dangerous=False,
    ) is None


# ---------------------------------------------------------------------------
# merge_into_purpose — preserves config section, swaps trailing block
# ---------------------------------------------------------------------------


def test_merge_into_empty_purpose_writes_block_after_separator() -> None:
    out = merge_into_purpose(
        "",
        "Resume:\n```\ncd /tmp && claude --resume s1\n```",
    )
    assert out == (
        "---\n"
        "Resume:\n"
        "```\n"
        "cd /tmp && claude --resume s1\n"
        "```"
    )


def test_merge_into_config_only_purpose_appends_block() -> None:
    out = merge_into_purpose(
        "claude, opus, autorespond",
        "Resume:\n```\ncd /tmp && claude --resume s1\n```",
    )
    assert out == (
        "claude, opus, autorespond\n"
        "\n"
        "---\n"
        "\n"
        "Resume:\n"
        "```\n"
        "cd /tmp && claude --resume s1\n"
        "```"
    )


def test_merge_into_purpose_replaces_stale_resume_block() -> None:
    existing = (
        "claude, opus\n"
        "\n"
        "---\n"
        "\n"
        "Resume:\n```\ncd /old && claude --resume old\n```"
    )
    out = merge_into_purpose(
        existing,
        "Resume:\n```\ncd /new && claude --resume new\n```",
    )
    assert out == (
        "claude, opus\n"
        "\n"
        "---\n"
        "\n"
        "Resume:\n```\ncd /new && claude --resume new\n```"
    )


def test_merge_into_purpose_none_strips_existing_resume_block() -> None:
    """Unsupported backend → caller passes None → we keep only the config."""
    existing = (
        "claude, opus\n"
        "\n"
        "---\n"
        "\n"
        "Resume:\n```\ncd /old && claude --resume old\n```"
    )
    assert merge_into_purpose(existing, None) == "claude, opus"


def test_merge_into_purpose_none_with_config_only_is_no_op() -> None:
    assert merge_into_purpose("claude, opus", None) == "claude, opus"


# ---------------------------------------------------------------------------
# normalize_backend — accepts purpose tokens, canon names, SSE display strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("claude", "claude"),
        ("Claude Code", "claude"),
        ("claude-code", "claude"),
        ("claudecode", "claude"),
        ("codex", "codex"),
        ("Codex", "codex"),
    ],
)
def test_normalize_backend_known_aliases(alias: str, expected: str) -> None:
    assert normalize_backend(alias) == expected


@pytest.mark.parametrize("alias", ["pi", "opencode", "", None, "unknown"])
def test_normalize_backend_unknown_returns_none(alias) -> None:
    assert normalize_backend(alias) is None
