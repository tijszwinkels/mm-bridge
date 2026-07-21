"""Backend-invocation errors must be surfaced into the Mattermost channel.

When a backend/harness interaction fails, the bridge historically logged the
error but left the channel silent (or posted an opaque one-liner). These tests
pin the user-visible behaviour: every mapped failure path posts a clear message
that leads with *what was attempted*, names the backend, and includes a
concise error detail — while the full error stays in the log.

See ``format_backend_error`` for the shared template.
"""
from __future__ import annotations

import unittest

import httpx

from mm_bridge.config import Anchor
from mm_bridge.purpose import PurposeConfig

# Reuse the async bridge fixture from the main suite.
from test_bridge import _BridgeTestCase


# ───────────────────── Integration: silent paths now post ──────────────────


class SessionBootstrapErrorTests(_BridgeTestCase):
    async def test_bootstrap_failure_surfaces_backend_and_detail(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude"}

        async def boom(**_kwargs):
            raise httpx.HTTPStatusError(
                "agent-harness POST /v1/sessions -> 500: harness exploded",
                request=httpx.Request("POST", "http://h/v1/sessions"),
                response=httpx.Response(500),
            )

        self.bridge.harness.create_session = boom  # type: ignore[assignment]

        await self.bridge._start_invited_session(
            "c1", initial_message="hello", awaits_first_message=False,
        )

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings, "bootstrap failure must post a warning")
        msg = warnings[-1]
        self.assertIn("start a session", msg)
        self.assertIn("`claude` backend", msg)
        self.assertIn("harness exploded", msg)


class SessionRestartErrorTests(_BridgeTestCase):
    async def test_restart_failure_surfaces_backend_and_detail(self):
        self.bridge.mapping.link(Anchor("c1"), "s-old")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        cfg = PurposeConfig(backend="codex", model=None, mention_only=False)

        async def boom(**_kwargs):
            raise RuntimeError("harness down")

        self.bridge.harness.create_session = boom  # type: ignore[assignment]

        result = await self.bridge._restart_session_with_config("c1", "s-old", cfg)
        self.assertIsNone(result)

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings, "restart failure must post a warning")
        msg = warnings[-1]
        self.assertIn("restart the session", msg)
        self.assertIn("`codex` backend", msg)
        self.assertIn("harness down", msg)


class RunForwardErrorTests(_BridgeTestCase):
    async def test_run_create_failure_surfaces_backend_and_detail(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        async def failing(session_id, message):
            raise RuntimeError("run create blew up")

        self.bridge.harness.create_run = failing  # type: ignore[assignment]

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "@claude do the thing",
            "user_id": "u1", "type": "",
        })

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings, "run-create failure must post a warning")
        msg = warnings[-1]
        self.assertIn("run your message", msg)
        self.assertIn("`claude` backend", msg)
        self.assertIn("run create blew up", msg)


class CatchUpErrorTests(_BridgeTestCase):
    async def test_catch_up_run_failure_is_no_longer_silent(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )
        self.bridge.mm.posts_by_channel["c1"] = [
            {"user_id": "u1", "message": "m1", "type": ""},
        ]

        async def failing(session_id, message):
            raise RuntimeError("catch-up run blew up")

        self.bridge.harness.create_run = failing  # type: ignore[assignment]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude catch up 50",
            "user_id": "u1", "type": "",
        })

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings, "catch-up run failure must not be silent")
        msg = warnings[-1]
        self.assertIn("`claude` backend", msg)
        self.assertIn("catch-up run blew up", msg)


class RunFailedSseTests(_BridgeTestCase):
    async def test_run_failed_event_posts_error_with_detail(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )

        await self.bridge._on_harness_event(
            "run.failed",
            {
                "sequence": 9,
                "event": "run.failed",
                "data": {"error": "No such file: claude", "error_type": "FileNotFoundError"},
                "session_id": "s1",
                "run_id": "run-1",
            },
        )

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings, "run.failed must surface a warning")
        msg = warnings[-1]
        self.assertIn("`claude` backend", msg)
        self.assertIn("No such file: claude", msg)

    async def test_run_failed_returncode_variant(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )

        await self.bridge._on_harness_event(
            "run.failed",
            {
                "event": "run.failed",
                "data": {"returncode": 127},
                "session_id": "s1",
                "run_id": "run-2",
            },
        )

        warnings = [p.message for p in self.bridge.mm.posted if ":warning:" in p.message]
        self.assertTrue(warnings)
        self.assertIn("127", warnings[-1])

    async def test_run_completed_stays_silent(self):
        """The new post fires ONLY on run.failed — not on normal completion."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )
        before = list(self.bridge.mm.posted)

        await self.bridge._on_harness_event(
            "run.completed",
            {
                "event": "run.completed",
                "data": {"stop_reason": "end_turn"},
                "session_id": "s1",
                "run_id": "run-3",
            },
        )

        new_warnings = [
            p for p in self.bridge.mm.posted
            if p not in before and ":warning:" in p.message
        ]
        self.assertEqual(new_warnings, [])

    async def test_run_failed_without_anchor_is_dropped(self):
        """No mapped channel → nothing to post to; must not raise."""
        await self.bridge._on_harness_event(
            "run.failed",
            {
                "event": "run.failed",
                "data": {"returncode": 1},
                "session_id": "unmapped",
                "run_id": "run-4",
            },
        )
        self.assertFalse(
            [p for p in self.bridge.mm.posted if ":warning:" in p.message]
        )


if __name__ == "__main__":
    unittest.main()
