"""Best-effort lookup of a codex session_id from on-disk rollout files.

When mm-bridge runs from inside a codex tool shell, the shell's env
does not (always) carry the session_id — codex generates the UUID
post-launch, and unless the launcher pinned ``MM_BRIDGE_SESSION_ID``
into the shell-environment policy, there is no env-var to read.

Codex does, however, write each session's transcript to:

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO_TS>-<uuidv7>.jsonl

The first JSONL line is a ``session_meta`` record carrying
``payload.id`` (the session UUID) and ``payload.cwd`` (the directory
codex was launched in).

This module scans those files newest-first, parses each first line,
and returns the most recent session id whose ``cwd`` matches the
caller's. Scoping by cwd avoids picking up an unrelated codex session
that happens to be running on the same machine.

Pure stdlib; no ``/proc`` or process-tree dependency.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def _read_session_meta(rollout_path: Path) -> tuple[str, str] | None:
    """Return ``(session_id, cwd)`` from the first line of *rollout_path*.

    Returns ``None`` when the file is empty, the first line isn't valid
    JSON, isn't a ``session_meta`` record, or lacks the required
    ``payload.id`` / ``payload.cwd`` fields. Errors are logged at debug
    level so a malformed file never breaks the resolver.
    """
    try:
        with rollout_path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError as exc:
        logger.debug("codex_session: cannot read %s: %s", rollout_path, exc)
        return None
    if not first:
        return None
    try:
        record = json.loads(first)
    except json.JSONDecodeError as exc:
        logger.debug(
            "codex_session: first line of %s is not JSON: %s",
            rollout_path, exc,
        )
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    sid = payload.get("id")
    cwd = payload.get("cwd")
    if not isinstance(sid, str) or not isinstance(cwd, str):
        return None
    return sid, cwd


def find_session_id_by_cwd(
    cwd: str | os.PathLike[str],
    *,
    sessions_root: Path | str = _DEFAULT_SESSIONS_ROOT,
) -> str | None:
    """Return the session id of the most recent codex rollout in *cwd*.

    Iterates ``rollout-*.jsonl`` files under *sessions_root* in
    descending mtime order. For each file, parses the first line and
    compares ``payload.cwd`` against *cwd* (string-equality after
    ``os.fspath``). Returns the matching ``payload.id`` or ``None`` if
    no rollout matches.

    Returns ``None`` cleanly when *sessions_root* doesn't exist (no
    codex sessions on this machine yet).
    """
    root = Path(sessions_root)
    if not root.is_dir():
        return None
    target = os.fspath(cwd)

    rollouts: list[tuple[float, Path]] = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            logger.debug("codex_session: stat failed on %s: %s", path, exc)
            continue
        rollouts.append((mtime, path))
    rollouts.sort(key=lambda entry: entry[0], reverse=True)

    for _, path in rollouts:
        meta = _read_session_meta(path)
        if meta is None:
            continue
        sid, rollout_cwd = meta
        if rollout_cwd == target:
            return sid
    return None
