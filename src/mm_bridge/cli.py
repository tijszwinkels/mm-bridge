"""Command-line entry points for `mm-bridge`.

Subcommands:
  - `serve`                 → run the bridge daemon
  - `invite <username>`     → invite a MM user to this session's channel
  - `channel`               → print this session's MM channel_id (debug)
  - `spawn <prompt>`        → start a VD sub-session in a new sibling channel

A bare `mm-bridge` prints help and exits with status 1 — this is intentional
so that a typo like `mm-bridge` (meaning to ask a question inside a channel)
doesn't accidentally spin up a second daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from . import sidecar, spawn as spawn_mod
from .bridge import Bridge
from .config import Anchor, Config
from .mm_client import MattermostClient
from .vd_client import VibeDeckClient

logger = logging.getLogger(__name__)


class NotInMattermostChannel(RuntimeError):
    """Raised when the current Claude session has no MM-bridge sidecar."""


# ─────────────────────── Helpers (unit-tested) ────────────────────────────


def _resolve_anchor_from_session(
    sidecar_dir: Path | str, session_id: str,
) -> Anchor:
    """Return the :class:`Anchor` for `session_id` by reading its sidecar.

    Channel-level sidecars yield ``Anchor(channel_id)``; thread-fork
    sidecars (two-line files) yield ``Anchor(channel_id, root_id)``.
    Raises :class:`NotInMattermostChannel` when the sidecar is missing
    or unreadable.
    """
    result = sidecar.read(Path(sidecar_dir), session_id)
    if result is None:
        path = Path(sidecar_dir) / session_id
        raise NotInMattermostChannel(
            f"not running inside a Mattermost channel (no sidecar at {path})",
        )
    channel_id, root_id = result
    return Anchor(channel_id, root_id)


def _invite_to_channel(mm, channel_id: str, username: str) -> None:
    """Resolve @username → user_id and invite them to `channel_id`.

    `mm` must expose `get_user_by_username(name) -> {id}` and
    `invite_user(channel_id, user_id)`. Raises on failure.
    """
    clean = username.lstrip("@").strip()
    if not clean:
        raise ValueError("username must not be empty")
    user = mm.get_user_by_username(clean)
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError(f"could not resolve user_id for @{clean}")
    mm.invite_user(channel_id, user_id)


def _current_session_id() -> str:
    sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if not sid:
        raise NotInMattermostChannel(
            "CLAUDE_SESSION_ID is not set — "
            "this command only works inside a Claude Code session.",
        )
    return sid


def _make_mm_client(cfg: Config) -> MattermostClient:
    return MattermostClient(
        url=cfg.mm_url,
        port=cfg.mm_port,
        scheme=cfg.mm_scheme,
        token=cfg.mm_bot_token,
        team_name=cfg.mm_team,
    )


def _require_bot_token(cfg: Config) -> None:
    if not cfg.mm_bot_token:
        print(
            "Error: MM_BOT_TOKEN environment variable is required.",
            file=sys.stderr,
        )
        sys.exit(1)


def _snapshot_sidecar_names(sidecar_dir: Path | str) -> set[str]:
    """Current set of sidecar filenames (session_ids), or empty if missing."""
    d = Path(sidecar_dir)
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_file()}


def _wait_for_new_sidecar(
    sidecar_dir: Path | str,
    before: set[str],
    *,
    timeout: float = 30.0,
    interval: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Poll `sidecar_dir` for a file whose name wasn't in `before`.

    Returns ``(session_id, channel_id)``. When multiple new sidecars appear
    concurrently (rare: two spawns racing), the one with the newest mtime
    is chosen — a best-effort tiebreaker. Raises ``TimeoutError`` if no new
    sidecar appears within ``timeout`` seconds.
    """
    deadline = clock() + timeout
    while True:
        current = _snapshot_sidecar_names(sidecar_dir)
        new = current - before
        if new:
            if len(new) == 1:
                sid = next(iter(new))
            else:
                sid = max(
                    new,
                    key=lambda n: (Path(sidecar_dir) / n).stat().st_mtime,
                )
            result = sidecar.read(Path(sidecar_dir), sid)
            if result is None:
                raise RuntimeError(f"new sidecar {sid} is empty or unreadable")
            channel_id, _root_id = result
            return sid, channel_id
        if clock() >= deadline:
            raise TimeoutError(
                f"no new sidecar appeared in {sidecar_dir} within {timeout}s "
                "— is the mm-bridge daemon running?",
            )
        sleep(interval)


# ─────────────────────── Subcommand handlers ──────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.load()
    _require_bot_token(cfg)
    bridge = Bridge(cfg)

    async def _run() -> None:
        try:
            await bridge.start()
        except KeyboardInterrupt:
            pass
        finally:
            await bridge.stop()

    asyncio.run(_run())
    return 0


