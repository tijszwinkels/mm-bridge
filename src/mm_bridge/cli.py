"""Command-line entry points for `mm-bridge`.

Subcommands:
  - `serve`                 → run the bridge daemon
  - `invite <username>`     → invite a MM user to this session's channel
  - `channel`               → print this session's MM channel_id (debug)

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
from pathlib import Path

from .bridge import Bridge
from .config import Config
from .mm_client import MattermostClient

logger = logging.getLogger(__name__)


class NotInMattermostChannel(RuntimeError):
    """Raised when the current Claude session has no MM-bridge sidecar."""


# ─────────────────────── Helpers (unit-tested) ────────────────────────────


def _resolve_channel_from_session(
    sidecar_dir: Path | str, session_id: str,
) -> str:
    """Return the channel_id for `session_id` by reading its sidecar file.

    Raises `NotInMattermostChannel` if the sidecar is missing or empty.
    """
    path = Path(sidecar_dir) / session_id
    if not path.exists():
        raise NotInMattermostChannel(
            f"not running inside a Mattermost channel (no sidecar at {path})",
        )
    try:
        channel_id = path.read_text().strip()
    except OSError as exc:
        raise NotInMattermostChannel(f"could not read sidecar {path}: {exc}") from exc
    if not channel_id:
        raise NotInMattermostChannel(f"sidecar {path} is empty")
    return channel_id


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
        channel_id = _resolve_channel_from_session(cfg.sidecar_dir, session_id)
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(channel_id)
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        session_id = _current_session_id()
        channel_id = _resolve_channel_from_session(cfg.sidecar_dir, session_id)
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
