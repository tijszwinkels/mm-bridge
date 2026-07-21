"""Unit tests for the shared backend-error message template + detail helpers.

These are the pure building blocks (no bridge/IO). The integration tests that
assert each failure path actually posts them live in
``test_backend_error_surfacing.py``.
"""
from __future__ import annotations

import unittest

from mm_bridge.backend_errors import (
    condense_error_detail,
    format_backend_error,
    run_failure_detail,
)


class FormatBackendErrorTests(unittest.TestCase):
    def test_message_leads_with_action_and_names_backend(self):
        msg = format_backend_error("start a session", "claude", "boom")
        self.assertTrue(msg.startswith(":warning:"))
        self.assertIn("I tried to start a session", msg)
        self.assertIn("`claude` backend", msg)
        # The concrete error detail is fenced so it renders as a code block.
        self.assertIn("boom", msg)
        self.assertIn("```", msg)

    def test_message_points_at_the_log_and_doctor(self):
        msg = format_backend_error("run your message", "codex", "kaboom")
        self.assertIn("bridge log", msg)
        self.assertIn("mm-bridge doctor", msg)

    def test_unknown_backend_still_grammatical(self):
        msg = format_backend_error("run your message", None, "kaboom")
        self.assertIn("I tried to run your message", msg)
        # No dangling backticks / "None" leaking into the sentence.
        self.assertNotIn("None", msg)
        self.assertNotIn("``", msg.split("```")[0])


class CondenseErrorDetailTests(unittest.TestCase):
    def test_single_line_passthrough(self):
        self.assertEqual(
            condense_error_detail("agent-harness POST /v1/sessions -> 500: boom"),
            "agent-harness POST /v1/sessions -> 500: boom",
        )

    def test_multiline_keeps_final_meaningful_line(self):
        raw = "Traceback (most recent call last):\n  File x\nRuntimeError: real cause"
        self.assertEqual(condense_error_detail(raw), "RuntimeError: real cause")

    def test_empty_yields_placeholder(self):
        out = condense_error_detail("")
        self.assertTrue(out.strip())

    def test_long_detail_is_truncated(self):
        out = condense_error_detail("x" * 5000, max_len=200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("…"))


class RunFailureDetailTests(unittest.TestCase):
    def test_error_with_type(self):
        detail = run_failure_detail(
            {"error": "No such file: claude", "error_type": "FileNotFoundError"}
        )
        self.assertIn("FileNotFoundError", detail)
        self.assertIn("No such file: claude", detail)

    def test_returncode_only(self):
        detail = run_failure_detail({"returncode": 2})
        self.assertIn("2", detail)
        self.assertIn("exit", detail.lower())

    def test_missing_fields_has_fallback(self):
        self.assertTrue(run_failure_detail({}).strip())


if __name__ == "__main__":
    unittest.main()
