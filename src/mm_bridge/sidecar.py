"""Session-id sidecar files.

Mirrors the in-memory `session_id → Anchor(channel_id, root_id?)` mapping
to disk so that a Claude Code session can self-identify as "running
inside a Mattermost channel (or thread fork)" by reading its sidecar.

The canonical location is `~/.mm-bridge/sessions/<session_id>`. The file
has one or two lines, with owner-only permissions (0600) inside an
owner-only directory (0700):

* Line 1 (always): the Mattermost channel_id.
* Line 2 (thread-fork sessions only): the root post id of the thread.

Old single-line files written before the two-line format are still read
correctly as channel-level anchors.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_DIR = Path.home() / ".mm-bridge" / "sessions"


def _ensure_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        logger.debug("Could not chmod sidecar dir %s", directory, exc_info=True)


def _dashed_alias(session_id: str) -> str | None:
    """Return the dashed-UUID alias filename for a canonical ``ses_<32hex>`` id.

    Claude Code sub-sessions see ``$CLAUDE_SESSION_ID`` as a dashed UUID
    (``<8>-<4>-<4>-<4>-<12>``) while the harness writes sidecars under the
    canonical ``ses_<32hex>`` form. A symlink at the dashed-UUID path lets
    the literal ``test -f ~/.mm-bridge/sessions/$CLAUDE_SESSION_ID`` check
    pass directly, without going through the read-time fallback in
    ``read()``.

    Returns the dashed alias when ``session_id`` is exactly
    ``ses_<32 lowercase hex chars>``. Returns ``None`` for any other shape
    (non-``ses_`` prefix, wrong hex length, non-hex / uppercase chars) —
    those ids already are or already include a unique on-disk filename.
    """
    if not session_id.startswith("ses_"):
        return None
    hex_part = session_id[4:]
    if len(hex_part) != 32:
        return None
    # int(..., 16) accepts both cases; require lowercase explicitly so we
    # don't alias a mixed-case canonical that doesn't actually exist on disk.
    if hex_part != hex_part.lower():
        return None
    try:
        int(hex_part, 16)
    except ValueError:
        return None
    return (
        f"{hex_part[:8]}-{hex_part[8:12]}-{hex_part[12:16]}"
        f"-{hex_part[16:20]}-{hex_part[20:]}"
    )


def _write_dashed_alias(directory: Path, session_id: str) -> None:
    """Create/refresh `<directory>/<dashed-uuid>` → `<session_id>` symlink.

    Idempotent: a correct symlink is left alone; a wrong symlink or a
    regular file at the alias path is replaced. Uses a relative target
    (the bare canonical filename) so the alias survives moves of the
    parent directory. Best-effort — OSErrors are logged, never raised.
    """
    alias = _dashed_alias(session_id)
    if alias is None:
        return
    alias_path = directory / alias
    try:
        if alias_path.is_symlink():
            try:
                current = os.readlink(str(alias_path))
            except OSError:
                current = None
            if current == session_id:
                logger.debug(
                    "Sidecar alias already correct for %s", session_id[:8],
                )
                return
            alias_path.unlink()
        elif alias_path.exists():
            # Defensive: a stale regular file at the alias path.
            alias_path.unlink()
        os.symlink(session_id, str(alias_path))
    except OSError:
        logger.warning(
            "Failed to create sidecar alias for session %s",
            session_id[:8],
            exc_info=True,
        )


def _delete_dashed_alias(directory: Path, session_id: str) -> None:
    """Remove `<directory>/<dashed-uuid>` if `session_id` has one. Best-effort."""
    alias = _dashed_alias(session_id)
    if alias is None:
        return
    try:
        (directory / alias).unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Failed to delete sidecar alias for session %s",
            session_id[:8],
            exc_info=True,
        )


def write(
    directory: Path,
    session_id: str,
    channel_id: str,
    root_id: str | None = None,
) -> None:
    """Write `<directory>/<session_id>` (0600, overwrite).

    Channel-level sessions get a single line (``channel_id``). Thread-fork
    sessions get two lines (``channel_id\\nroot_id``). Passing an empty
    ``session_id`` or ``channel_id`` is a no-op.

    For canonical ``ses_<32hex>`` ids, also creates a dashed-UUID symlink
    at `<directory>/<dashed-uuid>` pointing to `<session_id>`, so the
    literal ``test -f ~/.mm-bridge/sessions/$CLAUDE_SESSION_ID`` check
    from inside a spawned Claude Code session passes directly.
    """
    if not session_id or not channel_id:
        return
    payload = channel_id if not root_id else f"{channel_id}\n{root_id}"
    try:
        _ensure_dir(directory)
        path = directory / session_id
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        logger.warning(
            "Failed to write sidecar for session %s", session_id[:8], exc_info=True,
        )
        return
    _write_dashed_alias(directory, session_id)


def read(directory: Path, session_id: str) -> tuple[str, str | None] | None:
    """Return ``(channel_id, root_id)`` for `session_id`, or ``None``.

    A missing file, empty file, or blank channel_id all yield ``None``.
    A missing or blank second line yields ``root_id = None`` so legacy
    single-line sidecars round-trip to channel-level anchors.

    Claude sub-sessions see ``CLAUDE_SESSION_ID`` as a dashed UUID
    (8-4-4-4-12) but the harness writes sidecars under the canonical
    ``ses_<32hex>`` form. When the literal lookup misses we also try
    that canonical form, so the bridge resolves either spelling. The
    fallback only fires when the literal file is absent — codex and
    pre-existing exact-match ids stay unchanged.
    """
    if not session_id:
        return None
    text = _read_text_or_none(directory / session_id, session_id)
    if text is None and not session_id.startswith("ses_"):
        canonical = f"ses_{session_id.replace('-', '')}"
        if canonical != session_id:
            text = _read_text_or_none(directory / canonical, session_id)
    if text is None:
        return None
    lines = text.splitlines()
    channel_id = lines[0].strip() if lines else ""
    if not channel_id:
        return None
    root_id = lines[1].strip() if len(lines) >= 2 and lines[1].strip() else None
    return channel_id, root_id


def canonical_id(directory: Path, session_id: str) -> str | None:
    """Return the canonical on-disk session id for `session_id`, or ``None``.

    The "canonical" id is the real sidecar filename — which is the HARNESS
    session id the bridge stores in its anchor mapping. A caller that needs to
    compare a locally-resolved session id against that mapping (e.g. self-post
    loop-back suppression) must canonicalise first: a claude sub-session looks
    itself up by the dashed ``CLAUDE_SESSION_ID`` UUID, which ``write()`` only
    records as a *symlink alias* pointing at the canonical ``ses_<32hex>`` file.

    Resolution mirrors ``read()``:
      * a dashed-UUID alias (symlink) → its ``ses_<hex>`` target;
      * a real file at the literal id → already canonical (``ses_`` / ``codex_``
        / any harness id written directly);
      * a dashed UUID with no alias but a real ``ses_<32hex>`` file present →
        that ``ses_`` id (matches ``read()``'s dashed→canonical fallback).
    Returns ``None`` when nothing resolves, so callers can omit the marker.
    """
    if not session_id:
        return None
    path = directory / session_id
    if path.is_symlink():
        try:
            target = os.readlink(str(path))
        except OSError:
            target = None
        if target:
            return os.path.basename(target)
        # Unreadable symlink: fall through to reconstruction. Do NOT let the
        # is_file() branch below run — it follows the link and would return the
        # ALIAS name (the dashed UUID), the exact wrong value we're avoiding.
    else:
        try:
            if path.is_file():
                return session_id
        except OSError:
            pass
    if not session_id.startswith("ses_"):
        candidate = f"ses_{session_id.replace('-', '')}"
        if (directory / candidate).is_file():
            return candidate
    return None


def _read_text_or_none(path: Path, session_id: str) -> str | None:
    """Read *path* as UTF-8 text, returning ``None`` on missing/unreadable.

    Logs OSErrors (other than ``FileNotFoundError``) with a truncated
    session_id, matching the original error semantics.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "Failed to read sidecar for session %s", session_id[:8], exc_info=True,
        )
        return None


