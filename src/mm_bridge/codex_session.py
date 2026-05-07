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
and yields candidate session ids whose ``cwd`` matches the caller's.
Scoping by cwd avoids picking up an unrelated codex session that
happens to be running on the same machine; the *caller* is expected
to apply a further gate (typically: does a sidecar exist?) before
adopting a candidate, and to walk past candidates that fail the gate.

Pure stdlib; no ``/proc`` or process-tree dependency.

Ordering note: sorting by file mtime means "most recently active"
session wins, not "most recently created". For our use case (resolving
which codex session a tool shell belongs to), recently active is the
right answer — the session that's currently executing tool calls is
the one whose rollout was just appended to. The two notions only
diverge when multiple codex sessions share a cwd; in that case the
sidecar-existence gate (or the env-var resolvers earlier in the chain)
breaks the tie. We deliberately do NOT sort on the recorded
``session_meta.timestamp`` because that's frozen at session-create
time and would route to a long-idle session over an active one.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def _read_session_meta(rollout_path: Path) -> tuple[str, str] | None:
    """Return ``(session_id, cwd)`` from the first line of *rollout_path*.

    Returns ``None`` when the file is empty, the first line isn't valid
    JSON, isn't a ``session_meta`` record, or lacks the required
    ``payload.id`` / ``payload.cwd`` fields. Errors are logged at debug
    level so a malformed file (including one whose first line is being
    written right now) never breaks the resolver.
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


def _canonicalise(path: str | os.PathLike[str]) -> str:
    """Resolve *path* without requiring it to exist.

    Used on both sides of the cwd comparison so symlinked launches,
    trailing slashes, and ``./`` segments don't cause spurious misses.
    ``strict=False`` keeps us safe if the rollout's recorded cwd points
    at a directory that has since been removed.
    """
    try:
        return os.fspath(Path(path).resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        logger.debug("codex_session: cannot resolve %r: %s", path, exc)
        return os.fspath(path)


def iter_session_ids_by_cwd(
    cwd: str | os.PathLike[str],
    *,
    sessions_root: Path | str = _DEFAULT_SESSIONS_ROOT,
) -> Iterator[str]:
    """Yield session ids of codex rollouts in *cwd*, newest-mtime first.

    Iterates ``rollout-*.jsonl`` files under *sessions_root* in
    descending mtime order. Skips files whose first line isn't a valid
    ``session_meta`` record. Compares the recorded ``payload.cwd``
    against the caller's cwd after canonicalising both, so symlinked
    paths and ``./``-style spellings still match.

    Yields nothing when *sessions_root* doesn't exist (no codex
    sessions on this machine yet) or when no rollout's cwd matches.
    """
    root = Path(sessions_root)
    if not root.is_dir():
        return
    target = _canonicalise(cwd)

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
        if _canonicalise(rollout_cwd) == target:
            yield sid


def find_session_id_by_cwd(
    cwd: str | os.PathLike[str],
    *,
    sessions_root: Path | str = _DEFAULT_SESSIONS_ROOT,
) -> str | None:
    """Return the most recent cwd-matched session id, or ``None``.

    Convenience wrapper over :func:`iter_session_ids_by_cwd` for
    callers that want only the newest match. Most callers should use
    the iterator directly so they can fall through to older candidates
    when a gate (e.g. sidecar existence) rejects the newest one.
    """
    return next(iter_session_ids_by_cwd(cwd, sessions_root=sessions_root), None)
