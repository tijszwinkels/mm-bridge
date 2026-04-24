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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import json

from . import sidecar, spawn as spawn_mod
from .bridge import Bridge, resolve_attachment_path
from .config import Anchor, ChannelMapping, Config
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


_PURPOSE_BADGE_CAP = 40


def _sanitise_badge(text: str) -> str:
    """Collapse tabs and newlines to single spaces for tab-separated output."""
    return " ".join(text.split())


def _format_channels_text(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        display = row["display_name"] or row["name"]
        badges: list[str] = []
        if row.get("session_id"):
            badges.append("[session]")
        purpose = _sanitise_badge(row.get("purpose") or "")
        if purpose:
            if len(purpose) > _PURPOSE_BADGE_CAP:
                purpose = purpose[: _PURPOSE_BADGE_CAP - 1] + "…"
            badges.append(f"[purpose: {purpose}]")
        lines.append("\t".join([row["id"], display, " ".join(badges)]))
    return "\n".join(lines)


def cmd_channels(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    mm = _make_mm_client(cfg)
    try:
        mm.login()
        channels = mm.list_bot_channels()
    except Exception as exc:
        print(f"Error: could not list channels: {exc}", file=sys.stderr)
        return 3

    channels = [c for c in channels if c.get("type") != "D"]

    if args.title:
        kw = args.title.lower()
        channels = [
            c for c in channels
            if kw in (
                (c.get("display_name") or "") + " " + (c.get("name") or "")
            ).lower()
        ]

    channels.sort(
        key=lambda c: (c.get("last_post_at") or 0, c.get("create_at") or 0),
        reverse=True,
    )

    try:
        mapping = ChannelMapping.load(
            cfg.state_file,
            sidecar_dir=cfg.sidecar_dir,
            reconcile_sidecars=False,
        )
        channel_to_session = {
            a.channel_id: sid
            for a, sid in mapping.anchor_to_session.items()
            if a.root_id is None
        }
    except Exception:
        logger.warning("Failed to load channel mapping", exc_info=True)
        channel_to_session = {}

    if args.n and args.n > 0:
        channels = channels[: args.n]

    rows = [
        {
            "id": c["id"],
            "name": c.get("name") or "",
            "display_name": c.get("display_name") or "",
            "last_post_at": c.get("last_post_at") or 0,
            "create_at": c.get("create_at") or 0,
            "purpose": c.get("purpose") or "",
            "header": c.get("header") or "",
            "session_id": channel_to_session.get(c["id"]),
        }
        for c in channels
    ]

    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        text = _format_channels_text(rows)
        if text:
            print(text)
    return 0


_MAX_POST_ATTACHMENTS = 10


def _resolve_post_anchor(
    cfg: Config, explicit_channel: str | None,
) -> Anchor:
    """Channel + default root_id for a post.

    Explicit ``--channel`` wins with no default root_id. Otherwise fall
    back to the current session's sidecar (which may carry a root_id
    for thread-forked sessions).
    """
    if explicit_channel:
        return Anchor(explicit_channel, None)
    return _resolve_anchor_from_session(cfg.sidecar_dir, _current_session_id())


def _resolve_effective_root(
    anchor: Anchor, thread: str | None, no_thread: bool,
) -> str | None:
    if no_thread:
        return None
    if thread:
        return thread
    return anchor.root_id


def _validate_attachments(
    paths: list[str], allowed_roots: list[str], max_bytes: int,
) -> tuple[list[Path] | None, int, str]:
    """Resolve + stat every --file. Returns (paths|None, exit_code, err_msg).

    A non-None paths list means all checks passed. On failure, ``paths``
    is None and the caller should exit with ``exit_code``.
    """
    if len(paths) > _MAX_POST_ATTACHMENTS:
        return (
            None, 2,
            f"Error: at most {_MAX_POST_ATTACHMENTS} --file attachments "
            f"allowed per post (got {len(paths)}).",
        )
    resolved: list[Path] = []
    for raw in paths:
        raw_path = Path(raw)
        candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        path = resolve_attachment_path(
            str(candidate), project_path=None, allowed_roots=allowed_roots,
        )
        if path is None:
            return (
                None, 2,
                f"Error: --file {raw!r} is outside allowed_attachment_roots.",
            )
        if not path.is_file():
            return (
                None, 3,
                f"Error: --file {raw!r} is not a readable file ({path}).",
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            return None, 3, f"Error: could not stat --file {raw!r}: {exc}"
        if size > max_bytes:
            return (
                None, 3,
                f"Error: --file {raw!r} is {size} bytes, exceeds server "
                f"max of {max_bytes} bytes.",
            )
        resolved.append(path)
    return resolved, 0, ""


def cmd_post(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        anchor = _resolve_post_anchor(cfg, args.channel)
    except NotInMattermostChannel as exc:
        print(
            f"Error: not running inside a Mattermost channel and "
            f"no --channel given ({exc}).",
            file=sys.stderr,
        )
        return 2

    root_id = _resolve_effective_root(anchor, args.thread, args.no_thread)

    if args.message == "-":
        body = sys.stdin.read().rstrip("\n")
    else:
        body = args.message

    if not body.strip() and not args.file:
        print("Error: message body is empty.", file=sys.stderr)
        return 2

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        max_bytes = mm.get_max_file_size()
    except Exception:
        logger.debug("get_max_file_size failed, using 50MB fallback", exc_info=True)
        max_bytes = 50 * 1024 * 1024

    resolved, err_code, err_msg = _validate_attachments(
        args.file, cfg.allowed_attachment_roots, max_bytes,
    )
    if resolved is None:
        print(err_msg, file=sys.stderr)
        return err_code

    file_ids: list[str] = []
    for path in resolved:
        try:
            file_ids.append(mm.upload_file(anchor.channel_id, path))
        except Exception as exc:
            print(
                f"Error: upload failed for {path}: {exc}", file=sys.stderr,
            )
            return 3

    try:
        post = mm.post(
            anchor.channel_id, body,
            file_ids=file_ids or None,
            root_id=root_id,
        )
    except Exception as exc:
        print(f"Error: post failed: {exc}", file=sys.stderr)
        return 3

    print(post["id"])
    return 0


_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_DURATION_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def _parse_since(raw: str, now_ms: int) -> int:
    """Return a ms-epoch for a ``--since`` argument.

    Accepted forms:

    * ``<int><m|h|d>`` — relative duration, subtracted from ``now_ms``.
    * All-digits → parsed as an ms-epoch verbatim.
    * ISO-8601 timestamp (``Z`` or offset suffix accepted).
    """
    match = _DURATION_RE.match(raw)
    if match:
        amount = int(match.group(1))
        unit_ms = _DURATION_UNIT_MS[match.group(2)]
        return now_ms - amount * unit_ms
    if raw.isdigit():
        return int(raw)
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            "--since must be an ISO-8601 timestamp, an ms-epoch, or a "
            "duration like '30m', '2h', '1d'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class _UserCache:
    """Per-invocation username cache; unknown users degrade gracefully."""

    def __init__(self, mm) -> None:
        self._mm = mm
        self._cache: dict[str, str] = {}

    def username(self, user_id: str) -> str:
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            user = self._mm.get_user(user_id)
            name = user.get("username") or f"user:{user_id[:8]}"
        except Exception:
            name = f"user:{user_id[:8]}"
        self._cache[user_id] = name
        return name


def _human_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n // 1000} KB"
    return f"{n} B"


def _render_post_text(
    post: dict, username: str, file_infos: list[dict],
) -> str:
    ts_ms = int(post.get("create_at") or 0)
    ts = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    lines = [f"[{ts}] {username}"]
    body = (post.get("message") or "").rstrip()
    if body:
        lines.append(body)
    for info in file_infos:
        name = info.get("name") or info.get("id") or "file"
        size = info.get("size") or 0
        lines.append(f"📎 {name} ({_human_size(size)})")
    return "\n".join(lines)


def _project_post_json(
    post: dict, username: str, is_bot: bool, file_infos: list[dict],
) -> dict:
    return {
        "id": post.get("id"),
        "create_at": post.get("create_at"),
        "user_id": post.get("user_id"),
        "username": username,
        "is_bot": is_bot,
        "root_id": post.get("root_id") or "",
        "message": post.get("message") or "",
        "files": [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "size": f.get("size"),
                "mime_type": f.get("mime_type"),
            }
            for f in file_infos
        ],
    }


def _collect_file_infos(mm, post: dict) -> list[dict]:
    infos: list[dict] = []
    for fid in post.get("file_ids") or []:
        try:
            infos.append(mm.get_file_info(fid))
        except Exception:
            logger.debug("get_file_info failed for %s", fid, exc_info=True)
            infos.append({"id": fid, "name": fid, "size": 0})
    return infos


def cmd_read(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        anchor = _resolve_post_anchor(cfg, args.channel)
    except NotInMattermostChannel as exc:
        print(
            f"Error: not running inside a Mattermost channel and "
            f"no --channel given ({exc}).",
            file=sys.stderr,
        )
        return 2

    root_id = _resolve_effective_root(anchor, args.thread, args.no_thread)

    since_ms: int | None = None
    if args.since:
        try:
            since_ms = _parse_since(args.since, int(time.time() * 1000))
        except ValueError as exc:
            print(f"Error: --since: {exc}", file=sys.stderr)
            return 2

    if args.n == 0:
        limit = cfg.catch_up_max_n
        uncapped = True
    else:
        limit = min(max(args.n, 1), cfg.catch_up_max_n)
        uncapped = False

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        if root_id:
            posts = mm.get_thread_posts(root_id)
        elif since_ms is not None:
            posts = mm.get_posts_since(anchor.channel_id, since_ms)
        else:
            posts = mm.get_posts(anchor.channel_id, limit)
    except Exception as exc:
        print(f"Error: could not fetch posts: {exc}", file=sys.stderr)
        return 3

    if since_ms is not None:
        posts = [p for p in posts if (p.get("create_at") or 0) >= since_ms]

    posts = [p for p in posts if not (p.get("type") or "")]
    if args.no_bot:
        bot_id = getattr(mm, "bot_user_id", "")
        posts = [p for p in posts if p.get("user_id") != bot_id]

    posts.sort(key=lambda p: p.get("create_at") or 0)

    if not uncapped and len(posts) > limit:
        posts = posts[-limit:]

    users = _UserCache(mm)
    bot_id = getattr(mm, "bot_user_id", "")

    if args.format == "text":
        blocks: list[str] = []
        for p in posts:
            uname = users.username(p.get("user_id") or "")
            infos = _collect_file_infos(mm, p)
            blocks.append(_render_post_text(p, uname, infos))
        if blocks:
            print("\n\n".join(blocks))
        return 0

    projected = []
    for p in posts:
        uname = users.username(p.get("user_id") or "")
        infos = _collect_file_infos(mm, p)
        projected.append(
            _project_post_json(p, uname, p.get("user_id") == bot_id, infos),
        )

    if args.format == "jsonl":
        for obj in projected:
            print(json.dumps(obj))
    else:
        print(json.dumps(projected, indent=2))
    return 0


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

    thread_permalink: str | None = None
    if parent_anchor.is_thread:
        base_url = cfg.mm_public_url or spawn_mod.build_mm_base_url(
            cfg.mm_scheme, cfg.mm_url, cfg.mm_port,
        )
        thread_permalink = spawn_mod.format_post_permalink(
            base_url, cfg.mm_team, parent_anchor.root_id or "",
        )
    try:
        mm.set_channel_header(
            new_channel_id,
            spawn_mod.format_parent_header(
                parent_name, thread_permalink=thread_permalink,
            ),
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

    p_channels = sub.add_parser(
        "channels",
        help="List Mattermost channels the bot can see.",
    )
    p_channels.add_argument(
        "--title",
        help="Case-insensitive substring filter on display_name or name.",
    )
    p_channels.add_argument(
        "-n", type=int, default=20,
        help="Max rows to display (0 = unlimited). Default 20.",
    )
    p_channels.add_argument(
        "--format", choices=["text", "json"], default="text",
    )
    p_channels.set_defaults(func=cmd_channels)

    p_post = sub.add_parser(
        "post", help="Post a message to a Mattermost channel.",
    )
    p_post.add_argument(
        "--channel",
        help="Channel id. Defaults to the current session's channel.",
    )
    thread_group = p_post.add_mutually_exclusive_group()
    thread_group.add_argument(
        "--thread", metavar="ROOT_POST_ID",
        help="Post as a reply inside this thread.",
    )
    thread_group.add_argument(
        "--no-thread", action="store_true",
        help="Post at channel level even if the session is thread-forked.",
    )
    p_post.add_argument(
        "--file", action="append", default=[], metavar="PATH",
        help="Attachment path (repeatable, max 10).",
    )
    p_post.add_argument(
        "message", help="Message body, or '-' to read from stdin.",
    )
    p_post.set_defaults(func=cmd_post)

    p_read = sub.add_parser(
        "read", help="Print recent posts from a Mattermost channel.",
    )
    p_read.add_argument(
        "--channel",
        help="Channel id. Defaults to the current session's channel.",
    )
    read_thread_group = p_read.add_mutually_exclusive_group()
    read_thread_group.add_argument(
        "--thread", metavar="ROOT_POST_ID",
        help="Read only posts inside this thread.",
    )
    read_thread_group.add_argument(
        "--no-thread", action="store_true",
        help="Read channel-level even if the session is thread-forked.",
    )
    p_read.add_argument(
        "-n", type=int, default=50,
        help="Max posts (0 = unlimited within --since). Default 50, cap 500.",
    )
    p_read.add_argument(
        "--since",
        help="ISO-8601 timestamp, ms-epoch, or duration like '1h', '2d'.",
    )
    p_read.add_argument(
        "--format", choices=["text", "json", "jsonl"], default="text",
    )
    p_read.add_argument(
        "--no-bot", action="store_true", help="Exclude bot posts.",
    )
    p_read.set_defaults(func=cmd_read)

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
