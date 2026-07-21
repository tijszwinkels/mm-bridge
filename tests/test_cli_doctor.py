"""`mm-bridge doctor` — the install-verification subcommand.

`doctor` is the runbook's checkpoint primitive: it prints a ✓/✗ line per
check and exits nonzero if any check fails. It diagnoses only — it never
mutates config, creates directories, or otherwise "fixes" anything.

Checks:
  * config loads and required keys are present,
  * Mattermost reachable + ``MM_BOT_TOKEN`` valid (resolves the bot username),
  * agent-harness base URL reachable (``/v1/health``),
  * sidecar dir writable.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli
from mm_bridge.config import Config


class _FakeMM:
    """Minimal MM double: login() + a resolved bot_username."""

    def __init__(self, *, bot_username: str = "b3mo", fail: Exception | None = None):
        self.bot_username = bot_username
        self._fail = fail
        self.logged_in = False

    def login(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.logged_in = True


class _FakeHarness:
    """Async agent-harness double exposing health()/close()."""

    def __init__(self, url: str, *, fail: Exception | None = None):
        self.base_url = url
        self._fail = fail
        self.closed = False

    async def health(self) -> dict:
        if self._fail is not None:
            raise self._fail
        return {"status": "ok"}

    async def close(self) -> None:
        self.closed = True


def _harness_factory(*, fail: Exception | None = None):
    """Return an ``AgentHarnessClient``-shaped constructor for patching."""
    def _make(url: str) -> _FakeHarness:
        return _FakeHarness(url, fail=fail)
    return _make


# ─────────────────────────── check-function unit tests ────────────────────


class DoctorConfigCheckTests(unittest.TestCase):
    def test_ok_when_required_keys_present(self) -> None:
        cfg = Config(mm_bot_token="t", mm_url="localhost", mm_team="ws")
        result = cli._doctor_check_config(cfg)
        self.assertTrue(result.ok)

    def test_fails_when_token_missing(self) -> None:
        cfg = Config(mm_bot_token="", mm_url="localhost", mm_team="ws")
        result = cli._doctor_check_config(cfg)
        self.assertFalse(result.ok)
        self.assertIn("MM_BOT_TOKEN", result.detail)

    def test_fails_lists_all_missing_keys(self) -> None:
        cfg = Config(mm_bot_token="", mm_team="", agent_harness_url="")
        result = cli._doctor_check_config(cfg)
        self.assertFalse(result.ok)
        self.assertIn("MM_BOT_TOKEN", result.detail)
        self.assertIn("mm_team", result.detail)


class DoctorMattermostCheckTests(unittest.TestCase):
    def test_ok_reports_resolved_bot_username(self) -> None:
        cfg = Config(mm_bot_token="t", mm_team="ws")
        fake = _FakeMM(bot_username="b3mo")
        with patch("mm_bridge.cli._make_mm_client", return_value=fake):
            result = cli._doctor_check_mattermost(cfg)
        self.assertTrue(result.ok)
        self.assertTrue(fake.logged_in)
        self.assertIn("b3mo", result.detail)

    def test_fails_without_token(self) -> None:
        cfg = Config(mm_bot_token="")
        result = cli._doctor_check_mattermost(cfg)
        self.assertFalse(result.ok)

    def test_fails_on_login_error(self) -> None:
        cfg = Config(mm_bot_token="bad")
        fake = _FakeMM(fail=RuntimeError("401 unauthorized"))
        with patch("mm_bridge.cli._make_mm_client", return_value=fake):
            result = cli._doctor_check_mattermost(cfg)
        self.assertFalse(result.ok)
        self.assertIn("401", result.detail)


class DoctorHarnessCheckTests(unittest.TestCase):
    def test_ok_when_health_responds(self) -> None:
        cfg = Config(agent_harness_url="http://localhost:8877")
        with patch("mm_bridge.cli.AgentHarnessClient", _harness_factory()):
            result = cli._doctor_check_harness(cfg)
        self.assertTrue(result.ok)
        self.assertIn("8877", result.detail)

    def test_fails_when_unreachable(self) -> None:
        cfg = Config(agent_harness_url="http://localhost:8877")
        factory = _harness_factory(fail=RuntimeError("connection refused"))
        with patch("mm_bridge.cli.AgentHarnessClient", factory):
            result = cli._doctor_check_harness(cfg)
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.detail)


class DoctorSidecarCheckTests(unittest.TestCase):
    def test_ok_when_dir_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(sidecar_dir=str(Path(tmp) / "sessions"))
            result = cli._doctor_check_sidecar(cfg)
        self.assertTrue(result.ok)

    def test_fails_when_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(sidecar_dir=str(Path(tmp) / "sessions"))
            with patch("os.access", return_value=False):
                result = cli._doctor_check_sidecar(cfg)
        self.assertFalse(result.ok)


# ──────────────────────────── end-to-end dispatch ─────────────────────────


class DoctorCommandTests(unittest.TestCase):
    def _run(self, cfg: Config, *, mm=None, harness_fail=None):
        buf = io.StringIO()
        mm = mm or _FakeMM(bot_username="b3mo")
        with patch("sys.argv", ["mm-bridge", "doctor"]), \
             patch("mm_bridge.cli.Config.load", return_value=cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=mm), \
             patch(
                 "mm_bridge.cli.AgentHarnessClient",
                 _harness_factory(fail=harness_fail),
             ), \
             patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        return cm.exception.code, buf.getvalue()

    def test_all_green_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                mm_bot_token="t",
                mm_team="ws",
                sidecar_dir=str(Path(tmp) / "sessions"),
            )
            code, out = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        self.assertNotIn("✗", out)
        self.assertIn("b3mo", out)

    def test_missing_token_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                mm_bot_token="",
                sidecar_dir=str(Path(tmp) / "sessions"),
            )
            code, out = self._run(cfg)
        self.assertNotEqual(code, 0)
        self.assertIn("✗", out)

    def test_harness_down_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                mm_bot_token="t",
                mm_team="ws",
                sidecar_dir=str(Path(tmp) / "sessions"),
            )
            code, out = self._run(cfg, harness_fail=RuntimeError("refused"))
        self.assertNotEqual(code, 0)
        self.assertIn("✗", out)


if __name__ == "__main__":
    unittest.main()
