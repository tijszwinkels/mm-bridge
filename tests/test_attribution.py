"""Tests for the PosterTracker attribution helper."""

from __future__ import annotations

from mm_bridge.attribution import PosterTracker


def test_single_user_three_posts_never_attributed() -> None:
    tracker = PosterTracker()
    assert tracker.note_post("s1", "alice") is False
    assert tracker.note_post("s1", "alice") is False
    assert tracker.note_post("s1", "alice") is False


def test_second_distinct_poster_triggers_attribution_immediately() -> None:
    tracker = PosterTracker()
    assert tracker.note_post("s1", "alice") is False
    # B's *first* post already flips the switch.
    assert tracker.note_post("s1", "bob") is True


def test_attribution_stays_on_once_triggered() -> None:
    tracker = PosterTracker()
    assert tracker.note_post("s1", "alice") is False
    assert tracker.note_post("s1", "bob") is True
    # A posting again continues to be attributed.
    assert tracker.note_post("s1", "alice") is True


def test_same_user_five_posts_set_does_not_grow() -> None:
    tracker = PosterTracker()
    for _ in range(5):
        assert tracker.note_post("s1", "alice") is False


def test_per_session_isolation() -> None:
    tracker = PosterTracker()
    # Session 1: two posters, attribution active.
    assert tracker.note_post("s1", "alice") is False
    assert tracker.note_post("s1", "bob") is True
    # Session 2: only alice — still not attributed.
    assert tracker.note_post("s2", "alice") is False
    assert tracker.note_post("s2", "alice") is False
    # Session 1 remains attributed.
    assert tracker.note_post("s1", "alice") is True


def test_format_with_attribute_true_prepends_username() -> None:
    tracker = PosterTracker()
    assert tracker.format("hi", "alice", True) == "alice: hi"


def test_format_with_attribute_false_returns_text_unchanged() -> None:
    tracker = PosterTracker()
    assert tracker.format("hi", "alice", False) == "hi"


def test_forget_drops_session_state() -> None:
    tracker = PosterTracker()
    tracker.note_post("s1", "alice")
    tracker.note_post("s1", "bob")
    # Drop the session — a new note_post starts from scratch.
    tracker.forget("s1")
    assert tracker.note_post("s1", "alice") is False


def test_forget_unknown_session_is_noop() -> None:
    tracker = PosterTracker()
    # Must not raise.
    tracker.forget("never-seen")
    # And state remains usable.
    assert tracker.note_post("s1", "alice") is False
