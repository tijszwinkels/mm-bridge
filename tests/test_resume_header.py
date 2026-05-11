"""Tests for channel-header resume command helpers."""

from __future__ import annotations

import pytest

from mm_bridge.resume_header import (
    RESUME_PREFIX,
    format_resume_command,
    format_resume_line,
    merge_into_header,
    normalize_backend,
)


@pytest.mark.parametrize(
    ("backend", "dangerous", "expected"),
    [
        ("claude", False, "claude --resume sess-123"),
        (
            "claude",
            True,
            "claude --resume sess-123 --dangerously-skip-permissions",
        ),
        ("codex", False, "codex resume sess-123"),
        (
            "codex",
            True,
            "codex resume sess-123 --dangerously-bypass-approvals-and-sandbox",
        ),
    ],
)
def test_format_resume_command_known_backends(
    backend: str, dangerous: bool, expected: str,
) -> None:
    assert format_resume_command(
        backend, "sess-123", dangerous=dangerous,
    ) == expected


@pytest.mark.parametrize("backend", ["pi", "opencode", "unknown", ""])
def test_format_resume_command_unsupported_backends_return_none(
    backend: str,
) -> None:
    assert format_resume_command(backend, "sess-123", dangerous=False) is None


def test_format_resume_command_empty_session_returns_none() -> None:
    assert format_resume_command("claude", "", dangerous=True) is None


def test_format_resume_line_uses_stable_prefix() -> None:
    assert RESUME_PREFIX == "Resume: "
    assert (
        format_resume_line("codex", "sess-123", dangerous=False)
        == "Resume: codex resume sess-123"
    )


@pytest.mark.parametrize(
    ("existing", "resume_line", "expected"),
    [
        ("", "Resume: claude --resume s1", "Resume: claude --resume s1"),
        (
            "Parent: ~impl~",
            "Resume: claude --resume s1",
            "Parent: ~impl~\nResume: claude --resume s1",
        ),
        (
            "Parent: ~impl~\nResume: claude --resume old",
            "Resume: claude --resume new",
            "Parent: ~impl~\nResume: claude --resume new",
        ),
        (
            "Resume: claude --resume old",
            "Resume: codex resume new",
            "Resume: codex resume new",
        ),
        (
            "Owner: Tijs\nParent: ~impl~",
            "Resume: claude --resume s1",
            "Owner: Tijs\nParent: ~impl~\nResume: claude --resume s1",
        ),
        (
            " Owner: Tijs \n Resume: claude --resume old ",
            "Resume: claude --resume new",
            "Owner: Tijs\nResume: claude --resume new",
        ),
    ],
)
def test_merge_into_header_adds_or_replaces_resume_line(
    existing: str, resume_line: str, expected: str,
) -> None:
    assert merge_into_header(existing, resume_line) == expected


def test_merge_into_header_none_leaves_header_unchanged() -> None:
    existing = "Owner: Tijs\nResume: claude --resume old"
    assert merge_into_header(existing, None) == existing


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("claude", "claude"),
        ("Claude Code", "claude"),
        ("claude-code", "claude"),
        ("claudecode", "claude"),  # vd_client.canon_backend output
        ("codex", "codex"),
        ("Codex", "codex"),
    ],
)
def test_normalize_backend_known_aliases(alias: str, expected: str) -> None:
    assert normalize_backend(alias) == expected


@pytest.mark.parametrize("alias", ["pi", "opencode", "", None, "unknown"])
def test_normalize_backend_unknown_returns_none(alias) -> None:
    assert normalize_backend(alias) is None
