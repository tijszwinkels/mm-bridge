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


class PublicUrlTests(unittest.TestCase):
    """``mm_public_url`` — optional user-facing base URL for permalinks.

    The daemon talks to MM over ``mm_url`` (often ``localhost``); permalinks
    rendered in channel headers must point at a URL humans can reach from
    their browsers. ``MM_PUBLIC_URL`` / ``[mattermost].public_url`` decouples
    the two. Falls back to empty (callers construct from ``mm_url``).
    """

    def test_default_is_empty(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.mm_public_url, "")

    def test_env_var_sets_public_url(self) -> None:
        cfg = Config()
        with patch.dict(
            "os.environ",
            {"MM_PUBLIC_URL": "http://pillar.tail72f2bc.ts.net:8065"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertEqual(
            cfg.mm_public_url, "http://pillar.tail72f2bc.ts.net:8065",
        )

    def test_toml_mattermost_public_url_sets_field(self) -> None:
        cfg = Config()
        cfg._apply_toml({"mattermost": {"public_url": "https://mm.example.com"}})
        self.assertEqual(cfg.mm_public_url, "https://mm.example.com")


class DangerousPermissionsTests(unittest.TestCase):
    """Bridge-owned dangerous-permissions config mirrors VibeDeck operator mode."""

    def test_default_is_false(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.dangerous_permissions)

    def test_env_true_sets_dangerous_permissions(self) -> None:
        cfg = Config()
        with patch.dict(
            "os.environ",
            {"MM_BRIDGE_DANGEROUS_PERMISSIONS": "true"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertTrue(cfg.dangerous_permissions)

    def test_env_zero_clears_dangerous_permissions(self) -> None:
        cfg = Config(dangerous_permissions=True)
        with patch.dict(
            "os.environ",
            {"MM_BRIDGE_DANGEROUS_PERMISSIONS": "0"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertFalse(cfg.dangerous_permissions)

    def test_toml_sets_dangerous_permissions(self) -> None:
        cfg = Config()
        cfg._apply_toml({"dangerous_permissions": True})
        self.assertTrue(cfg.dangerous_permissions)


if __name__ == "__main__":
    unittest.main()
