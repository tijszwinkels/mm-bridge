"""Unit tests for the pure watchdog-notice formatters.

These derive the operator-facing wording from the harness event payload
(agent-harness v0.1.1 idle-watchdog spec) so the copy tracks the configured
thresholds instead of hardcoded minute counts.
"""

from mm_bridge import watchdog_notices as wn


class TestHumanizeSeconds:
    def test_minutes(self):
        assert wn.humanize_seconds(600) == "10 min"
        assert wn.humanize_seconds(3000) == "50 min"
        assert wn.humanize_seconds(5400) == "90 min"

    def test_whole_hours(self):
        assert wn.humanize_seconds(3600) == "1 hour"
        assert wn.humanize_seconds(7200) == "2 hours"

    def test_rounds_to_nearest_minute(self):
        # 605s → 10 min, 634s → 11 min (round-half handling not important).
        assert wn.humanize_seconds(605) == "10 min"

    def test_sub_minute_floors_to_one(self):
        # A notice must never claim "0 min".
        assert wn.humanize_seconds(30) == "1 min"


class TestIdleWarningNotice:
    def test_uses_idle_seconds_from_payload(self):
        msg = wn.idle_warning_notice({"idle_seconds": 605})
        assert "10 min" in msg
        assert msg.startswith("⏳")
        assert ".stop" in msg

    def test_falls_back_to_default_when_missing(self):
        msg = wn.idle_warning_notice({})
        # Default idle timeout is 600s → 10 min.
        assert "10 min" in msg


class TestMaxRuntimeWarningNotice:
    def test_derives_elapsed_and_cap(self):
        msg = wn.max_runtime_warning_notice(
            {"max_run_seconds": 3600, "seconds_until_kill": 600}
        )
        # elapsed = cap - lead = 3000s → 50 min; cap = 3600s → 1 hour.
        assert "50 min" in msg
        assert "1 hour" in msg
        assert ".stop" in msg
        # Nothing hardcoded — a different cap changes the numbers.
        assert "50/60" not in msg

    def test_non_default_cap(self):
        msg = wn.max_runtime_warning_notice(
            {"max_run_seconds": 7200, "seconds_until_kill": 600}
        )
        # elapsed = 6600s → 110 min; cap = 7200s → 2 hours.
        assert "110 min" in msg
        assert "2 hours" in msg

    def test_falls_back_to_defaults(self):
        msg = wn.max_runtime_warning_notice({})
        assert "50 min" in msg
        assert "1 hour" in msg


class TestExceededMaxRuntimeNotice:
    def test_mentions_cap_and_recovery(self):
        msg = wn.exceeded_max_runtime_notice({"max_run_seconds": 3600})
        assert msg.startswith("⚠️")
        assert "1 hour" in msg
        assert "new message" in msg

    def test_falls_back_to_default(self):
        msg = wn.exceeded_max_runtime_notice({})
        assert "1 hour" in msg


class TestIdleTimeoutNotice:
    def test_uses_configured_threshold_not_thirty(self):
        # ``idle_seconds`` on this event carries the CONFIGURED timeout.
        msg = wn.idle_timeout_notice({"idle_seconds": 600})
        assert "10 min" in msg
        assert "30 minutes" not in msg
        assert "inactivity" in msg
        assert "resume" in msg

    def test_falls_back_to_default(self):
        msg = wn.idle_timeout_notice({})
        assert "10 min" in msg

    def test_distinct_from_cap_kill(self):
        idle = wn.idle_timeout_notice({"idle_seconds": 600})
        cap = wn.exceeded_max_runtime_notice({"max_run_seconds": 3600})
        assert idle != cap
        # Idle-kill talks about inactivity / a possibly-incomplete reply; the
        # cap-kill reassures the session is fine.
        assert "inactivity" in idle
        assert "inactivity" not in cap
