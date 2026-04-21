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


def read(directory: Path, session_id: str) -> tuple[str, str | None] | None:
    """Return ``(channel_id, root_id)`` for `session_id`, or ``None``.

    A missing file, empty file, or blank channel_id all yield ``None``.
    A missing or blank second line yields ``root_id = None`` so legacy
    single-line sidecars round-trip to channel-level anchors.
    """
    if not session_id:
        return None
    path = directory / session_id
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "Failed to read sidecar for session %s", session_id[:8], exc_info=True,
        )
        return None
    lines = text.splitlines()
    channel_id = lines[0].strip() if lines else ""
    if not channel_id:
        return None
    root_id = lines[1].strip() if len(lines) >= 2 and lines[1].strip() else None
    return channel_id, root_id


def delete(directory: Path, session_id: str) -> None:
    """Remove the sidecar for `session_id`, if it exists."""
    if not session_id:
        return
    try:
        (directory / session_id).unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "Failed to delete sidecar for session %s", session_id[:8], exc_info=True,
        )


def reconcile(
    directory: Path,
    mapping: dict[str, tuple[str, str | None]],
) -> None:
    """Make disk state match `mapping` (session_id → (channel_id, root_id)).

    Writes sidecars for sessions in the mapping that don't have one on disk
    and removes any files in the directory that don't correspond to a
    current mapping entry. Failures are logged but don't raise.
    """
    try:
        _ensure_dir(directory)
    except OSError:
        logger.warning(
            "Could not prepare sidecar dir %s", directory, exc_info=True,
        )
        return

    try:
        existing = {p.name for p in directory.iterdir() if p.is_file()}
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
