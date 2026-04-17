"""Poster attribution tracking for the Mattermost <-> VibeDeck bridge.

When a VibeDeck session has only one human participant, forwarded posts
don't need a username prefix (it's obvious who wrote them). Once a second
distinct human speaks, every subsequent post in that session gets prefixed
with ``<username>: `` so readers can tell the posters apart.

See: specs/20260417-mattermost-bridge-v2/design.md section 4 and
     requirements section 11.1.
"""

from __future__ import annotations


class PosterTracker:
    """Tracks which user_ids have posted per session and decides when to
    prefix forwarded messages with a username.

    Rules:
      * The set of posters is append-only per session.
      * Attribution is enabled as soon as >=2 distinct posters exist, which
        means the *second* human's first post is already attributed (we add
        to the set before checking its size).
      * ``forget(session_id)`` drops tracking for a session (e.g. on leave
        or kick); after that, the next post starts from scratch.
    """

    def __init__(self) -> None:
        self._posters_by_session: dict[str, set[str]] = {}

    def note_post(self, session_id: str, user_id: str) -> bool:
        """Record that ``user_id`` posted in ``session_id``.

        Returns ``True`` if the forwarded post should be attributed
        (i.e. >=2 distinct posters including this one), ``False`` otherwise.
        """
        posters = self._posters_by_session.setdefault(session_id, set())
        posters.add(user_id)
        return len(posters) >= 2

    def format(self, text: str, username: str, attribute: bool) -> str:
        """Return ``text`` with a ``"<username>: "`` prefix when
        ``attribute`` is ``True``; otherwise return ``text`` unchanged."""
        return f"{username}: {text}" if attribute else text

    def forget(self, session_id: str) -> None:
        """Drop tracking for ``session_id``. No-op if unknown."""
        self._posters_by_session.pop(session_id, None)
