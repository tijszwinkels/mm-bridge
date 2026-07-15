"""Dot-command parser + registry.

A *pure* module (no I/O, modelled on ``purpose.py``): it maps a Mattermost
message to a typed :class:`ParsedCommand`, and describes the known commands in
a registry the bridge dispatches from and ``.help`` renders.

Contract:
    * ``parse()`` returns ``None`` when the message is **not** a dot-command
      (after stripping an optional leading ``@claude`` / bot mention). The
      bridge forwards those to the agent as normal user turns.
    * ``parse()`` returns a :class:`ParsedCommand` when the (mention-stripped)
      message matches ``^\\.(\\w+)(\\s+(.*))?$`` case-insensitively. Known words
      carry their :class:`CommandSpec`; unknown dot-words carry ``spec=None``
      so the bridge can intercept them with an "unknown command — try `.help`"
      reply instead of forwarding them.

Execution lives in the bridge (it needs ``self.mm`` / ``self.harness`` /
``self.mapping``), exactly like the existing ``_run_stop_command`` handlers.

Spec: implementation plan (ig ...135aeb87), "Parser rules" / "Command semantics".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CommandSpec:
    """A known dot-command's metadata (used for dispatch + ``.help``)."""

    name: str
    usage: str
    summary: str
    # Session-scoped commands normally require a mapped session. The bridge
    # explicitly handles model/backend as pre-session configuration while a
    # channel is dormant; stop/status still reply "no session" there.
    session_scoped: bool = False


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed dot-command. ``spec is None`` marks an unknown dot-word."""

    name: str
    arg: str | None
    spec: CommandSpec | None

    @property
    def known(self) -> bool:
        return self.spec is not None


# Ordered registry — insertion order drives ``.help`` output. Phase 2/3
# commands (`.model`, `.models`, `.running`, `.sessions`, `.invite`) are
# appended in the follow-up PR.
_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("help", ".help", "Show this list of commands."),
    CommandSpec(
        "stop", ".stop", "Interrupt the running turn in this channel.",
        session_scoped=True,
    ),
    CommandSpec(
        "autorespond", ".autorespond [on|off]",
        "Reply to every message, or only when @mentioned (bare = toggle).",
    ),
    CommandSpec(
        "status", ".status", "Show the session, model and run status here.",
        session_scoped=True,
    ),
    CommandSpec(
        "model", ".model [<name>]",
        "Show/select the model (configures the next session if dormant).",
        session_scoped=True,
    ),
    CommandSpec(
        "backend", ".backend [<name>]",
        "Show/select the backend (resets the model; configures if dormant).",
        session_scoped=True,
    ),
    CommandSpec(
        "models", ".models",
        "List the available models for this channel's backend.",
    ),
    CommandSpec(
        "running", ".running",
        "List sessions with a run in flight right now.",
    ),
    CommandSpec(
        "sessions", ".sessions [N]",
        "List recent sessions across all agents (incl. terminal ones).",
    ),
    CommandSpec(
        "invite", ".invite <session-id>",
        "Get invited to a session's Mattermost channel (creating it if needed).",
    ),
)

REGISTRY: dict[str, CommandSpec] = {s.name: s for s in _SPECS}


# ``^\.(\w+)`` — a leading dot immediately followed by a word. The optional
# ``(?:\s+(.*))?`` captures a trailing argument. Not DOTALL: an argument stays
# on one logical line, matching how these are typed.
_COMMAND_RE = re.compile(r"^\.(\w+)(?:\s+(.*\S))?\s*$", re.IGNORECASE)

# A leading ``@name`` mention we may strip. ``[\w.\-]+`` covers usernames with
# dots/hyphens as Mattermost allows.
_MENTION_RE = re.compile(r"^@([\w.\-]+)\s+(.*)$", re.DOTALL)


def _strip_leading_mention(message: str, mentions: Iterable[str]) -> str:
    """Drop a single leading ``@<ours>`` mention, else return text unchanged.

    ``@claude`` is always recognised (the canonical bot handle used across the
    codebase); any configured bot username is added on top. A leading mention
    that isn't ours is left in place — the message then won't start with ``.``
    and ``parse`` returns ``None`` (forwarded), which is the safe default.
    """
    text = message.strip()
    names = {"claude"}
    names.update(m.lstrip("@").lower() for m in mentions if m)
    m = _MENTION_RE.match(text)
    if m and m.group(1).lower() in names:
        return m.group(2).strip()
    return text


def parse(message: str, *, mentions: Iterable[str] = ()) -> ParsedCommand | None:
    """Parse ``message`` into a :class:`ParsedCommand`, or ``None``.

    ``mentions`` is an optional iterable of extra bot usernames (with or
    without a leading ``@``) whose leading mention should be stripped in
    addition to ``@claude``.
    """
    text = _strip_leading_mention(message or "", mentions)
    m = _COMMAND_RE.match(text)
    if not m:
        return None
    name = m.group(1).lower()
    arg = m.group(2)
    arg = arg.strip() if arg else None
    return ParsedCommand(name=name, arg=arg or None, spec=REGISTRY.get(name))


def help_text() -> str:
    """Render the registry as a Mattermost-friendly command list."""
    lines = ["**Commands** — type these in-channel:"]
    for spec in REGISTRY.values():
        lines.append(f"• `{spec.usage}` — {spec.summary}")
    return "\n".join(lines)
