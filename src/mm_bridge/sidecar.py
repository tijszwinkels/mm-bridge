"""Session-id sidecar files.

Mirrors the in-memory `session_id → channel_id` mapping to disk so that a
Claude Code session can self-identify as "running inside a Mattermost
channel" by checking for the presence of its sidecar file.

The canonical location is `~/.mm-bridge/sessions/<session_id>`. Each file
holds exactly one line — the Mattermost channel_id — with owner-only
permissions (0600) inside an owner-only directory (0700) since the channel
IDs are mildly sensitive.
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


def write(directory: Path, session_id: str, channel_id: str) -> None:
    """Write `channel_id` into `<directory>/<session_id>` (0600, overwrite)."""
    if not session_id or not channel_id:
        return
    try:
        _ensure_dir(directory)
        path = directory / session_id
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, channel_id.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        logger.warning(
            "Failed to write sidecar for session %s", session_id[:8], exc_info=True,
        )


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


def reconcile(directory: Path, mapping: dict[str, str]) -> None:
    """Make disk state match `mapping` (session_id → channel_id).

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
        write(directory, sid, mapping[sid])