def cmd_channel(args: argparse.Namespace) -> int:
    cfg = Config.load()
    try:
        session_id = _current_session_id()
        anchor = _resolve_anchor_from_session(cfg.sidecar_dir, session_id)
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(anchor.channel_id)
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        session_id = _current_session_id()
        channel_id = _resolve_anchor_from_session(
            cfg.sidecar_dir, session_id,
        ).channel_id
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        _invite_to_channel(mm, channel_id, args.username)
    except Exception as exc:
        print(
            f"Error: could not invite @{args.username.lstrip('@')} "
            f"to {channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3

    print(f"Invited @{args.username.lstrip('@')} to this channel.")
    return 0


async def _vd_create_session(
    vd_url: str, message: str, cwd: str, backend: str | None,
) -> dict:
    """Async wrapper — one-shot VD client for the spawn CLI path."""
    vd = VibeDeckClient(vd_url)
    try:
        return await vd.create_session(
            message=message, cwd=cwd, backend=backend,
        )
    finally:
        await vd.close()


def cmd_spawn(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        parent_session_id = _current_session_id()
        parent_anchor = _resolve_anchor_from_session(
            cfg.sidecar_dir, parent_session_id,
        )
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    parent_channel_id = parent_anchor.channel_id

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        parent_channel = mm.get_channel(parent_channel_id)
    except Exception as exc:
        print(
            f"Error: could not fetch parent channel {parent_channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3
    parent_name = parent_channel.get("name") or parent_channel_id

    cwd = args.cwd or os.getcwd()
    backend = args.backend or cfg.default_backend

    before = _snapshot_sidecar_names(cfg.sidecar_dir)

    try:
        resp = asyncio.run(
            _vd_create_session(cfg.vd_url, args.prompt, cwd, backend),
        )
    except Exception as exc:
        print(f"Error: VibeDeck create_session failed: {exc}", file=sys.stderr)
        return 3
    status = resp.get("status")
    if status != "started":
        print(
            f"Error: VibeDeck returned unexpected status {status!r}",
            file=sys.stderr,
        )
        return 3

    try:
        session_id, new_channel_id = _wait_for_new_sidecar(
            cfg.sidecar_dir, before,
        )
    except TimeoutError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3

    try:
        new_channel = mm.get_channel(new_channel_id)
    except Exception as exc:
        print(
            f"Error: could not fetch new channel {new_channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3
    new_channel_name = new_channel.get("name") or new_channel_id

    display_name = spawn_mod.derive_display_name(
        args.title, new_channel.get("display_name", "") or new_channel_name,
    )
    if args.title:
        try:
            mm.rename_channel(new_channel_id, display_name)
        except Exception:
            logger.warning(
                "Failed to set display_name on %s", new_channel_id, exc_info=True,
            )

    try:
        mm.set_channel_header(
            new_channel_id, spawn_mod.format_parent_header(parent_name),
        )
    except Exception:
        logger.warning(
            "Failed to set parent header on %s", new_channel_id, exc_info=True,
        )

    if not args.no_forward_prompt:
        try:
            mm.post_message(
                new_channel_id,
                spawn_mod.format_spawn_kickoff(parent_name, args.prompt),
            )
        except Exception:
            logger.warning(
                "Failed to post kickoff message to new channel",
                exc_info=True,
            )

    if args.invite:
        try:
            _invite_to_channel(mm, new_channel_id, args.invite)
        except Exception as exc:
            print(
                f"Warning: could not invite @{args.invite.lstrip('@')}: {exc}",
                file=sys.stderr,
            )

    if not args.no_forward_prompt:
        try:
            mm.post(
                parent_channel_id,
                spawn_mod.format_spawn_announcement(
                    display_name, new_channel_name, args.prompt,
                ),
                root_id=parent_anchor.root_id,
            )
        except Exception:
            logger.warning(
                "Failed to post spawn announcement to parent channel",
                exc_info=True,
            )

    print(
        f"Spawned session {session_id[:12]} in ~{new_channel_name}~ "
        f"({new_channel_id})",
    )
    return 0


# ─────────────────────── argparse dispatch ────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mm-bridge",
        description="Mattermost ↔ VibeDeck bridge.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_serve = sub.add_parser("serve", help="Run the bridge daemon.")
    p_serve.set_defaults(func=cmd_serve)

    p_invite = sub.add_parser(
        "invite",
        help="Invite a Mattermost user to this session's channel.",
    )
    p_invite.add_argument(
        "username", help="Mattermost username (with or without leading @)",
    )
    p_invite.set_defaults(func=cmd_invite)

    p_channel = sub.add_parser(
        "channel",
        help="Print the Mattermost channel_id for the current session.",
    )
    p_channel.set_defaults(func=cmd_channel)

    p_spawn = sub.add_parser(
        "spawn",
        help="Start a VibeDeck sub-session in a new sibling MM channel.",
    )
    p_spawn.add_argument(
        "prompt",
        help="Initial prompt for the sub-session.",
    )
    p_spawn.add_argument(
        "--cwd",
        help="Working directory for the sub-session (default: current dir).",
    )
    p_spawn.add_argument(
        "--backend",
        help="Backend name (e.g. 'claude', 'codex'). Default: config default.",
    )
    p_spawn.add_argument(
        "--title",
        help="Display name for the new channel (default: daemon-derived).",
    )
    p_spawn.add_argument(
        "--invite",
        metavar="USERNAME",
        help="Invite a Mattermost user to the new channel.",
    )
    p_spawn.add_argument(
        "--no-forward-prompt",
        action="store_true",
        help="Don't post the prompt to either the new or parent channel.",
    )
    p_spawn.set_defaults(func=cmd_spawn)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        sys.exit(1)
    rc = args.func(args) or 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
