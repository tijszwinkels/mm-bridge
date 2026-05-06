"""Tests for the codex rollout-file → session_id resolver.

The resolver scans ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl``
files newest-first, parses the first JSONL line (a ``session_meta``
record with ``payload.cwd`` and ``payload.id``), and returns the most
recent session id whose ``payload.cwd`` matches the caller's cwd.

Pure stdlib; no dependency on ``/proc`` or process trees.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mm_bridge.codex_session import find_session_id_by_cwd


def _write_rollout(
    sessions_root: Path,
    *,
    yyyy: str,
    mm: str,
    dd: str,
    iso_ts: str,
    session_id: str,
    cwd: str,
    mtime: float | None = None,
    extra_lines: list[dict] | None = None,
    bad_first_line: str | None = None,
) -> Path:
    day_dir = sessions_root / yyyy / mm / dd
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{iso_ts}-{session_id}.jsonl"
    lines: list[str] = []
    if bad_first_line is not None:
        lines.append(bad_first_line)
    else:
        meta = {
            "timestamp": f"{yyyy}-{mm}-{dd}T{iso_ts.split('T')[1].replace('-', ':')}.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        }
        lines.append(json.dumps(meta))
    for extra in extra_lines or []:
        lines.append(json.dumps(extra))
    path.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class FindSessionIdByCwdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_returns_id_for_matching_cwd(self) -> None:
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T10-14-22",
            session_id="019dfc5a-3123-7d63-8aa1-38fa382509c9",
            cwd="/home/claude/projects/mm-bridge",
        )
        found = find_session_id_by_cwd(
            "/home/claude/projects/mm-bridge", sessions_root=self.root,
        )
        self.assertEqual(found, "019dfc5a-3123-7d63-8aa1-38fa382509c9")

    def test_returns_none_when_no_match(self) -> None:
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T10-14-22",
            session_id="019dfc5a-3123-7d63-8aa1-38fa382509c9",
            cwd="/some/other/dir",
        )
        self.assertIsNone(
            find_session_id_by_cwd(
                "/home/claude/projects/mm-bridge", sessions_root=self.root,
            ),
        )

    def test_returns_none_when_root_missing(self) -> None:
        self.assertIsNone(
            find_session_id_by_cwd(
                "/anywhere",
                sessions_root=self.root / "does-not-exist",
            ),
        )

    def test_picks_newest_when_multiple_match(self) -> None:
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="05",
            iso_ts="2026-05-05T08-00-00",
            session_id="aaaaaaaa-1111-7111-aaaa-111111111111",
            cwd="/work/repo",
            mtime=1_000_000.0,
        )
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T09-30-00",
            session_id="bbbbbbbb-2222-7222-bbbb-222222222222",
            cwd="/work/repo",
            mtime=2_000_000.0,
        )
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T11-00-00",
            session_id="cccccccc-3333-7333-cccc-333333333333",
            cwd="/work/repo",
            mtime=3_000_000.0,
        )
        self.assertEqual(
            find_session_id_by_cwd("/work/repo", sessions_root=self.root),
            "cccccccc-3333-7333-cccc-333333333333",
        )

    def test_skips_files_with_unparseable_first_line(self) -> None:
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T11-00-00",
            session_id="dddddddd-4444-7444-dddd-444444444444",
            cwd="/work/repo",
            mtime=2_000_000.0,
            bad_first_line="not-json{{{",
        )
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T10-00-00",
            session_id="eeeeeeee-5555-7555-eeee-555555555555",
            cwd="/work/repo",
            mtime=1_000_000.0,
        )
        self.assertEqual(
            find_session_id_by_cwd("/work/repo", sessions_root=self.root),
            "eeeeeeee-5555-7555-eeee-555555555555",
        )

    def test_skips_files_missing_session_meta(self) -> None:
        # First line must be a session_meta record. A stray file whose
        # first line is some other type is skipped (not crashed on).
        weird_meta = {
            "type": "event_msg",
            "payload": {"id": "ffffffff-6666-7666-ffff-666666666666",
                        "cwd": "/work/repo"},
        }
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T11-00-00",
            session_id="ffffffff-6666-7666-ffff-666666666666",
            cwd="/work/repo",
            mtime=2_000_000.0,
            bad_first_line=json.dumps(weird_meta),
        )
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T10-00-00",
            session_id="11111111-7777-7777-1111-777777777777",
            cwd="/work/repo",
            mtime=1_000_000.0,
        )
        self.assertEqual(
            find_session_id_by_cwd("/work/repo", sessions_root=self.root),
            "11111111-7777-7777-1111-777777777777",
        )

    def test_returns_none_for_empty_sessions_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(
            find_session_id_by_cwd("/any/cwd", sessions_root=self.root),
        )

    def test_ignores_non_rollout_files(self) -> None:
        day_dir = self.root / "2026" / "05" / "06"
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "README.txt").write_text("hello")
        _write_rollout(
            self.root,
            yyyy="2026", mm="05", dd="06",
            iso_ts="2026-05-06T10-00-00",
            session_id="22222222-8888-7888-2222-888888888888",
            cwd="/work/repo",
        )
        self.assertEqual(
            find_session_id_by_cwd("/work/repo", sessions_root=self.root),
            "22222222-8888-7888-2222-888888888888",
        )


if __name__ == "__main__":
    unittest.main()
