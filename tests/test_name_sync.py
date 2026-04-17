"""Tests for the bidirectional name-sync debouncer."""

from __future__ import annotations

import pytest

from mm_bridge.name_sync import NameSync


@pytest.fixture
def clock():
    """Mutable fake clock: ``clock[0]`` holds current time in seconds."""
    return [1000.0]


@pytest.fixture
def sync(clock):
    return NameSync(window_seconds=10.0, time_func=lambda: clock[0])


def test_fresh_key_should_sync(sync):
    assert sync.should_sync("mm", "c1") is True
    assert sync.should_sync("vd", "c1") is True


def test_note_then_should_sync_within_window_returns_false(sync, clock):
    sync.note_remote_update("mm", "c1")
    # Still within the 10s window.
    clock[0] += 5.0
    assert sync.should_sync("mm", "c1") is False


def test_note_then_advance_past_window_returns_true_and_clears(sync, clock):
    sync.note_remote_update("mm", "c1")
    clock[0] += 10.01  # Past the window (strictly greater than 10s).
    assert sync.should_sync("mm", "c1") is True
    # Entry should have been cleared from internal dict.
    assert ("mm", "c1") not in sync._debounce


def test_boundary_exactly_at_window_still_debounced(sync, clock):
    """At exactly ``window_seconds``, the entry is still within the window.

    The implementation uses strict ``>``, so equal timestamps debounce.
    """
    sync.note_remote_update("mm", "c1")
    clock[0] += 10.0
    assert sync.should_sync("mm", "c1") is False


def test_kind_isolation_mm_does_not_affect_vd(sync):
    sync.note_remote_update("mm", "c1")
    assert sync.should_sync("vd", "c1") is True


def test_id_isolation_c1_does_not_affect_c2(sync):
    sync.note_remote_update("mm", "c1")
    assert sync.should_sync("mm", "c2") is True


def test_same_id_different_kinds_tracked_separately(sync, clock):
    sync.note_remote_update("mm", "X")
    clock[0] += 1.0
    sync.note_remote_update("vd", "X")

    # Both within their windows → both should debounce.
    assert sync.should_sync("mm", "X") is False
    assert sync.should_sync("vd", "X") is False

    # Advance past mm's window but not vd's.
    # mm was recorded at t=1000 (elapsed 1s so far); vd at t=1001.
    # Jump to mm elapsed = 10.02s, vd elapsed = 9.02s.
    clock[0] += 9.02
    assert sync.should_sync("mm", "X") is True  # mm expired (>10s).
    assert sync.should_sync("vd", "X") is False  # vd still within window.


def test_overwriting_extends_window(sync, clock):
    sync.note_remote_update("mm", "c1")
    clock[0] += 8.0
    # Re-record — resets the clock for this key.
    sync.note_remote_update("mm", "c1")
    # 8s after the *first* call (so would've expired at 10s from first),
    # but only 5s after the *second* call — should still debounce.
    clock[0] += 5.0
    assert sync.should_sync("mm", "c1") is False

    # Now 11s after the second call → expired.
    clock[0] += 6.01
    assert sync.should_sync("mm", "c1") is True


def test_default_window_is_ten_seconds():
    ns = NameSync()
    assert ns._window == 10.0


def test_default_time_func_is_monotonic():
    import time as _time

    ns = NameSync()
    assert ns._time is _time.monotonic


def test_custom_window(clock):
    ns = NameSync(window_seconds=2.0, time_func=lambda: clock[0])
    ns.note_remote_update("vd", "c1")
    clock[0] += 1.5
    assert ns.should_sync("vd", "c1") is False
    clock[0] += 1.0  # Total 2.5s elapsed.
    assert ns.should_sync("vd", "c1") is True
