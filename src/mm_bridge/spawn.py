"""Pure helpers for the `mm-bridge spawn` subcommand.

The CLI orchestration lives in ``cli.cmd_spawn``; this module just owns
the small string-formatting primitives so they're trivially unit-tested
without mocking Mattermost or the agent backend.
"""

from __future__ import annotations

from collections.abc import Mapping


MM_DISPLAY_NAME_MAX = 64

# Cap on the *rendered* forwarded-quote preview posted to the kickoff /
# announcement channels. Coding-agent briefs — now pipeable via
# ``mm-bridge spawn - <<'EOF'`` — get large, so this is generous. It caps
# the RENDERED blockquote (not the raw prompt) so the whole post stays
# under Mattermost's default ``MaxPostSize`` (16383 chars) regardless of
# the prompt's line shape: a brief that's mostly short lines would double
# in size from the ``> `` prefixes, and a raw-input cap couldn't bound
# that. The header and truncation marker add only ~120 chars on top,
# leaving comfortable headroom. The full prompt is always delivered to
# the sub-session via the harness, untouched — only this preview is
# clipped.
SPAWN_QUOTE_MAX_CHARS = 12000


def _quote_prompt(prompt: str) -> str:
    """Render *prompt* as a Markdown blockquote for a channel preview.

    The rendered quote is capped at :data:`SPAWN_QUOTE_MAX_CHARS` (trimmed
    to the last whole line) with a marker line, so the preview post never
    exceeds Mattermost's size limit regardless of the prompt's line shape.
    Truncation is cosmetic — it clips only this quote, never the prompt
    delivered to the sub-session. Callers guarantee *prompt* is non-blank
    (blank prompts skip the quote entirely).
    """
    quoted = "\n".join(f"> {line}" for line in prompt.splitlines())
    if len(quoted) <= SPAWN_QUOTE_MAX_CHARS:
        return quoted
    clipped = quoted[:SPAWN_QUOTE_MAX_CHARS]
    # Prefer a clean cut at the last complete quoted line; fall back to the
    # hard character cut when the first line alone already overflows.
    last_nl = clipped.rfind("\n")
    if last_nl > 0:
        clipped = clipped[:last_nl]
    return (
        f"{clipped}\n>\n> _… quote truncated for preview ({len(prompt)} "
        f"chars); full prompt delivered to the sub-session._"
    )


def build_spawn_child_env(
    parent_env: Mapping[str, str],
    new_session_id: str,
    backend: str,
) -> dict[str, str]:
    """Return the env-overlay a spawned sub-session's process tree should
    inherit, given the parent shell's *parent_env*.

    The overlay achieves two things:

    1. Pins ``MM_BRIDGE_SESSION_ID`` to the *new_session_id* so any
       ``mm-bridge`` tool-shell call inside the child resolves to the
       child's own bridge sidecar — not the parent's. This is the
       explicit, backend-agnostic contract documented in the resolver.
    2. Strips ``CLAUDE_SESSION_ID`` from the inherited env. Both
       backends need this: for the **codex** path, the inherited value
       would poison the resolver (codex has no SessionStart hook to
       overwrite it). For the **claude** path, Claude Code's own
       SessionStart hook (``~/.claude/hooks/export-session-id.sh``)
       writes the correct dashed UUID on first tool invocation — but
       until then, the child env shouldn't carry the parent's. The
       cost of unsetting is negligible; the cost of keeping it is the
       cross-channel-agentcom drop we're closing.

    Returns the **overlay** (not the full merged env): the keys to set
    are present with their target values, and keys to unset are present
    with empty-string values. Callers that hand the result to a
    ``subprocess`` family can iterate the items, applying ``os.environ |
    overlay`` and then filtering empty values out — or pass the overlay
    verbatim to a service that supports an "unset" sentinel. The pair
    ``(MM_BRIDGE_SESSION_ID=<id>, CLAUDE_SESSION_ID="")`` documents both
    sides of the contract symmetrically.

    *backend* is accepted today purely for the documented contract:
    both ``"claude"`` and ``"codex"`` get the same treatment. The
    parameter keeps the door open for backend-specific tweaks (e.g.
    pinning a codex-rollout path) without breaking the call site.
    """
    del backend  # uniform treatment today; kept for future backend tweaks
    overlay: dict[str, str] = {}
    if not new_session_id:
        raise ValueError("new_session_id must be non-empty")
    overlay["MM_BRIDGE_SESSION_ID"] = new_session_id
    # Explicit unset signal: empty string conveys "remove this from the
    # child env" to any caller that respects the convention. We include
    # the key even when the parent didn't have CLAUDE_SESSION_ID set —
    # the contract is symmetric, and a missing key on the parent side
    # is indistinguishable from a present-but-empty one to a child that
    # only looks at ``os.environ.get(...)``.
    overlay["CLAUDE_SESSION_ID"] = ""
    del parent_env  # currently unused; reserved for future env carry-over
    return overlay


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
    return f"{header}\n\n{_quote_prompt(prompt)}"


def format_spawn_kickoff(parent_channel_name: str, prompt: str) -> str:
    """Kickoff message posted into a newly-spawned sub-channel.

    The backend's initial prompt stays inside the backend and never reaches
    MM, so without this post the new channel would appear empty until the
    backend emits its first assistant reply.
    """
    header = f":thread: Spawned from ~{parent_channel_name}~"
    if not prompt.strip():
        return header
    return f"{header}\n\n{_quote_prompt(prompt)}"


def derive_display_name(title: str | None, fallback: str) -> str:
    """Resolve the ``--title`` argument or fall back to a default.

    The fallback is used when ``title`` is None or blank; both the title
    and fallback are truncated to Mattermost's display_name limit.
    """
    if title and title.strip():
        return title.strip()[:MM_DISPLAY_NAME_MAX]
    return fallback[:MM_DISPLAY_NAME_MAX]
