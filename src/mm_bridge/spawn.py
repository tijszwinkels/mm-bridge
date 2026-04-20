"""Pure helpers for the `mm-bridge spawn` subcommand.

The CLI orchestration lives in ``cli.cmd_spawn``; this module just owns
the small string-formatting primitives so they're trivially unit-tested
without mocking Mattermost or VibeDeck.
"""

from __future__ import annotations


MM_DISPLAY_NAME_MAX = 64


def format_parent_header(parent_channel_name: str) -> str:
    """Header for a spawned channel pointing back to its parent.

    Uses Mattermost's ``~channel-name~`` mention syntax so the header
    renders as a clickable link (behaviour varies by MM version — see
    the spec's "Open questions" section).
    """
    return f"Parent: ~{parent_channel_name}~"


def format_spawn_announcement(
    title: str, new_channel_name: str, prompt: str,
) -> str:
    """Message posted to the parent channel announcing a spawn.

    - ``title`` — the new channel's display_name (human-readable).
    - ``new_channel_name`` — the URL slug (used for ``~channel~`` mention).
    - ``prompt`` — the kicked-off prompt; quoted with ``> `` per line when
      non-empty, omitted entirely otherwise.
    """
    header = f":thread: Spawned **{title}** in ~{new_channel_name}~"
    if not prompt.strip():
        return header
    quoted = "\n".join(f"> {line}" for line in prompt.splitlines())
    return f"{header}\n\n{quoted}"


def derive_display_name(title: str | None, fallback: str) -> str:
    """Resolve the ``--title`` argument or fall back to a default.

    The fallback is used when ``title`` is None or blank; both the title
    and fallback are truncated to Mattermost's display_name limit.
    """
    if title and title.strip():
        return title.strip()[:MM_DISPLAY_NAME_MAX]
    return fallback[:MM_DISPLAY_NAME_MAX]
