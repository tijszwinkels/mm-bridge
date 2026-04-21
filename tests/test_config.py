"""Tests for `Config._apply_env` MM_URL parsing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mm_bridge.config import Config


class ApplyEnvMmUrlTests(unittest.TestCase):
    """MM_URL may be a bare hostname or a full URL; both must work."""

    def _apply(self, env: dict[str, str]) -> Config:
        cfg = Config()
        with patch.dict("os.environ", env, clear=True):
            cfg._apply_env()
        return cfg

    def test_bare_hostname_leaves_port_and_scheme_unchanged(self) -> None:
        cfg = self._apply({"MM_URL": "localhost"})
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 8065)  # default
        self.assertEqual(cfg.mm_scheme, "http")  # default

    def test_full_http_url_with_explicit_port(self) -> None:
        cfg = self._apply({"MM_URL": "http://localhost:8065"})
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 8065)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_full_https_url_without_port_defaults_to_443(self) -> None:
        cfg = self._apply({"MM_URL": "https://mm.example.com"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 443)
        self.assertEqual(cfg.mm_scheme, "https")

    def test_full_http_url_without_port_defaults_to_80(self) -> None:
        cfg = self._apply({"MM_URL": "http://mm.example.com"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 80)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_full_https_url_with_explicit_port(self) -> None:
        cfg = self._apply({"MM_URL": "https://mm.example.com:8443"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 8443)
        self.assertEqual(cfg.mm_scheme, "https")

    def test_explicit_mm_port_env_overrides_url_port(self) -> None:
        """Explicit MM_PORT wins even when MM_URL embeds a port."""
        cfg = self._apply({
            "MM_URL": "http://localhost:8065",
            "MM_PORT": "9000",
        })
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 9000)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_explicit_mm_scheme_env_overrides_url_scheme(self) -> None:
        cfg = self._apply({
            "MM_URL": "http://localhost:8065",
            "MM_SCHEME": "https",
        })
        self.assertEqual(cfg.mm_scheme, "https")


if __name__ == "__main__":
    unittest.main()
