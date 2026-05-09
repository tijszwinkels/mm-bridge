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

This module exposes two complementary resolvers:

* :func:`iter_session_ids_by_cwd` / :func:`find_session_id_by_cwd` —
  scan rollout files newest-mtime first and yield candidates whose
  recorded ``cwd`` matches the caller's. Pure stdlib; works whether or
  not the originating codex process is still alive. Ordering is by
  rollout mtime, which routes "most recently active" first.
* :func:`find_active_codex_rollout_uuid` — a Linux ``/proc`` tie-breaker
  that walks the caller's parent-pid chain looking for a live codex
  process and returns the UUID of the rollout file it currently has
  open. Use this BEFORE the cwd-mtime walk to disambiguate when
  multiple codex sessions share a cwd: only the one whose process is
  actually in our ancestor chain is the one we belong to. Returns
  ``None`` cleanly on macOS / non-``/proc`` systems and when no codex
  ancestor exists (background tasks, shells that outlive their codex
  parent), letting callers fall through to mtime ordering.

Ordering note: sorting by file mtime means "most recently active"
session wins, not "most recently created". For our use case (resolving
which codex session a tool shell belongs to), recently active is the
right answer — the session that's currently executing tool calls is
the one whose rollout was just appended to. The two notions only
diverge when multiple codex sessions share a cwd; in that case the
PPid tie-breaker (or the env-var resolvers earlier in the chain) picks
the right candidate. We deliberately do NOT sort on the recorded
``session_meta.timestamp`` because that's frozen at session-create
time and would route to a long-idle session over an active one.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

_DEFAULT_PROC_ROOT = Path("/proc")

_DEFAULT_MAX_DEPTH = 8

# RFC-4122-shaped UUID at the tail of a rollout filename. Codex names
# rollouts ``rollout-<ISO_TS>-<uuid>.jsonl``; we anchor at ``.jsonl`` so
# a hex-looking timestamp segment can't accidentally match.
_ROLLOUT_UUID_RE = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$",
)


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


def _read_proc_comm(proc_root: Path, pid: int) -> str | None:
    """Return ``/proc/<pid>/comm`` stripped of its trailing newline.

    Returns ``None`` if the file is missing or unreadable (process gone,
    permission error, ``/proc`` not present). Real ``/proc/<pid>/comm``
    is always one line ending in ``\\n``; the strip leaves ``"codex"``
    rather than ``"codex\\n"`` so equality checks work cleanly.
    """
    try:
        return (proc_root / str(pid) / "comm").read_text().rstrip("\n")
    except OSError as exc:
        logger.debug("codex_session: cannot read comm for pid=%s: %s", pid, exc)
        return None


def _read_proc_ppid(proc_root: Path, pid: int) -> int | None:
    """Return the parent pid recorded in ``/proc/<pid>/status`` or None.

    Walks the file looking for a ``PPid:\\t<num>`` line — that's the
    canonical layout on Linux. Returns ``None`` for missing entries,
    unreadable files, or values we can't parse as an int.
    """
    try:
        with (proc_root / str(pid) / "status").open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return int(parts[1])
                        except ValueError:
                            return None
                    return None
    except OSError as exc:
        logger.debug("codex_session: cannot read status for pid=%s: %s", pid, exc)
        return None
    return None


def _rollout_uuid_for_pid(
    proc_root: Path, pid: int, sessions_root: Path,
) -> str | None:
    """Find the rollout-file UUID held open by codex *pid*, or ``None``.

    Scans ``/proc/<pid>/fd/*`` symlinks for targets under *sessions_root*
    matching the ``rollout-*-<uuid>.jsonl`` filename shape. The
    sessions-root check keeps us from being fooled by a binary called
    ``codex`` with other unrelated files open.

    A single codex process can have **multiple** rollout fds open at
    the same time — codex's review/fork/subagent workflows keep the
    parent rollout open alongside the spawned subagent's. Returning the
    first fd ``iterdir()`` happens to yield is non-deterministic and
    routinely lands on the parent (wrong session). Resolve by picking
    the candidate whose rollout file has the newest mtime: codex
    appends to the active rollout on every event, so its mtime
    overtakes the parent's within microseconds of the subagent
    starting. Files that ``stat`` fails on (raced delete) sort below
    any real-mtime candidate.
    """
    fd_dir = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except OSError as exc:
        logger.debug("codex_session: cannot list fds for pid=%s: %s", pid, exc)
        return None

    sessions_root_str = _canonicalise(sessions_root)
    candidates: list[tuple[float, str]] = []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        target_canon = _canonicalise(target)
        if not target_canon.startswith(sessions_root_str + os.sep) \
                and target_canon != sessions_root_str:
            continue
        match = _ROLLOUT_UUID_RE.search(os.path.basename(target_canon))
        if not match:
            continue
        try:
            mtime = os.stat(target_canon).st_mtime
        except OSError as exc:
            logger.debug(
                "codex_session: cannot stat rollout %s for pid=%s: %s",
                target_canon, pid, exc,
            )
            mtime = 0.0
        candidates.append((mtime, match.group(1)))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def find_active_codex_rollout_uuid(
    *,
    starting_pid: int | None = None,
    proc_root: Path | str = _DEFAULT_PROC_ROOT,
    sessions_root: Path | str = _DEFAULT_SESSIONS_ROOT,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> str | None:
    """Walk the parent-pid chain for a live codex; return its rollout UUID.

    Linux-only tie-breaker for the cwd-mtime resolver. Starts from
    *starting_pid* (default ``os.getppid()``) and climbs at most
    *max_depth* hops. At each hop, reads ``/proc/<pid>/comm``; on the
    first ``codex`` it finds, scans that pid's open fds for a target
    under *sessions_root* matching the rollout-filename pattern. Returns
    the embedded session UUID, or ``None`` when:

    * ``proc_root`` doesn't exist (macOS / non-``/proc`` host),
    * no codex ancestor is found within *max_depth* hops,
    * the codex ancestor has no rollout fd held open (closed or never
      opened), or
    * the chain reaches PID 1 / 0 first.

    Returning ``None`` is a normal outcome — callers fall through to the
    cwd-mtime walk in those cases. We do NOT short-circuit on the first
    codex pid if it has no rollout fd: in case the user has nested
    codex-launching-codex, we keep walking past a fd-less codex until
    we find one with a real rollout. ``OSError`` on any individual /proc
    read is swallowed at debug level so a process disappearing mid-walk
    can't crash the resolver.
    """
    proc = Path(proc_root)
    if not proc.is_dir():
        return None

    sessions = Path(sessions_root)
    pid = os.getppid() if starting_pid is None else starting_pid

    for _ in range(max_depth):
        if pid <= 1:
            return None
        comm = _read_proc_comm(proc, pid)
        if comm is None:
            # /proc/<pid> disappeared (process exited) — chain is broken.
            return None
        if comm == "codex":
            uuid = _rollout_uuid_for_pid(proc, pid, sessions)
            if uuid is not None:
                return uuid
            # Codex with no usable rollout fd — keep walking in case a
            # higher-up codex (rare but possible) does have one.
        ppid = _read_proc_ppid(proc, pid)
        if ppid is None or ppid == pid:
            return None
        pid = ppid
    return None
