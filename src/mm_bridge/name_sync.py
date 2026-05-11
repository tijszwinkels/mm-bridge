"""Bidirectional name-sync debouncer for bridge-side rename flows.

When the bridge writes a rename to one side (e.g. MM), the remote server will
reflect that change back as a ``channel_updated`` event. Without a debounce,
the bridge would see its own write and sync it back to the other side,
causing a ping-pong loop. ``NameSync`` lets callers mark "I just wrote this"
so the reflected event can be ignored within a configurable time window.
"""

from __future__ import annotations

import time
from typing import Callable, Literal

Kind = Literal["mm", "vd"]


class NameSync:
    """Bidirectional sync with ping-pong debounce.

    Callers pass ``time_func`` (defaults to :func:`time.monotonic`) so tests
    can inject a fake clock.
    """

    def __init__(
        self,
        window_seconds: float = 10.0,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._debounce: dict[tuple[str, str], float] = {}
        self._window = window_seconds
        self._time = time_func

    def note_remote_update(self, kind: Kind, id_: str) -> None:
        """Record that we just wrote a rename to ``kind``/``id_``.

        The reflected WS/SSE event arriving within ``window_seconds`` should
        be ignored via :meth:`should_sync` returning ``False``.
        """
        self._debounce[(kind, id_)] = self._time()

    def should_sync(self, kind: Kind, id_: str) -> bool:
        """Return ``True`` if we should act on a rename event for ``kind``/``id_``.

        Returns ``False`` if we set it ourselves recently (within the window).
        Expired entries are cleared from the internal dict.
        """
        ts = self._debounce.get((kind, id_))
        if ts is None:
            return True
        if self._time() - ts > self._window:
            self._debounce.pop((kind, id_), None)
            return True
        return False