def delete(directory: Path, session_id: str) -> None:
    """Remove the sidecar for `session_id`, if it exists.

    Also removes the dashed-UUID alias symlink for canonical
    ``ses_<32hex>`` ids, if present.
    """
    if not session_id:
        return
    try:
        (directory / session_id).unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "Failed to delete sidecar for session %s", session_id[:8], exc_info=True,
        )
    _delete_dashed_alias(directory, session_id)


def reconcile(
    directory: Path,
    mapping: dict[str, tuple[str, str | None]],
) -> None:
    """Make disk state match `mapping` (session_id → (channel_id, root_id)).

    Writes sidecars for sessions in the mapping that don't have one on disk
    and removes any files in the directory that don't correspond to a
    current mapping entry. Failures are logged but don't raise.

    Symlinks (the dashed-UUID aliases produced by ``write()``) are
    reconcile-invariant: the existing-files probe skips them so they are
    never flagged as stale, and ``write()`` recreates them idempotently
    for every kept session. ``delete()`` removes the alias when its
    canonical session is dropped.
    """
    try:
        _ensure_dir(directory)
    except OSError:
        logger.warning(
            "Could not prepare sidecar dir %s", directory, exc_info=True,
        )
        return

    try:
        existing = {
            p.name
            for p in directory.iterdir()
            if p.is_file() and not p.is_symlink()
        }
    except OSError:
        logger.warning(
            "Could not read sidecar dir %s", directory, exc_info=True,
        )
        existing = set()

    desired = set(mapping.keys())
    for stale in existing - desired:
        delete(directory, stale)
    for sid in desired:
        channel_id, root_id = mapping[sid]
        write(directory, sid, channel_id, root_id)
