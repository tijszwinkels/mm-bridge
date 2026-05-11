"""Channel-Purpose resume command helpers.

Each Mattermost channel bound to a VibeDeck session gets a copy-pasteable
``cd <cwd> && <backend> --resume <id>`` command written into its Channel
Purpose (trailing section, after the section separator defined in
:mod:`mm_bridge.purpose`). The bridge daemon's
``Config.dangerous_permissions`` controls whether the elevated-permission
flag is appended so the resumed local session matches the daemon's
permission level.

Spec: ``specs/20260511-resume-purpose-with-cwd/{requirements,design}.md``

All helpers here are pure — no MM/VD client imports — so they are
trivially covered by ``tests/test_resume_header.py``.
"""

from __future__ import annotations

import shlex

from . import purpose


RESUME_BLOCK_HEADING = "Resume:"
_CODE_FENCE = "```"

_RESUME_CMD_BY_BACKEND: dict[str, str] = {
    "claude": "claude --resume {sid}",
    "codex": "codex resume {sid}",
}

_DANGEROUS_FLAG_BY_BACKEND: dict[str, str] = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
}

_BACKEND_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claudecode": "claude",
    "claude-code": "claude",
    "claude code": "claude",
    "codex": "codex",
}


def normalize_backend(name: str | None) -> str | None:
    """Return the formatter token for `name`, or None if unsupported.

    Accepts the lowercase purpose tokens (`claude`, `codex`), the
    canonical-form output of ``vd_client.canon_backend`` (`claudecode`),
    and raw SSE display strings (`Claude Code`, `Codex`). Empty/unknown
    inputs return None so callers can feed it directly into
    :func:`format_resume_command`.
    """
    if not name:
        return None
    return _BACKEND_ALIASES.get(name.strip().lower())


def format_resume_command(
    backend: str,
    session_id: str,
    cwd: str | None,
    *,
    dangerous: bool,
) -> str | None:
    """Return the bare CLI command, or None for unsupported inputs.

    With a non-empty ``cwd``, the output is prefixed by
    ``cd <quoted-cwd> && `` so the operator lands in the right directory
    before resuming. Paths are shell-quoted via :mod:`shlex` so spaces and
    metacharacters survive copy-paste.
    """
    if not session_id:
        return None
    template = _RESUME_CMD_BY_BACKEND.get(backend)
    if template is None:
        return None
    cmd = template.format(sid=session_id)
    if dangerous:
        cmd = f"{cmd} {_DANGEROUS_FLAG_BY_BACKEND[backend]}"
    if cwd:
        cmd = f"cd {shlex.quote(cwd)} && {cmd}"
    return cmd


def format_resume_block(
    backend: str,
    session_id: str,
    cwd: str | None,
    *,
    dangerous: bool,
) -> str | None:
    """Return the heading + fenced command block ready for Channel Purpose.

    Shape::

        Resume:
        ```
        cd <cwd> && <backend> --resume <id> [--dangerous-flag]
        ```

    Mattermost renders triple-backtick fences as a code block in the
    channel-info panel, giving operators a one-click copy. Returns None
    when the backend has no resume command or ``session_id`` is empty.
    """
    cmd = format_resume_command(backend, session_id, cwd, dangerous=dangerous)
    if cmd is None:
        return None
    return f"{RESUME_BLOCK_HEADING}\n{_CODE_FENCE}\n{cmd}\n{_CODE_FENCE}"


def merge_into_purpose(existing: str, resume_block: str | None) -> str:
    """Combine an existing Channel Purpose with a fresh resume block.

    Strategy: split ``existing`` on the section separator defined in
    :mod:`mm_bridge.purpose`, keep the config part untouched, and stitch
    the new ``resume_block`` in as the trailing section. Passing
    ``resume_block=None`` strips any existing trailing section and returns
    only the config part — used when the bound session's backend has no
    resume command (the operator's other Purpose content stays intact).
    """
    config_section, _existing_block = purpose.split_config_section(existing)
    return purpose.join_sections(config_section, resume_block or "")
