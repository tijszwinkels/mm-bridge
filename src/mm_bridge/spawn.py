"""Pure helpers for the `mm-bridge spawn` subcommand.

The CLI orchestration lives in ``cli.cmd_spawn``; this module just owns
the small string-formatting primitives so they're trivially unit-tested
without mocking Mattermost or VibeDeck.
"""

from __future__ import annotations


MM_DISPLAY_NAME_MAX = 64


def format_parent_header(
    parent_channel_name: str,
    thread_permalink: str | None = None,
) -> str:
    """Header for a spawned channel pointing back to its parent.

    Uses Mattermost's ``~channel-name~`` mention syntax so the header
    renders as a clickable link. When ``thread_permalink`` is supplied
    (spawn from a thread-fork session), a ``[thread](url)`` link is
    appended so the child can jump directly into the parent thread —
    the bare channel mention would only land at the channel root and
    lose the thread context.
    """
    base = f"Parent: ~{parent_channel_name}~"
    if thread_permalink:
        return f"{base} ([thread]({thread_permalink}))"
    return base


def build_mm_base_url(scheme: str, host: str, port: int) -> str:
    """Assemble ``scheme://host[:port]``, omitting default ports.

    Used as the fallback base for permalinks when ``mm_public_url`` is
    not configured.
    """
    default = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    host_port = host if default else f"{host}:{port}"
    return f"{scheme}://{host_port}"


def format_post_permalink(base_url: str, team: str, post_id: str) -> str:
    """Build a Mattermost post-permalink URL from a pre-resolved base.

    MM renders ``<base>/<team>/pl/<post_id>`` as a deep link that opens
    the post (and its thread, if any) in the web client. A trailing slash
    on ``base_url`` is tolerated so operators can set
    ``MM_PUBLIC_URL=http://host:port/`` without producing a doubled slash.
    """
    return f"{base_url.rstrip('/')}/{team}/pl/{post_id}"


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


def format_spawn_kickoff(parent_channel_name: str, prompt: str) -> str:
    """Kickoff message posted into a newly-spawned sub-channel.

    VD's initial prompt stays inside VibeDeck and never reaches MM, so
    without this post the new channel would appear empty until the
    backend emits its first assistant reply.
    """
    header = f":thread: Spawned from ~{parent_channel_name}~"
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
