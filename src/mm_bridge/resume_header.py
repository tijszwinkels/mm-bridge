"""Channel-header resume command helpers.

Each Mattermost channel bound to a VibeDeck session gets a copy-pasteable
``Resume: <cmd>`` line written into its Header field. The CLI shape
depends on the backend; when the bridge daemon is configured for
elevated permissions, the matching dangerous-permission flag is
appended so a resumed local session matches the daemon's permission
level.

Spec: ``specs/20260508-channel-header-resume-command/{requirements,design}.md``

All helpers here are pure — no MM/VD client imports — so they are
trivially covered by ``tests/test_resume_header.py``.
"""

from __future__ import annotations


RESUME_PREFIX = "Resume: "

_RESUME_CMD_BY_BACKEND: dict[str, str] = {
    "claude": "claude --resume {sid}",
    "codex": "codex resume {sid}",
}

_DANGEROUS_FLAG_BY_BACKEND: dict[str, str] = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
}

# Accepted aliases for each formatter backend token. Both the lowercase
# Channel-Purpose token (`claude`, `codex`) and `vd_client.canon_backend`'s
# output (`claudecode`, `codex`) — plus a few raw SSE display names — map
# to a single formatter token here so callers don't have to canonicalise
# at every entrypoint.
_BACKEND_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claudecode": "claude",
    "claude-code": "claude",
    "claude code": "claude",
    "codex": "codex",
}


def normalize_backend(name: str | None) -> str | None:
    """Return the formatter token for `name`, or None if unsupported.

    Accepts the lowercase purpose tokens, the canonical-form output of
    ``vd_client.canon_backend``, and raw SSE display strings like
    ``"Claude Code"``. Empty/unknown inputs return None so callers can
    feed it directly into :func:`format_resume_command`.
    """
    if not name:
        return None
    return _BACKEND_ALIASES.get(name.strip().lower())


def format_resume_command(
    backend: str,
    session_id: str,
    *,
    dangerous: bool,
) -> str | None:
    """Return the bare CLI command for `(backend, session_id)`, or None.

    Returns None for unsupported backends and for an empty ``session_id``
    so callers can pass through without a special-case skip.
    """
    if not session_id:
        return None
    template = _RESUME_CMD_BY_BACKEND.get(backend)
    if template is None:
        return None
    cmd = template.format(sid=session_id)
    if dangerous:
        cmd = f"{cmd} {_DANGEROUS_FLAG_BY_BACKEND[backend]}"
    return cmd


def format_resume_line(
    backend: str,
    session_id: str,
    *,
    dangerous: bool,
) -> str | None:
    """Return ``Resume: <cmd>`` or None if no command is available."""
    cmd = format_resume_command(backend, session_id, dangerous=dangerous)
    if cmd is None:
        return None
    return f"{RESUME_PREFIX}{cmd}"


def merge_into_header(existing: str, resume_line: str | None) -> str:
    """Combine an existing channel header with a fresh resume line.

    Behaviour:

    * ``resume_line is None`` → return ``existing`` unchanged. This keeps
      operator-set content intact when the backend has no resume command
      (US-2.5).
    * Otherwise split ``existing`` on ``\\n``, strip per-line whitespace,
      drop empty lines and any prior ``Resume:`` line, append the new
      ``resume_line``, and re-join with ``\\n``.

    The split/strip is tolerant of operator edits (e.g. extra spaces
    around lines) without losing siblings like ``Parent: ~channel~`` or
    free-form notes.
    """
    if resume_line is None:
        return existing

    kept: list[str] = []
    for raw in existing.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(RESUME_PREFIX):
            continue
        kept.append(line)
    kept.append(resume_line)
    return "\n".join(kept)
