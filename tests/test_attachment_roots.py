"""Attachment-path resolution against safety roots + the human-readable
root description used when an attach is rejected.

Regression cover for the clean-install attach gap: a `~/mm-attachments/…`
path must expand to `$HOME` (not be treated as a cwd-relative dir literally
named `~`, which never matches a root), and a rejected attach must tell the
agent which roots *are* allowed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mm_bridge.bridge import describe_allowed_roots, resolve_attachment_path


class ResolveAttachmentPathTests(unittest.TestCase):
    def test_expands_tilde_against_home(self):
        home = Path.home()
        resolved = resolve_attachment_path(
            "~/mm-attachments/report.html",
            project_path="/some/cwd",
            allowed_roots=[str(home / "mm-attachments")],
        )
        self.assertEqual(resolved, home / "mm-attachments" / "report.html")

    def test_absolute_path_inside_root_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            resolved = resolve_attachment_path(
                f"{d}/x.html", project_path=None, allowed_roots=[d],
            )
            self.assertEqual(resolved, Path(d) / "x.html")

    def test_path_outside_all_roots_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(
                resolve_attachment_path(
                    "/tmp/evil.html", project_path=d, allowed_roots=[d],
                )
            )

    def test_cwd_is_always_an_implicit_root(self):
        with tempfile.TemporaryDirectory() as d:
            resolved = resolve_attachment_path(
                "sub/x.html", project_path=d, allowed_roots=[],
            )
            self.assertEqual(resolved, Path(d) / "sub" / "x.html")


class DescribeAllowedRootsTests(unittest.TestCase):
    def test_lists_cwd_first_then_configured_roots(self):
        desc = describe_allowed_roots("/home/u/proj", ["/home/u/mm-attachments"])
        self.assertEqual(desc, "`/home/u/proj`, `/home/u/mm-attachments`")

    def test_expands_tilde_in_configured_roots(self):
        home = Path.home()
        desc = describe_allowed_roots(None, ["~/mm-attachments"])
        self.assertEqual(desc, f"`{home / 'mm-attachments'}`")

    def test_dedupes_cwd_that_also_appears_in_roots(self):
        desc = describe_allowed_roots("/home/u/proj", ["/home/u/proj"])
        self.assertEqual(desc, "`/home/u/proj`")

    def test_empty_when_no_roots_and_no_cwd(self):
        self.assertEqual(describe_allowed_roots(None, []), "")


if __name__ == "__main__":
    unittest.main()
