"""Tests for the live-codex-parent process-tree tie-breaker.

``find_active_codex_rollout_uuid`` walks the parent-pid chain from the
caller (defaulting to ``os.getppid()``), finds the nearest live process
whose ``/proc/<pid>/comm`` is exactly ``codex``, and reads its open file
descriptors to discover which rollout file the codex process has open.
The session UUID embedded in the rollout filename is returned.

This is the in-turn resolver: it only succeeds while a codex process
is alive in the caller's ancestor chain and is holding its rollout fd
open. Background tasks, crons, and shells that outlive their codex
process get ``None`` and the cwd-mtime fallback handles those.

Tests use a fake ``/proc`` tree under ``tmp_path`` so we don't depend
on whatever real codex processes happen to be running.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mm_bridge.codex_session import find_active_codex_rollout_uuid


def _make_proc_entry(
    proc_root: Path,
    pid: int,
    *,
    comm: str,
    ppid: int,
    fds: dict[int, str] | None = None,
) -> None:
    """Materialise ``/proc/<pid>/{comm,status,fd/*}`` under *proc_root*.

    ``comm`` is written with the trailing newline real ``/proc`` uses, so
    consumers that forget to ``rstrip`` get caught by tests. ``fds`` is a
    mapping ``{fd_num: target_path}`` — each entry becomes a symlink
    under ``/proc/<pid>/fd/<fd_num>`` pointing at *target_path* (which
    needn't exist).
    """
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "comm").write_text(f"{comm}\n")
    (pid_dir / "status").write_text(
        f"Name:\t{comm}\n"
        f"State:\tS (sleeping)\n"
        f"Pid:\t{pid}\n"
        f"PPid:\t{ppid}\n",
    )
    fd_dir = pid_dir / "fd"
    fd_dir.mkdir(exist_ok=True)
    for fd_num, target in (fds or {}).items():
        link = fd_dir / str(fd_num)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)


def _rollout(sessions_root: Path, ymd: str, iso_ts: str, uuid: str) -> Path:
    """Build a rollout-file path under *sessions_root* and return it.

    The file isn't actually written — process-tree lookup only reads
    fd-symlink targets, never opens the file. Keeping it virtual avoids
    interfering with the cwd-mtime resolver tests that DO write rollouts.
    """
    yyyy, mm, dd = ymd.split("-")
    return sessions_root / yyyy / mm / dd / f"rollout-{iso_ts}-{uuid}.jsonl"


class FindActiveCodexRolloutUuidTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.proc_root = self.tmp / "proc"
        self.proc_root.mkdir()
        self.sessions_root = self.tmp / "sessions"

    def test_returns_uuid_when_immediate_parent_is_codex(self) -> None:
        rollout = _rollout(
            self.sessions_root, "2026-05-08",
            "2026-05-08T15-44-50",
            "019e07d5-7574-7b10-9605-62796688bef7",
        )
        _make_proc_entry(
            self.proc_root, 2000, comm="codex", ppid=1,
            fds={3: "/dev/null", 67: str(rollout)},
        )
        _make_proc_entry(
            self.proc_root, 2001, comm="bash", ppid=2000,
        )
        self.assertEqual(
            find_active_codex_rollout_uuid(
                starting_pid=2001,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
            "019e07d5-7574-7b10-9605-62796688bef7",
        )

    def test_walks_through_non_codex_intermediates(self) -> None:
        """Tool shells reach codex through whatever sandbox helpers
        codex spawns. The walk must climb past non-codex pids."""
        rollout = _rollout(
            self.sessions_root, "2026-05-08",
            "2026-05-08T11-00-00",
            "aaaaaaaa-1111-7111-aaaa-111111111111",
        )
        _make_proc_entry(
            self.proc_root, 1000, comm="codex", ppid=1,
            fds={42: str(rollout)},
        )
        _make_proc_entry(
            self.proc_root, 1001, comm="bwrap", ppid=1000,
        )
        _make_proc_entry(
            self.proc_root, 1002, comm="sh", ppid=1001,
        )
        _make_proc_entry(
            self.proc_root, 1003, comm="bash", ppid=1002,
        )
        self.assertEqual(
            find_active_codex_rollout_uuid(
                starting_pid=1003,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
            "aaaaaaaa-1111-7111-aaaa-111111111111",
        )

    def test_returns_none_when_no_codex_in_chain(self) -> None:
        _make_proc_entry(self.proc_root, 3000, comm="systemd", ppid=1)
        _make_proc_entry(self.proc_root, 3001, comm="bash", ppid=3000)
        _make_proc_entry(self.proc_root, 3002, comm="bash", ppid=3001)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=3002,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_returns_none_when_codex_has_no_rollout_fd(self) -> None:
        """A codex process that hasn't opened a rollout (or has already
        closed it) shouldn't yield a stale UUID. Returning None lets the
        caller fall through to the mtime-walk fallback."""
        _make_proc_entry(
            self.proc_root, 4000, comm="codex", ppid=1,
            fds={3: "/dev/null", 4: "/tmp/unrelated.log"},
        )
        _make_proc_entry(self.proc_root, 4001, comm="bash", ppid=4000)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=4001,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_ignores_codex_fd_outside_sessions_root(self) -> None:
        """A spoofed binary named codex with an unrelated fd must not
        be mistaken for a real codex session — only fds whose target
        sits under *sessions_root* count."""
        bogus = self.tmp / "decoy" / "rollout-2026-05-08T11-00-00-deadbeef-1111-7111-1111-111111111111.jsonl"
        _make_proc_entry(
            self.proc_root, 5000, comm="codex", ppid=1,
            fds={5: str(bogus)},
        )
        _make_proc_entry(self.proc_root, 5001, comm="bash", ppid=5000)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=5001,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_stops_at_max_depth(self) -> None:
        """Linear chain of >max_depth pids with codex past the cap →
        None. Prevents pathological walks if /proc gets weird."""
        # Build a 12-deep chain: pid=10..21, codex at pid=21 (root).
        for i in range(10, 22):
            comm = "codex" if i == 21 else "bash"
            ppid = i + 1 if i < 21 else 1
            fds = (
                {7: str(_rollout(
                    self.sessions_root, "2026-05-08",
                    "2026-05-08T11-00-00",
                    "bbbbbbbb-2222-7222-bbbb-222222222222",
                ))}
                if i == 21
                else None
            )
            _make_proc_entry(
                self.proc_root, i, comm=comm, ppid=ppid, fds=fds,
            )
        # Starting at pid=10 with max_depth=4 means we visit pids
        # 10,11,12,13,14 — codex at 21 is past the cap.
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=10,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
                max_depth=4,
            ),
        )
        # Generous depth finds it.
        self.assertEqual(
            find_active_codex_rollout_uuid(
                starting_pid=10,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
                max_depth=20,
            ),
            "bbbbbbbb-2222-7222-bbbb-222222222222",
        )

    def test_stops_at_init(self) -> None:
        """If we walk all the way to PID 1 (init) without hitting codex,
        we stop — the chain doesn't continue past the kernel root."""
        _make_proc_entry(self.proc_root, 1, comm="systemd", ppid=0)
        _make_proc_entry(self.proc_root, 6000, comm="bash", ppid=1)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=6000,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_handles_missing_proc_entries(self) -> None:
        """A process that died between status reads should be skipped,
        not crashed on. The walk stops cleanly when /proc/<pid> is
        gone."""
        # Only pid=7000 exists; its parent 6999 has no /proc entry
        # (process died).
        _make_proc_entry(self.proc_root, 7000, comm="bash", ppid=6999)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=7000,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_handles_missing_proc_root(self) -> None:
        """macOS / non-Linux: ``/proc`` doesn't exist. Resolver returns
        None cleanly so the cwd-mtime fallback can take over."""
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=42,
                proc_root=self.proc_root / "nope",
                sessions_root=self.sessions_root,
            ),
        )

    def test_invalid_rollout_filename_is_ignored(self) -> None:
        """An fd target that lives under sessions_root but doesn't match
        the ``rollout-*-<uuid>.jsonl`` pattern is skipped — there might
        be a partial-write or rolled-over file we shouldn't mistake for
        the active session."""
        weird = (
            self.sessions_root / "2026" / "05" / "08" / "rollout-no-uuid-here.jsonl"
        )
        _make_proc_entry(
            self.proc_root, 8000, comm="codex", ppid=1,
            fds={9: str(weird)},
        )
        _make_proc_entry(self.proc_root, 8001, comm="bash", ppid=8000)
        self.assertIsNone(
            find_active_codex_rollout_uuid(
                starting_pid=8001,
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
        )

    def test_default_starting_pid_uses_getppid(self) -> None:
        """Without an explicit ``starting_pid``, the resolver starts at
        ``os.getppid()``. Test by faking the test process's PPid only —
        the walk should attempt that pid first."""
        ppid = os.getppid()
        rollout = _rollout(
            self.sessions_root, "2026-05-08",
            "2026-05-08T13-00-00",
            "cccccccc-3333-7333-cccc-333333333333",
        )
        _make_proc_entry(
            self.proc_root, ppid, comm="codex", ppid=1,
            fds={11: str(rollout)},
        )
        self.assertEqual(
            find_active_codex_rollout_uuid(
                proc_root=self.proc_root,
                sessions_root=self.sessions_root,
            ),
            "cccccccc-3333-7333-cccc-333333333333",
        )


if __name__ == "__main__":
    unittest.main()
