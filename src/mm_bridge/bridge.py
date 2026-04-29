"""Bridge orchestrator — dispatches Mattermost ↔ VibeDeck events to handlers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import attribution, directives, name_sync, purpose, vd_client
from .config import Anchor, ChannelMapping, Config
from .mm_client import MattermostClient
from .typing_indicator import TypingIndicator
from .vd_client import VibeDeckClient

logger = logging.getLogger(__name__)

# Placeholder used when creating an invite-driven session, since VibeDeck's
# /sessions/new rejects empty messages.
INVITE_PLACEHOLDER = (
    "Hello! I've just been added to a Mattermost channel. "
    "I'll wait for the user to start the conversation."
)

MM_POST_MAX_LEN = 16000
MM_DISPLAY_NAME_MAX = 64
VD_TITLE_MAX = 200

_CATCH_UP_RE = re.compile(r"^@claude\s+catch\s+up(?:\s+(\d+))?\s*$", re.IGNORECASE)
_LEAVE_CMD_RE = re.compile(r"^@claude\s+leave\b(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_STOP_CMD_RE = re.compile(r"^(?P<mention>@claude\s+)?stop\s*$", re.IGNORECASE)
_RUNTIME_TOGGLE_RE = re.compile(
    r"^(autorespond|noautorespond|autoresponse|noautoresponse)$", re.IGNORECASE,
)


@dataclass
class PendingMattermostSession:
    """A channel that invited the bot and is waiting for VD session_added."""
    channel_id: str
    cwd: str
    backend: str | None
    initial_message: str
    requested_at: float
    purpose_cfg: purpose.PurposeConfig | None = None
    is_fork: bool = False
    # For fork-originated pending sessions:
    fork_parent_session: str | None = None
    fork_thread_channel: str | None = None
    fork_thread_root: str | None = None
    queued_messages: list[str] = field(default_factory=list)
    # Whether the next user post in this channel may be parsed as a
    # Channel Purpose reconfiguration. True for explicit invites, false
    # for engagement-triggered sessions in auto-joined channels (the
    # engagement message is already the user's first real post).
    allow_first_message_config: bool = True
    # MM channel display_name at invite time, used to seed the VD session
    # title on claim so the VibeDeck panel mirrors the MM channel name.
    channel_display_name: str | None = None


@dataclass
class ToolUseRun:
    """A single per-turn tool-use placeholder post, edited in place as
    the assistant fires tool calls. Lines accumulate one per tool switch;
    consecutive calls of the same tool bump the last line's counter.
    """
    post_id: str
    lines: list[list] = field(default_factory=list)  # [[tool_name, count], ...]


def _format_tool_run(run: ToolUseRun) -> str:
    parts: list[str] = []
    for tool, count in run.lines:
        suffix = f" (x{count})" if count > 1 else ""
        parts.append(f"_Using tool: {tool}{suffix}_")
    return "\n".join(parts)


def _bump_or_append(run: ToolUseRun, tool: str) -> None:
    if run.lines and run.lines[-1][0] == tool:
        run.lines[-1][1] += 1
    else:
        run.lines.append([tool, 1])


def _extract_text_from_blocks(blocks: list[dict]) -> str:
    """Flatten text + error blocks (not tool_use). Used for the real
    assistant response; tool_use blocks are handled separately via the
    ToolUseRun coalescing path in `_on_vd_message`.
    """
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_result":
            content = block.get("content", "")
            if block.get("is_error"):
                parts.append(f"**Tool error:** {str(content)[:500]}")
    return "\n".join(p for p in parts if p)


def _truncate_for_mm(text: str, max_len: int = MM_POST_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 50] + "\n\n_(truncated)_"


def _session_to_channel_name(session_id: str) -> str:
    short = session_id[:12].replace("-", "").lower()
    return f"s-{short}"


def _normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    return str(path).rstrip("/")


def _is_mm_system_post(post: dict) -> bool:
    return bool(post.get("type"))


def _format_first_message_preamble(
    channel_name: str, human_usernames: list[str],
) -> str:
    """Build the one-line `[...]` preamble for the first forwarded user message.

    The preamble tells the model it's running inside a Mattermost channel,
    who it's talking to, and how to keep their attention. Shape:

        [Running inside Mattermost channel "<name>". <who>. @-mention to keep their attention.]

    `<who>` varies:
      - 1 human  → `You're talking to @<u>`
      - ≥2       → ``Multiple users in this channel (`u1`, `u2`, ...) — messages are prefixed with `username:` ``
      - 0        → omitted entirely
    """
    pieces = [f'Running inside Mattermost channel "{channel_name}".']
    if len(human_usernames) == 1:
        pieces.append(f"You're talking to @{human_usernames[0]}.")
    elif len(human_usernames) > 1:
        user_list = ", ".join(f"`{u}`" for u in human_usernames)
        pieces.append(
            f"Multiple users in this channel ({user_list}) "
            "— messages are prefixed with `username:`."
        )
    pieces.append("@-mention to keep their attention.")
    return "[" + " ".join(pieces) + "]"


MM_INBOX_DIRNAME = ".mattermost-inbox"


def _safe_inbox_filename(name: str, fallback: str) -> str:
    """Sanitize a MM-supplied filename so it can't escape the inbox dir."""
    base = Path(name or "").name.strip().lstrip(".")
    base = base.replace("\0", "").replace("/", "_").replace("\\", "_")
    return base or fallback


def _unique_inbox_path(dest_dir: Path, filename: str) -> Path:
    """Return dest_dir/filename, with -N suffix on conflict."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(1, 10_000):
        cand = dest_dir / f"{stem}-{n}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"too many filename conflicts in {dest_dir}")


def resolve_attachment_path(
    raw_path: str,
    project_path: str | None,
    allowed_roots: list[str],
) -> Path | None:
    """Resolve a path from an <openFile/> directive against safety roots.
    Returns None if outside the allowed roots or if resolution fails.
    """
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            if not project_path:
                return None
            candidate = Path(project_path) / candidate
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return None

    root_candidates: list[Path] = []
    if project_path:
        try:
            root_candidates.append(Path(project_path).resolve(strict=False))
        except (OSError, ValueError):
            pass
    for r in allowed_roots:
        try:
            root_candidates.append(Path(r).expanduser().resolve(strict=False))
        except (OSError, ValueError):
            continue

    if not root_candidates:
        return resolved  # no sandbox configured → trust the caller

    for root in root_candidates:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


class Bridge:
    """Mediates Mattermost ↔ VibeDeck traffic."""

    def __init__(self, config: Config):
        self.config = config
        self.mapping = ChannelMapping.load(config.state_file, config.sidecar_dir)
        self.mm = MattermostClient(
            url=config.mm_url,
            port=config.mm_port,
            scheme=config.mm_scheme,
            token=config.mm_bot_token,
            team_name=config.mm_team,
        )
        self.vd = VibeDeckClient(config.vd_url)
        self.posters = attribution.PosterTracker()
        self.name_sync = name_sync.NameSync(
            window_seconds=config.name_sync_window_seconds
        )
        self.typing: TypingIndicator | None = None  # created after login
        self.purpose_by_channel: dict[str, purpose.PurposeConfig] = {}
        self.pending_mm_sessions: dict[str, PendingMattermostSession] = {}
        self.pending_forks: list[PendingMattermostSession] = []
        self.dead_threads: set[tuple[str, str]] = set()
        self.last_channel_state: dict[str, dict] = {}
        self.last_status_ts: dict[str, float] = {}
        # Channels waiting on their first user message; those messages are
        # parsed as config tokens when possible.
        self.awaiting_first_message: set[str] = set()
        # Channel purposes we just wrote ourselves, so the subsequent
        # `channel_updated` event doesn't trigger a spurious change notice.
        self._self_written_purpose: dict[str, str] = {}
        # Channels we joined ourselves (auto-join). The resulting
        # `user_added` event must not trigger a new VD session — presence
        # should be silent until a user engages.
        self._self_joined_channels: set[str] = set()
        self._max_file_size: int | None = None
        # Per-session coalesced tool-use placeholder posts. Created on the
        # first tool_use block of a turn, edited on subsequent ones; the
        # state is dropped (but the post is left intact) on turn end so
        # the next turn starts a fresh placeholder. The post remains as a
        # compact summary of that turn's tool usage.
        self.tool_use_runs: dict[str, ToolUseRun] = {}
        # Posts silently dropped in mention-only mode, keyed by
        # (channel_id, thread_root). Drained on the next forwarded
        # message in the same anchor and prepended as a catch-up block
        # so the session sees the preceding conversation it missed.
        self._silent_drops: dict[tuple[str, str | None], deque[dict]] = {}
        # user_id of the most recent MM post forwarded into each session.
        # Read by `_mention_triggerer_on_done` to @-mention that user when
        # the run ends. Cleared on use, so a single completion event only
        # pings once, and on session teardown (see `*_forget_session`).
        self._session_triggerer: dict[str, str] = {}
        # Recently-sent VD message bodies, keyed by session_id. Used to
        # de-duplicate the role=user echo VD broadcasts when the bridge
        # itself shipped the message (MM forwards, catch-up blocks,
        # firstMessage on invite/fork claims). Capped per session.
        self._recent_vd_sends: dict[str, deque[tuple[float, str]]] = {}

    # ----- lifecycle -----

    async def start(self) -> None:
        self.mm.login()
        self.typing = TypingIndicator(self.mm, self.config.typing_refresh_seconds)

        try:
            health = await self.vd.health()
            logger.info("VibeDeck health: %s", health)
        except Exception:
            logger.exception("VibeDeck health check failed — continuing anyway")

        logger.info(
            "Connected — Mattermost (team=%s, bot=%s) + VibeDeck (%s)",
            self.config.mm_team, self.mm.bot_username, self.config.vd_url,
        )

        await asyncio.gather(
            self._run_mm_listener(),
            self._run_vd_listener(),
            self._run_typing_watchdog(),
            self._run_auto_join_reconciler(),
        )

    async def stop(self) -> None:
        if self.typing:
            await self.typing.shutdown()
        await self.vd.close()

    # ----- listener loops -----

    async def _run_mm_listener(self) -> None:
        logger.info("Starting Mattermost WebSocket listener...")
        await self.mm.listen_websocket({
            "posted": self._on_mm_posted,
            "user_added": self._on_mm_user_added,
            "user_removed": self._on_mm_user_removed,
            "channel_updated": self._on_mm_channel_updated,
            "channel_created": self._on_mm_channel_created,
        })

    async def _run_auto_join_reconciler(self) -> None:
        """Periodically join all public team channels the bot is missing.

        No-op when `auto_join_public_channels` is disabled. Runs forever;
        each iteration's failures are logged but don't stop the loop.
        """
        if not self.config.auto_join_public_channels:
            return
        while True:
            try:
                await self._reconcile_auto_join_once()
            except Exception:
                logger.exception("auto-join reconciler iteration failed")
            await asyncio.sleep(self.config.auto_join_reconcile_seconds)

    async def _reconcile_auto_join_once(self) -> None:
        """One reconciler sweep — join each missing public channel, one by one.

        The mark-then-join order matters: the MM server dispatches
        `user_added` over WS the moment the HTTP join lands, and the event
        arrives on the main asyncio loop. If we marked after the join, the
        event could race in and spawn a VD session before the bridge knows
        the join was self-initiated. Marking first closes that window.
        """
        bot_ids = await asyncio.to_thread(self.mm.get_bot_channel_ids)
        public = await asyncio.to_thread(self.mm.list_public_team_channels)
        joined = 0
        for ch in public:
            cid = ch["id"]
            if cid in bot_ids:
                continue
            self._self_joined_channels.add(cid)
            try:
                await asyncio.to_thread(self.mm.join_channel, cid)
                joined += 1
                logger.info(
                    "Auto-joined channel: %s (%s)",
                    ch.get("display_name") or cid, cid,
                )
            except Exception:
                self._self_joined_channels.discard(cid)
                logger.warning(
                    "Failed to auto-join %s (%s)", ch.get("display_name") or cid, cid,
                    exc_info=True,
                )
        if joined:
            logger.info("Auto-join reconciler joined %d channel(s)", joined)

    async def _run_vd_listener(self) -> None:
        logger.info("Starting VibeDeck SSE listener...")
        await self.vd.stream_events(self._on_vd_event)

    async def _run_typing_watchdog(self) -> None:
        """Stop a typing loop if session_status hasn't been heard from recently."""
        while True:
            await asyncio.sleep(self.config.typing_refresh_seconds)
            if not self.typing:
                continue
            timeout = self.config.typing_stop_after_silence_seconds
            now = time.monotonic()
            for session_id in list(self.typing.running_sessions()):
                last = self.last_status_ts.get(session_id)
                if last is not None and now - last > timeout:
                    logger.debug(
                        "No session_status for %s in %.0fs, stopping typing",
                        session_id[:8], timeout,
                    )
                    await self.typing.stop(session_id)

    # ─────────────────────── Mattermost WS handlers ───────────────────────

    async def _on_mm_posted(self, post: dict) -> None:
        if _is_mm_system_post(post):
            return
        # Bridge CLI subcommands stamp `props.from_bridge_cli` on posts
        # they author themselves (e.g. `mm-bridge spawn`'s parent-channel
        # announcement). Those posts are visible to humans but must not
        # be forwarded to the linked session as a user turn — the daemon
        # didn't author them, so its per-process own-post tracker can't
        # suppress the WS echo on its own.
        props = post.get("props") or {}
        if isinstance(props, dict) and props.get("from_bridge_cli"):
            return
        message = (post.get("message") or "").strip()
        if not message and not post.get("file_ids"):
            return
        channel_id = post["channel_id"]
        root_id = post.get("root_id") or None

        # Thread reply?
        if root_id:
            await self._handle_thread_post(channel_id, root_id, post, message)
            return

        session_id = self.mapping.get_session(Anchor(channel_id))
        if not session_id:
            # v1's "create on first message" path is gone (§1.3). If a pending
            # session for this channel is still warming up, queue the message.
            pending = self.pending_mm_sessions.get(channel_id)
            if pending:
                pending.queued_messages.append(message)
                return
            # Auto-joined channel: session starts on first engagement.
            if self.config.auto_join_public_channels:
                await self._maybe_start_engagement_session(
                    channel_id, post, message,
                )
            return

        # First-message-after-invite: optionally reconfigure via plain tokens.
        # The flag is consumed here so subsequent messages don't re-trigger
        # config parsing. `is_first_user_message` carries forward so the
        # forwarded message (if any) can carry the MM-context preamble.
        is_first_user_message = False
        if channel_id in self.awaiting_first_message:
            self.awaiting_first_message.discard(channel_id)
            applied = await self._try_apply_first_message_config(
                channel_id, session_id, message,
            )
            if applied:
                return
            is_first_user_message = True

        # Runtime toggle (literal `autorespond` / `noautorespond`, nothing else).
        if _RUNTIME_TOGGLE_RE.match(message):
            await self._run_runtime_toggle(channel_id, message)
            return

        # Command: @claude catch up
        if m := _CATCH_UP_RE.match(message):
            await self._run_catch_up(channel_id, session_id, None, m)
            return
        # Command: @claude leave
        if m := _LEAVE_CMD_RE.match(message):
            await self._run_leave_command(
                channel_id, session_id, thread_root=None, reason=(m.group(1) or "").strip(),
            )
            return
        # Command: @claude stop (bare `stop` is honored only in autorespond mode)
        if sm := _STOP_CMD_RE.match(message):
            cfg = self.purpose_by_channel.get(channel_id)
            if sm.group("mention") or not (cfg and cfg.mention_only):
                await self._run_stop_command(channel_id, session_id, thread_root=None)
                return

        await self._forward_user_post(
            channel_id, session_id, post, message, thread_root=None,
            first_message=is_first_user_message,
        )

    async def _on_mm_user_added(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        # Self-initiated auto-join — silent presence, no session.
        if channel_id in self._self_joined_channels:
            self._self_joined_channels.discard(channel_id)
            logger.info("Auto-joined %s — silent presence (no session until engagement)", channel_id)
            return
        if self.mapping.get_session(Anchor(channel_id)):
            logger.info("Bot already mapped for channel %s — skipping", channel_id)
            return
        if channel_id in self.pending_mm_sessions:
            return
        await self._start_invited_session(channel_id)

    async def _maybe_start_engagement_session(
        self,
        channel_id: str,
        post: dict,
        message: str,
    ) -> None:
        """Start a VD session on first user engagement in an auto-joined channel.

        "Engagement" depends on the channel's effective config:
          - mention-only  → require an `@claude` / `@<bot>` mention
          - autorespond   → any non-empty message qualifies

        The engagement message itself becomes the session's first VD message
        (stripped of bot mention). Skips the first-message-config gate — the
        user is clearly talking, not configuring.
        """
        try:
            ch = await asyncio.to_thread(self.mm.get_channel, channel_id)
        except Exception:
            logger.exception("Failed to fetch channel %s for engagement", channel_id)
            return
        purpose_text = ch.get("purpose", "") or ""

        models_by_backend = await self._models_for_known_backends()
        cfg = purpose.parse(
            purpose_text,
            self.config.default_backend,
            self.config.default_model,
            lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )

        bot_mention = f"@{self.mm.bot_username}"
        mentioned = bot_mention in message or "@claude" in message.lower()
        if cfg.mention_only and not mentioned:
            return  # silent — not engaging

        cleaned = (
            message.replace(bot_mention, "").replace("@claude", "").strip()
        )
        if not cleaned and not post.get("file_ids"):
            return

        # If the very first engagement message is pure purpose tokens
        # (e.g. `@claude autorespond`, `@claude claude, sonnet`), apply
        # it as channel config without starting a session. The next
        # engagement message will start the session with these settings.
        if cleaned and self._try_apply_pre_session_config(
            channel_id, cleaned, models_by_backend,
        ):
            return

        initial = cleaned or INVITE_PLACEHOLDER

        await self._start_invited_session(
            channel_id,
            initial_message=initial,
            allow_first_message_config=False,
            post_welcome=False,
            exclude_post_id=post.get("id"),
        )

    def _try_apply_pre_session_config(
        self,
        channel_id: str,
        candidate: str,
        models_by_backend: dict[str, list[str]],
    ) -> bool:
        """If `candidate` parses cleanly as purpose tokens, apply without starting a session.

        Returns True when the message was consumed as config. Mirrors
        `_try_apply_first_message_config` but never triggers a session
        restart — there is no session yet.
        """
        parsed = purpose.parse(
            candidate,
            self.config.default_backend,
            self.config.default_model,
            lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )
        if parsed.warnings:
            return False
        if not candidate.replace(",", " ").split():
            return False

        current = self.purpose_by_channel.get(channel_id)
        merged = self._merge_configs(current, parsed)
        self.purpose_by_channel[channel_id] = merged
        self._persist_purpose(channel_id, merged)
        self._post_config_confirmation(channel_id, merged, restarted=False)
        return True

    async def _on_mm_channel_created(self, channel_id: str) -> None:
        """New channel appeared — join it if auto-join is enabled.

        Note: Mattermost scopes `channel_created` WS events to the creating
        user's session only, so this path typically fires only for channels
        the bot creates itself (e.g. `mm-bridge spawn`). Human-created public
        channels are picked up by the reconciler.
        """
        if not self.config.auto_join_public_channels:
            return
        self._self_joined_channels.add(channel_id)
        try:
            await asyncio.to_thread(self.mm.join_channel, channel_id)
            logger.info("Auto-joined newly-created channel %s", channel_id)
        except Exception:
            self._self_joined_channels.discard(channel_id)
            logger.warning(
                "Failed to auto-join newly-created channel %s", channel_id,
                exc_info=True,
            )

    async def _on_mm_user_removed(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        session_id = self.mapping.unlink(Anchor(channel_id))
        self.purpose_by_channel.pop(channel_id, None)
        self.pending_mm_sessions.pop(channel_id, None)
        self._forget_channel_silent_drops(channel_id)
        if session_id:
            self._end_tool_use_run(session_id)
            self.posters.forget(session_id)
            self._session_triggerer.pop(session_id, None)
            self._recent_vd_sends.pop(session_id, None)
            if self.typing:
                await self.typing.stop(session_id)
        logger.info("Bot removed from channel %s (session %s unlinked)",
                    channel_id, (session_id or "")[:8])

    async def _on_mm_channel_updated(self, channel: dict) -> None:
        channel_id = channel.get("id")
        if not channel_id:
            return
        prev = self.last_channel_state.get(channel_id, {})
        new_display = channel.get("display_name", "") or ""
        new_purpose = channel.get("purpose", "") or ""
        self.last_channel_state[channel_id] = {
            "display_name": new_display, "purpose": new_purpose,
        }

        session_id = self.mapping.get_session(Anchor(channel_id))
        if not session_id:
            return

        # display_name change → sync to VibeDeck
        if prev.get("display_name") != new_display and new_display:
            if self.name_sync.should_sync("mm", channel_id):
                try:
                    await self.vd.set_session_title(
                        session_id, new_display[:VD_TITLE_MAX],
                    )
                    self.name_sync.note_remote_update("vd", session_id)
                    logger.info("MM rename → VD title for %s", session_id[:8])
                except Exception:
                    logger.warning(
                        "Failed to sync MM rename → VD title for %s",
                        session_id[:8], exc_info=True,
                    )

        # Purpose change (only emit notice if actually changed AND we didn't
        # write it ourselves).
        if prev and prev.get("purpose") != new_purpose:
            self_written = self._self_written_purpose.pop(channel_id, None)
            if self_written is not None and self_written == new_purpose:
                return
            try:
                self.mm.post_message(
                    channel_id,
                    "_Channel Purpose changed — this takes effect only for new "
                    "sessions. Start a new channel (or thread) to use these "
                    "settings._",
                )
            except Exception:
                logger.warning("Failed to post purpose-change notice", exc_info=True)

    # ─────────────────────── Invite → new session ──────────────────────────

    async def _start_invited_session(
        self,
        channel_id: str,
        *,
        initial_message: str = INVITE_PLACEHOLDER,
        allow_first_message_config: bool = True,
        post_welcome: bool = True,
        exclude_post_id: str | None = None,
    ) -> None:
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            logger.exception("Failed to fetch channel %s on invite", channel_id)
            return
        purpose_text = ch.get("purpose", "") or ""
        display_name = (ch.get("display_name") or "").strip() or None

        # purpose.parse takes a sync callable; preload model lists so the
        # callable can just dict-lookup.
        models_by_backend: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models_by_backend[b] = await self.vd.list_models(b)
            except Exception:
                models_by_backend[b] = []

        cfg = purpose.parse(
            purpose_text,
            self.config.default_backend,
            self.config.default_model,
            lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )

        effective_cwd = self._resolve_purpose_cwd(cfg)

        for w in cfg.warnings:
            try:
                self.mm.post_message(channel_id, f":warning: {w}")
            except Exception:
                logger.debug("posting purpose warning failed", exc_info=True)

        model_index = None
        if cfg.model:
            for i, m in enumerate(models_by_backend.get(cfg.backend, [])):
                if m.lower() == cfg.model.lower():
                    model_index = i
                    break

        # Prepend the last N messages as catch-up context so the session
        # doesn't start cold. Only applies to brand-new sessions (not restarts).
        effective_initial = self._prepend_catch_up(
            channel_id, initial_message, exclude_post_id=exclude_post_id,
        )

        # Register pending BEFORE create_session: VD emits `session_added`
        # over SSE before the HTTP response lands, so if we registered after
        # the await, _on_vd_session_added would find no pending invite and
        # orphan the session into a new auto-created channel.
        self.pending_mm_sessions[channel_id] = PendingMattermostSession(
            channel_id=channel_id,
            cwd=effective_cwd,
            backend=cfg.backend,
            initial_message=effective_initial,
            requested_at=time.monotonic(),
            purpose_cfg=cfg,
            allow_first_message_config=allow_first_message_config,
            channel_display_name=display_name,
        )
        self.purpose_by_channel[channel_id] = cfg

        try:
            resp = await self.vd.create_session(
                message=effective_initial,
                cwd=effective_cwd,
                backend=cfg.backend,
                model_index=model_index,
            )
        except Exception:
            logger.exception("Failed to create VD session for channel %s", channel_id)
            self.pending_mm_sessions.pop(channel_id, None)
            self.purpose_by_channel.pop(channel_id, None)
            try:
                self.mm.post_message(
                    channel_id, ":warning: Failed to start a VibeDeck session.",
                )
            except Exception:
                pass
            return

        status = resp.get("status")
        if status == "permission_denied":
            self.pending_mm_sessions.pop(channel_id, None)
            self.purpose_by_channel.pop(channel_id, None)
            self.mm.post_message(
                channel_id,
                ":warning: VibeDeck could not start the session — needs additional permissions.",
            )
            return
        if status != "started":
            self.pending_mm_sessions.pop(channel_id, None)
            self.purpose_by_channel.pop(channel_id, None)
            self.mm.post_message(
                channel_id,
                f":warning: VibeDeck returned unexpected status `{status}` while starting the session.",
            )
            return

        # VD may have normalised the cwd — keep pending in sync so the claim
        # path matches projectPath from the SSE event.
        cwd = resp.get("cwd") or effective_cwd
        pending = self.pending_mm_sessions.get(channel_id)
        if pending is not None:
            pending.cwd = cwd

        if post_welcome:
            welcome = self._format_welcome(cfg, cwd)
            try:
                self.mm.post_message(channel_id, welcome)
            except Exception:
                logger.warning("Failed to post welcome message", exc_info=True)

    def _resolve_purpose_cwd(self, cfg: purpose.PurposeConfig) -> str:
        """Apply `cwd=` from the Channel Purpose, falling back to the default.

        The requested path must resolve inside `allowed_attachment_roots`;
        otherwise we append a warning to `cfg.warnings` so the channel learns
        the override was rejected, and fall back to `config.default_cwd`.
        With no allowed roots configured we trust the caller (same as
        `resolve_attachment_path`).
        """
        if not cfg.cwd:
            return self.config.default_cwd
        roots = self.config.allowed_attachment_roots
        resolved = resolve_attachment_path(cfg.cwd, project_path=None, allowed_roots=roots)
        if resolved is None:
            cfg.warnings.append(
                f"Channel Purpose `cwd={cfg.cwd}` is not inside any allowed_attachment_root "
                f"— using default `{self.config.default_cwd}`."
            )
            logger.warning(
                "Rejected Channel Purpose cwd=%r (not in allowed_attachment_roots)", cfg.cwd,
            )
            return self.config.default_cwd
        logger.info("Channel Purpose cwd override → %s", resolved)
        return str(resolved)

    # ─────────────────── First-message config + runtime toggle ─────────────

    async def _try_apply_first_message_config(
        self,
        channel_id: str,
        session_id: str,
        message: str,
    ) -> bool:
        """If `message` parses cleanly as Channel Purpose tokens, apply it.

        Returns True when the message was consumed as config (caller should
        not forward it) and False when it should be treated as a normal
        message.

        A "clean parse" means no warnings and the tokenised form is non-empty
        — so e.g. ``hello world`` is not config, but ``claude, haiku``,
        ``autorespond``, or ``cwd=/foo`` are.
        """
        candidate = message.strip()
        if not candidate:
            return False

        models_by_backend: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models_by_backend[b] = await self.vd.list_models(b)
            except Exception:
                models_by_backend[b] = []

        parsed = purpose.parse(
            candidate,
            self.config.default_backend,
            self.config.default_model,
            lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )
        # Unknown tokens → parser attaches warnings. Unless ALL tokens were
        # known, bail out and treat the message as text.
        if parsed.warnings:
            return False
        if not candidate.replace(",", " ").split():
            return False

        # Merge: start from the current purpose (if any) so flags stick
        # unless the new tokens override them.
        current = self.purpose_by_channel.get(channel_id)
        merged = self._merge_configs(current, parsed)

        current_cwd = current.cwd if current else None
        needs_restart = bool(
            current and (
                merged.backend != current.backend
                or merged.model != current.model
                or merged.cwd != current_cwd
            )
        )

        if needs_restart:
            await self._restart_session_with_config(channel_id, session_id, merged)
        else:
            self.purpose_by_channel[channel_id] = merged

        self._persist_purpose(channel_id, merged)
        self._post_config_confirmation(channel_id, merged, restarted=needs_restart)
        return True

    def _merge_configs(
        self,
        current: purpose.PurposeConfig | None,
        new: purpose.PurposeConfig,
    ) -> purpose.PurposeConfig:
        """Layer `new` on top of `current`. Any field explicitly set by the
        new parse wins; omitted fields fall back to current."""
        if current is None:
            return new
        return purpose.PurposeConfig(
            backend=new.backend,
            model=new.model if new.model is not None else current.model,
            mention_only=new.mention_only,
            cwd=new.cwd if new.cwd is not None else current.cwd,
            warnings=[],
        )

    async def _restart_session_with_config(
        self,
        channel_id: str,
        old_session_id: str,
        cfg: purpose.PurposeConfig,
    ) -> None:
        """Tear down the current session for `channel_id` and start a new one.

        We keep the MM channel and mapping slot; only the VD session is
        replaced. The old session is abandoned (no explicit VD-side delete).
        """
        self.mapping.unlink(Anchor(channel_id))
        self._end_tool_use_run(old_session_id)
        self.posters.forget(old_session_id)
        self._forget_channel_silent_drops(channel_id)
        self._session_triggerer.pop(old_session_id, None)
        self._recent_vd_sends.pop(old_session_id, None)
        if self.typing:
            await self.typing.stop(old_session_id)
        effective_cwd = self._resolve_purpose_cwd(cfg)

        models_by_backend = await self._models_for_known_backends()
        model_index = self._resolve_model_index(cfg, models_by_backend)

        self.pending_mm_sessions[channel_id] = PendingMattermostSession(
            channel_id=channel_id,
            cwd=effective_cwd,
            backend=cfg.backend,
            initial_message=INVITE_PLACEHOLDER,
            requested_at=time.monotonic(),
            purpose_cfg=cfg,
        )
        self.purpose_by_channel[channel_id] = cfg

        try:
            resp = await self.vd.create_session(
                message=INVITE_PLACEHOLDER,
                cwd=effective_cwd,
                backend=cfg.backend,
                model_index=model_index,
            )
        except Exception:
            logger.exception("Failed to restart VD session for %s", channel_id)
            self.pending_mm_sessions.pop(channel_id, None)
            try:
                self.mm.post_message(
                    channel_id,
                    ":warning: Failed to restart the VibeDeck session.",
                )
            except Exception:
                pass
            return

        pending = self.pending_mm_sessions.get(channel_id)
        if pending is not None:
            pending.cwd = resp.get("cwd") or effective_cwd

    async def _models_for_known_backends(self) -> dict[str, list[str]]:
        models: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models[b] = await self.vd.list_models(b)
            except Exception:
                models[b] = []
        return models

    def _resolve_model_index(
        self, cfg: purpose.PurposeConfig, models_by_backend: dict[str, list[str]],
    ) -> int | None:
        if not cfg.model:
            return None
        for i, m in enumerate(models_by_backend.get(cfg.backend, [])):
            if m.lower() == cfg.model.lower():
                return i
        return None

    async def _run_runtime_toggle(self, channel_id: str, message: str) -> None:
        """Handle a literal `autorespond` / `noautorespond` message.

        Flips the channel's mention_only flag and persists to Channel Purpose.
        Does not forward the token.
        """
        turn_on_autorespond = message.strip().lower() in purpose.AUTORESPOND_ALIASES
        current = self.purpose_by_channel.get(channel_id) or purpose.PurposeConfig(
            backend=self.config.default_backend,
            model=self.config.default_model,
            mention_only=not self.config.default_autorespond,
        )
        updated = purpose.PurposeConfig(
            backend=current.backend,
            model=current.model,
            mention_only=not turn_on_autorespond,
            cwd=current.cwd,
            warnings=[],
        )
        self.purpose_by_channel[channel_id] = updated
        self._persist_purpose(channel_id, updated)
        note = (
            ":loud_sound: _Autorespond on — I'll reply to every message._"
            if turn_on_autorespond
            else ":mute: _Mention-only — @mention me to talk._"
        )
        try:
            self.mm.post_message(channel_id, note)
        except Exception:
            logger.debug("failed posting runtime-toggle confirmation", exc_info=True)

    def _persist_purpose(
        self, channel_id: str, cfg: purpose.PurposeConfig,
    ) -> None:
        """Write the canonical form of `cfg` back to the MM channel's Purpose.

        Marks the resulting `channel_updated` event as self-triggered so the
        bridge doesn't post a "purpose changed" notice to itself.
        """
        serialized = purpose.to_purpose_string(
            cfg, default_autorespond=self.config.default_autorespond,
        )
        self._note_self_wrote_purpose(channel_id, serialized)
        try:
            self.mm.set_channel_purpose(channel_id, serialized)
        except Exception:
            logger.warning(
                "Failed to persist Channel Purpose for %s", channel_id, exc_info=True,
            )

    def _note_self_wrote_purpose(self, channel_id: str, purpose_text: str) -> None:
        self._self_written_purpose[channel_id] = purpose_text

    def _post_config_confirmation(
        self,
        channel_id: str,
        cfg: purpose.PurposeConfig,
        *,
        restarted: bool,
    ) -> None:
        head = f"backend: `{cfg.backend}`, model: `{cfg.model or 'default'}`"
        flag = "mention-only" if cfg.mention_only else "autorespond"
        cwd_note = f", cwd: `{cfg.cwd}`" if cfg.cwd else ""
        suffix = " (session restarted)" if restarted else ""
        note = f":gear: _Config applied — {head}, {flag}{cwd_note}._{suffix}"
        try:
            self.mm.post_message(channel_id, note)
        except Exception:
            logger.debug("failed posting config confirmation", exc_info=True)

    def _format_welcome(self, cfg: purpose.PurposeConfig, cwd: str) -> str:
        parts = [
            f"*Session started — backend: `{cfg.backend}`",
            f"model: `{cfg.model or 'default'}`",
            f"cwd: `{cwd}`*",
        ]
        head = ", ".join(parts)
        hint = (
            "Hi! Reply to catch me up, or just start asking. "
            f"Use `@claude catch up {self.config.catch_up_default_n}` to include "
            f"the last {self.config.catch_up_default_n} messages."
        )
        if cfg.mention_only:
            hint = f"_mention-only mode — @mention me to talk._\n{hint}"
        config_hint = (
            "_First message can reconfigure: send e.g. `claude, sonnet, "
            "autorespond` to switch. After that, the literal word "
            "`autorespond` or `noautorespond` toggles auto-reply._"
        )
        return f"{head}\n\n{hint}\n\n{config_hint}"

    # ─────────────────────── Forwarding user posts ─────────────────────────

    async def _forward_user_post(
        self,
        channel_id: str,
        session_id: str,
        post: dict,
        message: str,
        thread_root: str | None,
        first_message: bool = False,
    ) -> None:
        cfg = self.purpose_by_channel.get(channel_id)
        bot_mention = f"@{self.mm.bot_username}"

        if cfg and cfg.mention_only and bot_mention not in message:
            # Silently dropped for now; remember it so the next forwarded
            # message in this anchor can prepend it as catch-up context.
            self._enqueue_silent_drop(channel_id, thread_root, post)
            return

        # Strip the @-mention so Claude doesn't see a stray handle.
        cleaned = message.replace(bot_mention, "").replace("@claude", "").strip()

        # Download any inbound MM attachments into the session's cwd.
        attachment_notes: list[str] = []
        if post.get("file_ids"):
            try:
                cwd = await self._project_path_for(session_id)
            except Exception:
                logger.exception("project-path lookup failed for %s", session_id[:8])
                cwd = None
            if cwd:
                attachment_notes = await self._save_mm_attachments(post, cwd)
            else:
                attachment_notes = ["[MM attachment skipped: no project path for session]"]

        if not cleaned and not attachment_notes:
            return

        user_id = post.get("user_id", "")
        attribute = self.posters.note_post(session_id, user_id)
        if user_id:
            self._session_triggerer[session_id] = user_id
        if attribute and cleaned:
            username = self._resolve_username(user_id)
            cleaned = self.posters.format(cleaned, username, True)

        body = cleaned
        if attachment_notes:
            notes_block = "\n".join(attachment_notes)
            body = f"{notes_block}\n\n{body}" if body else notes_block

        silent_block, silent_key = await self._peek_silent_drops_as_block(
            channel_id, thread_root, session_id, exclude_post_id=post.get("id"),
        )
        if silent_block:
            body = f"{silent_block}\n\n{body}" if body else silent_block

        if first_message:
            preamble = await self._compute_first_message_preamble(channel_id)
            if preamble:
                body = f"{preamble}\n\n{body}" if body else preamble

        logger.info("MM → VD [%s]: %s", session_id[:8], body[:80])
        self._record_vd_send(session_id, body)
        try:
            await self.vd.send_message(session_id, body)
        except Exception:
            logger.exception("Failed to send to VD session %s", session_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Failed to deliver the message to the session.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            # Preserve the previously-queued drops AND enqueue the
            # current post too: the user's actual request would
            # otherwise vanish with the failed send. The next mention
            # replays everything that wasn't delivered.
            self._enqueue_silent_drop(channel_id, thread_root, post)
        else:
            self._clear_silent_drops(silent_key)

    def _resolve_username(self, user_id: str) -> str:
        try:
            u = self.mm.get_user(user_id)
            return u.get("username") or user_id[:8]
        except Exception:
            return user_id[:8]

    async def _compute_first_message_preamble(self, channel_id: str) -> str:
        """Build the MM-context preamble for the first forwarded user message.

        Returns an empty string if the MM lookups fail — we'd rather ship the
        user's message without a preamble than swallow it entirely.
        """
        try:
            ch = await asyncio.to_thread(self.mm.get_channel, channel_id)
        except Exception:
            logger.warning(
                "Failed to fetch channel %s for first-message preamble",
                channel_id, exc_info=True,
            )
            return ""
        channel_name = (
            ch.get("display_name") or ch.get("name") or channel_id
        ).strip() or channel_id

        try:
            members = await asyncio.to_thread(
                self.mm.get_channel_members, channel_id,
            )
        except Exception:
            logger.warning(
                "Failed to fetch members for %s (first-message preamble)",
                channel_id, exc_info=True,
            )
            members = []

        human_usernames: list[str] = []
        for m in members or []:
            uid = m.get("user_id")
            if not uid or uid == self.mm.bot_user_id:
                continue
            try:
                u = await asyncio.to_thread(self.mm.get_user, uid)
            except Exception:
                logger.debug(
                    "Failed to resolve user %s for preamble", uid, exc_info=True,
                )
                continue
            if u.get("is_bot"):
                continue
            uname = u.get("username")
            if uname:
                human_usernames.append(uname)

        return _format_first_message_preamble(channel_name, human_usernames)

    # ─────────────────────── Thread forks ──────────────────────────────────

    async def _handle_thread_post(
        self,
        channel_id: str,
        root_id: str,
        post: dict,
        message: str,
    ) -> None:
        if (channel_id, root_id) in self.dead_threads:
            return

        # Leave command inside a thread only removes the thread mapping.
        if m := _LEAVE_CMD_RE.match(message):
            await self._run_leave_command(
                channel_id, session_id=None, thread_root=root_id,
                reason=(m.group(1) or "").strip(),
            )
            return

        thread_session = self.mapping.get_session(Anchor(channel_id, root_id))
        if thread_session:
            if cm := _CATCH_UP_RE.match(message):
                await self._run_catch_up(channel_id, thread_session, root_id, cm)
                return
            if sm := _STOP_CMD_RE.match(message):
                cfg = self.purpose_by_channel.get(channel_id)
                if sm.group("mention") or not (cfg and cfg.mention_only):
                    await self._run_stop_command(
                        channel_id, thread_session, thread_root=root_id,
                    )
                    return
            await self._forward_user_post(
                channel_id, thread_session, post, message, thread_root=root_id,
            )
            return

        # No mapping yet — try to fork.
        parent_session = self.mapping.get_session(Anchor(channel_id))
        if not parent_session:
            return  # thread in an unmapped channel

        parent_meta = await self.vd.get_session_meta(parent_session)
        cwd = parent_meta.get("projectPath") or self.config.default_cwd

        # Download attachments into the parent's cwd (the fork inherits it).
        attachment_notes: list[str] = []
        if post.get("file_ids") and cwd:
            attachment_notes = await self._save_mm_attachments(post, cwd)

        message_for_llm = message
        if attachment_notes:
            notes_block = "\n".join(attachment_notes)
            message_for_llm = f"{notes_block}\n\n{message}" if message else notes_block

        fork_message = self._wrap_thread_fork_message(channel_id, root_id, message_for_llm)

        try:
            resp = await self.vd.fork_session(parent_session, fork_message)
        except Exception:
            logger.exception("fork_session error for session %s", parent_session[:8])
            self._mark_dead_thread(channel_id, root_id,
                                   "Couldn't fork this conversation.")
            return

        if resp.get("status") == "fork_unavailable":
            self._mark_dead_thread(channel_id, root_id,
                                   f"Couldn't fork ({resp.get('reason', 'unsupported')}).")
            return

        self.pending_forks.append(PendingMattermostSession(
            channel_id=channel_id,
            cwd=cwd,
            backend=parent_meta.get("backend") or None,
            initial_message=fork_message,
            requested_at=time.monotonic(),
            is_fork=True,
            fork_parent_session=parent_session,
            fork_thread_channel=channel_id,
            fork_thread_root=root_id,
        ))
        logger.info("Thread fork requested for %s:%s from parent %s",
                    channel_id, root_id[:8], parent_session[:8])

    def _wrap_thread_fork_message(
        self, channel_id: str, root_id: str, message: str,
    ) -> str:
        """Prefix the fork's initial message with thread context for the LLM."""
        quoted_root = ""
        try:
            root_post = self.mm.get_post(root_id)
            root_text = (root_post.get("message") or "").strip()
            if root_text:
                quoted_root = "\n".join(f"> {line}" for line in root_text.splitlines())
        except Exception:
            logger.debug("Failed to fetch thread-root post %s", root_id, exc_info=True)

        header = (
            "[Mattermost thread context] You are continuing the parent "
            "conversation in a Mattermost thread. The user replied to this "
            "message:\n\n"
            f"{quoted_root or '> (original message could not be retrieved)'}\n\n"
            "Their reply follows:\n\n"
        )
        return header + message

    def _mark_dead_thread(
        self, channel_id: str, root_id: str, reason_text: str,
    ) -> None:
        self.dead_threads.add((channel_id, root_id))
        try:
            self.mm.post(
                channel_id,
                f":warning: {reason_text} Reply in the main channel instead.",
                root_id=root_id,
            )
        except Exception:
            logger.warning("Failed to post dead-thread notice", exc_info=True)

    # ─────────────────────── Catch-up & leave ──────────────────────────────

    def _enqueue_silent_drop(
        self,
        channel_id: str,
        thread_root: str | None,
        post: dict,
    ) -> None:
        """Record a mention-only drop so the next forwarded message in
        this anchor can replay it as catch-up. Capped at
        ``config.initial_catch_up_n``; ``<= 0`` disables entirely."""
        cap = self.config.initial_catch_up_n
        if cap <= 0:
            return
        key = (channel_id, thread_root)
        q = self._silent_drops.get(key)
        if q is None or q.maxlen != cap:
            # Rebuild on cap change so runtime config tweaks apply.
            old = list(q) if q is not None else []
            q = deque(old[-cap:], maxlen=cap)
            self._silent_drops[key] = q
        q.append(post)

    async def _peek_silent_drops_as_block(
        self,
        channel_id: str,
        thread_root: str | None,
        session_id: str,
        *,
        exclude_post_id: str | None = None,
    ) -> tuple[str, tuple[str, str | None] | None]:
        """Render pending silent drops for ``(channel_id, thread_root)``
        as a catch-up block without mutating the queue. Downloads any
        attachments on queued posts into the session cwd so the replay
        carries the file the user uploaded before mentioning the bot.
        Returns ``(block_text, clear_key)``; callers should only pass
        ``clear_key`` to :meth:`_clear_silent_drops` after the forwarded
        message has been successfully delivered to VD, so a transient
        VD send failure doesn't discard queued conversation.
        """
        key = (channel_id, thread_root)
        dropped = self._silent_drops.get(key)
        if not dropped:
            return "", None
        cwd: str | None = None
        if any(p.get("file_ids") for p in dropped):
            try:
                cwd = await self._project_path_for(session_id)
            except Exception:
                logger.exception(
                    "project-path lookup failed while replaying drops for %s",
                    session_id[:8],
                )
                cwd = None
        lines: list[str] = []
        for p in dropped:
            if exclude_post_id and p.get("id") == exclude_post_id:
                continue
            if self.mm.is_own_post(p.get("id", "")):
                continue
            if _is_mm_system_post(p):
                continue
            msg = (p.get("message") or "").strip()
            note_lines: list[str] = []
            if p.get("file_ids"):
                if cwd:
                    note_lines = await self._save_mm_attachments(p, cwd)
                else:
                    note_lines = ["[MM attachment skipped: no project path for session]"]
            if not msg and not note_lines:
                continue
            username = self._resolve_username(p.get("user_id", ""))
            rendered = "\n".join([*note_lines, msg] if msg else note_lines)
            lines.append(f"{username}: {rendered}")
        if not lines:
            return "", None
        return self._format_catch_up_block(lines), key

    def _clear_silent_drops(
        self, key: tuple[str, str | None] | None,
    ) -> None:
        if key is not None:
            self._silent_drops.pop(key, None)

    def _drain_silent_drops_matching(
        self,
        channel_id: str,
        thread_root: str | None,
        ids: set[str],
    ) -> None:
        """Remove only queue entries whose post id is in ``ids``.

        Used after an explicit ``@claude catch up`` so that a partial
        catch-up doesn't throw away queued messages it never surfaced.
        """
        if not ids:
            return
        key = (channel_id, thread_root)
        q = self._silent_drops.get(key)
        if q is None:
            return
        kept = [p for p in q if p.get("id", "") not in ids]
        if not kept:
            self._silent_drops.pop(key, None)
            return
        if len(kept) == len(q):
            return
        # Preserve the deque's original maxlen so the cap survives.
        self._silent_drops[key] = deque(kept, maxlen=q.maxlen)

    def _forget_channel_silent_drops(self, channel_id: str) -> None:
        """Drop every queued silent-drop entry for this channel (root
        and any threads). Called on teardown paths so drops from a
        previous session don't leak into the next one mapped here."""
        for key in [k for k in self._silent_drops if k[0] == channel_id]:
            self._silent_drops.pop(key, None)

    def _forget_thread_silent_drops(
        self, channel_id: str, thread_root: str,
    ) -> None:
        self._silent_drops.pop((channel_id, thread_root), None)

    def _collect_catch_up_lines(
        self,
        channel_id: str,
        n: int,
        *,
        exclude_post_id: str | None = None,
    ) -> tuple[list[str], set[str]]:
        """Fetch up to `n` user messages from a channel, oldest-first, as
        `username: message` lines. Returns ``(lines, included_post_ids)``
        — the id set lets callers scope queue cleanup to only the posts
        that were actually surfaced. Skips bot posts, system posts, and
        any `@claude catch up` commands themselves.

        The `user_id == bot_user_id` filter is intentionally blanket: on
        a restart `is_own_post()` is empty-set, so using it here would
        let prior replies of this same bridge leak into catch-up context
        and confuse the model with its own earlier output. Cross-bot
        posts (sibling sessions, etc.) are already handled live by the
        dispatcher in `mm_client._dispatch_event`; history replay only
        needs to surface user-authored messages.
        """
        try:
            posts = self.mm.get_posts(channel_id, max(n + 2, 1))
        except Exception:
            logger.exception("get_posts failed for catch-up")
            return [], set()

        lines: list[str] = []
        included_ids: set[str] = set()
        for p in posts:
            if exclude_post_id and p.get("id") == exclude_post_id:
                continue
            if p.get("user_id") == self.mm.bot_user_id:
                continue
            if _is_mm_system_post(p):
                continue
            if _CATCH_UP_RE.match((p.get("message") or "").strip()):
                continue
            username = self._resolve_username(p.get("user_id", ""))
            lines.append(f"{username}: {p.get('message', '')}")
            pid = p.get("id")
            if pid:
                included_ids.add(pid)
            if len(lines) >= n:
                break
        return lines, included_ids

    @staticmethod
    def _format_catch_up_block(lines: list[str]) -> str:
        return (
            f"[Catch-up context — last {len(lines)} messages from this channel, oldest first]\n"
            + "\n".join(lines) + "\n[End of catch-up]"
        )

    def _prepend_catch_up(
        self,
        channel_id: str,
        initial_message: str,
        *,
        exclude_post_id: str | None = None,
    ) -> str:
        """Prepend the channel's recent history to `initial_message` when the
        `initial_catch_up_n` config is enabled and there's actual history to
        quote. Returns `initial_message` unchanged when the block is empty.
        """
        n = self.config.initial_catch_up_n
        if n <= 0:
            return initial_message
        lines, _ = self._collect_catch_up_lines(
            channel_id, n, exclude_post_id=exclude_post_id,
        )
        if not lines:
            return initial_message
        block = self._format_catch_up_block(lines)
        return f"{block}\n\n{initial_message}"

    async def _run_catch_up(
        self,
        channel_id: str,
        session_id: str,
        thread_root: str | None,
        match: re.Match,
    ) -> None:
        n_arg = match.group(1)
        n = int(n_arg) if n_arg else self.config.catch_up_default_n
        clamped = False
        if n > self.config.catch_up_max_n:
            n = self.config.catch_up_max_n
            clamped = True

        lines, included_ids = self._collect_catch_up_lines(channel_id, n)
        block = self._format_catch_up_block(lines)

        self._record_vd_send(session_id, block)
        try:
            await self.vd.send_message(session_id, block)
        except Exception:
            logger.exception("Failed to send catch-up block")
            return

        # Explicit catch-up already surfaced these messages from MM
        # history; drop only the queue entries that were actually
        # replayed. A smaller catch-up (e.g. `catch up 1`) or one
        # outside the queue's window must leave the rest intact so the
        # next mention still sees the missed conversation.
        self._drain_silent_drops_matching(
            channel_id, thread_root, included_ids,
        )

        note = (
            f":arrows_counterclockwise: Sent the last {len(lines)} messages as context."
        )
        if clamped:
            note += f" (Clamped to {self.config.catch_up_max_n}.)"
        try:
            self.mm.post(channel_id, note, root_id=thread_root)
        except Exception:
            pass

    async def _run_leave_command(
        self,
        channel_id: str,
        session_id: str | None,
        thread_root: str | None,
        reason: str,
    ) -> None:
        if thread_root:
            removed = self.mapping.unlink(Anchor(channel_id, thread_root))
            self.dead_threads.add((channel_id, thread_root))
            self._forget_thread_silent_drops(channel_id, thread_root)
            if removed:
                self._end_tool_use_run(removed)
                self.posters.forget(removed)
                self._session_triggerer.pop(removed, None)
                self._recent_vd_sends.pop(removed, None)
                if self.typing:
                    await self.typing.stop(removed)
            farewell = (
                f"Leaving this thread: {reason}" if reason
                else "Leaving this thread — reply in the main channel to continue."
            )
            try:
                self.mm.post(channel_id, farewell, root_id=thread_root)
            except Exception:
                pass
            return

        if not session_id:
            return  # nothing to leave from

        farewell = (
            f"Leaving: {reason}" if reason
            else "Leaving — invite me back any time for a fresh session."
        )
        await self._leave_channel(channel_id, session_id, farewell)

    async def _run_stop_command(
        self,
        channel_id: str,
        session_id: str,
        thread_root: str | None,
    ) -> None:
        try:
            await self.vd.interrupt_session(session_id)
        except Exception:
            logger.exception("Failed to interrupt VD session %s", session_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Couldn't interrupt the session.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        self._end_tool_use_run(session_id)
        if self.typing:
            await self.typing.stop(session_id)
        logger.info("MM → VD [%s]: stop (interrupt)", session_id[:8])
        try:
            self.mm.post(channel_id, ":octagonal_sign: Stopped.", root_id=thread_root)
        except Exception:
            pass

    async def _leave_channel(
        self,
        channel_id: str,
        session_id: str,
        farewell: str | None,
    ) -> None:
        if farewell:
            try:
                self.mm.post_message(channel_id, farewell)
            except Exception:
                pass
        try:
            self.mm.remove_self_from_channel(channel_id)
        except Exception:
            logger.warning("Failed to remove self from channel %s", channel_id, exc_info=True)
            try:
                self.mm.post_message(
                    channel_id, ":warning: Failed to leave the channel.",
                )
            except Exception:
                pass
            return
        self.mapping.unlink(Anchor(channel_id))
        self.purpose_by_channel.pop(channel_id, None)
        self._forget_channel_silent_drops(channel_id)
        self._end_tool_use_run(session_id)
        self.posters.forget(session_id)
        self._session_triggerer.pop(session_id, None)
        self._recent_vd_sends.pop(session_id, None)
        if self.typing:
            await self.typing.stop(session_id)

    # ─────────────────────── VibeDeck SSE handlers ─────────────────────────

    async def _on_vd_event(self, event_type: str, data: dict) -> None:
        if event_type == "session_added":
            await self._on_vd_session_added(data)
        elif event_type == "message":
            await self._on_vd_message(data)
        elif event_type == "session_summary_updated":
            await self._on_vd_summary_updated(data)
        elif event_type == "session_status":
            await self._on_vd_session_status(data)

    async def _on_vd_session_added(self, data: dict) -> None:
        session_id = data.get("id") or data.get("session_id") or ""
        if not session_id:
            return
        if self.mapping.get_anchor(session_id):
            return  # already mapped

        # First try fork-pending (thread invites).
        if await self._claim_pending_fork(session_id, data):
            return
        # Then try invite-pending (channel invites).
        if await self._claim_pending_invite(session_id, data):
            return
        # Otherwise — CLI-originated session: create a fresh channel.
        self._create_channel_for_session(data)

    def _expire_pending(self) -> None:
        now = time.monotonic()
        window = self.config.pending_session_merge_window_seconds
        expired = [
            cid for cid, p in self.pending_mm_sessions.items()
            if now - p.requested_at >= window
        ]
        for cid in expired:
            logger.warning("Pending invite expired for channel %s", cid)
            self.pending_mm_sessions.pop(cid, None)
        self.pending_forks = [
            p for p in self.pending_forks
            if now - p.requested_at < window
        ]

    async def _claim_pending_invite(self, session_id: str, data: dict) -> bool:
        self._expire_pending()
        incoming_cwd = _normalize_path(data.get("projectPath"))
        incoming_backend_canon = vd_client.canon_backend(data.get("backend"))
        first_msg = (data.get("firstMessage") or "").strip()

        if not incoming_cwd:
            return False

        candidates: list[tuple[str, PendingMattermostSession]] = []
        for channel_id, pending in self.pending_mm_sessions.items():
            if _normalize_path(pending.cwd) != incoming_cwd:
                continue
            pending_backend_canon = vd_client.canon_backend(pending.backend)
            if (pending_backend_canon and incoming_backend_canon
                    and pending_backend_canon != incoming_backend_canon):
                continue
            if first_msg:
                pending_msg = pending.initial_message.strip()
                if not (
                    pending_msg.startswith(first_msg)
                    or first_msg.startswith(pending_msg)
                ):
                    continue
            candidates.append((channel_id, pending))

        if len(candidates) != 1:
            return False

        channel_id, pending = candidates[0]
        self.mapping.link(Anchor(channel_id), session_id)
        self.pending_mm_sessions.pop(channel_id, None)
        if pending.allow_first_message_config:
            self.awaiting_first_message.add(channel_id)
        # The role=user echo VD will broadcast for this session's first
        # turn carries the *full* body we shipped via `create_session`.
        # `data["firstMessage"]` is the SSE-side preview field, which
        # every backend's `get_first_user_message` truncates to 200 chars
        # (see VD `backends/*/discovery.py`, `backends/claude_code/tailer.py`)
        # — so dedup against `first_msg` misses on any session whose
        # `effective_initial` exceeds 200 chars (almost always true once
        # `initial_catch_up_n` prepends a catch-up block). Record the
        # full `pending.initial_message` instead.
        if pending.initial_message:
            self._record_vd_send(session_id, pending.initial_message)
        logger.info("Claimed pending invite: channel %s → session %s",
                    channel_id, session_id[:8])
        if pending.channel_display_name:
            try:
                await self.vd.set_session_title(
                    session_id, pending.channel_display_name,
                )
            except Exception:
                logger.warning(
                    "Failed to seed VD session title for %s", session_id[:8],
                    exc_info=True,
                )
        await self._flush_queued(channel_id, session_id, pending)
        return True

    async def _claim_pending_fork(self, session_id: str, data: dict) -> bool:
        self._expire_pending()
        incoming_cwd = _normalize_path(data.get("projectPath"))

        # Fork claims can't use firstMessage: on Claude Code, a forked
        # session's firstMessage is the parent's context-continuation summary,
        # not the thread message. Match purely on cwd; if multiple forks are
        # pending for the same cwd, take the oldest one (FIFO).
        candidates: list[PendingMattermostSession] = [
            p for p in self.pending_forks
            if not incoming_cwd or _normalize_path(p.cwd) == incoming_cwd
        ]

        if not candidates:
            return False
        candidates.sort(key=lambda p: p.requested_at)
        pending = candidates[0]
        self.pending_forks.remove(pending)
        ch_id = pending.fork_thread_channel or ""
        root_id = pending.fork_thread_root or ""
        if not ch_id or not root_id:
            return False
        self.mapping.link(Anchor(ch_id, root_id), session_id)
        # On Claude Code, the fork's firstMessage is a synthetic
        # continuation summary — never user-typed input, so suppress it
        # from the direct-user-message mirror.
        synth_first = (data.get("firstMessage") or "").strip()
        if synth_first:
            self._record_vd_send(session_id, synth_first)
        logger.info("Claimed pending fork: thread %s:%s → session %s",
                    ch_id, root_id[:8], session_id[:8])
        try:
            self.mm.post(
                ch_id,
                ":information_source: _Forked conversation. The full history of the "
                "parent session up to its current state is included — not only up to "
                "the message you replied on._",
                root_id=root_id,
            )
        except Exception:
            logger.debug("Failed to post fork disclaimer", exc_info=True)
        return True

    async def _flush_queued(
        self,
        channel_id: str,
        session_id: str,
        pending: PendingMattermostSession,
    ) -> None:
        for msg in pending.queued_messages:
            self._record_vd_send(session_id, msg)
            try:
                await self.vd.send_message(session_id, msg)
            except Exception:
                logger.exception("Failed to flush queued message")
                try:
                    self.mm.post_message(
                        channel_id,
                        ":warning: Failed to deliver a queued message to the new session.",
                    )
                except Exception:
                    pass
                break

    def _create_channel_for_session(self, data: dict) -> str | None:
        session_id = data.get("id") or data.get("session_id") or ""
        if not session_id:
            return None
        channel_name = _session_to_channel_name(session_id)
        display_name = (
            data.get("summaryTitle")
            or data.get("projectName")
            or data.get("project", "")
            or session_id[:12]
        )
        display_name = str(display_name)[:MM_DISPLAY_NAME_MAX]
        try:
            ch = self.mm.create_channel(
                name=channel_name,
                display_name=display_name,
                purpose=f"VibeDeck session {session_id}",
            )
            channel_id = ch["id"]
            self.mapping.link(Anchor(channel_id), session_id)
            logger.info(
                "Created channel %s (%s) for VD session %s",
                display_name, channel_name, session_id[:12],
            )
            return channel_id
        except Exception:
            logger.exception("Failed to create channel for session %s", session_id[:12])
            return None

    # ----- VibeDeck message → Mattermost post -----

    async def _on_vd_message(self, data: dict) -> None:
        session_id = data.get("session_id", "")
        msg = data.get("message", {}) or {}
        role = msg.get("role")
        if role == "user":
            await self._maybe_mirror_user_message(session_id, msg)
            return
        if role != "assistant":
            return

        anchor = self.mapping.get_anchor(session_id)
        channel_id = anchor.channel_id if anchor else ""
        thread_root = anchor.root_id if anchor else None
        if not channel_id:
            return

        # Walk blocks in order. tool_use blocks coalesce into a per-session
        # placeholder (unless `show_tool_use` is off, in which case they're
        # silently dropped); text/error blocks end the run and post normally.
        for block in msg.get("blocks", []):
            btype = block.get("type")
            if btype == "tool_use":
                if not self.config.show_tool_use:
                    continue
                tool = block.get("tool_name", "unknown")
                self._upsert_tool_use(session_id, channel_id, thread_root, tool)
            elif btype == "tool_result" and block.get("is_error"):
                self._end_tool_use_run(session_id)
                err_text = f"**Tool error:** {str(block.get('content',''))[:500]}"
                try:
                    self.mm.post(
                        channel_id, _truncate_for_mm(err_text), root_id=thread_root,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post tool error for session %s", session_id[:8],
                    )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                await self._handle_assistant_text_block(
                    session_id, channel_id, thread_root, text,
                )

    # ----- direct user-message mirroring -----

    _RECENT_VD_SEND_MAX = 32

    def _record_vd_send(self, session_id: str, body: str) -> None:
        """Remember that we just shipped ``body`` to ``session_id`` so the
        SSE echo can be suppressed by ``_consume_dedup_match``."""
        if not session_id or not body:
            return
        q = self._recent_vd_sends.setdefault(
            session_id, deque(maxlen=self._RECENT_VD_SEND_MAX),
        )
        q.append((time.monotonic(), body))

    def _consume_dedup_match(self, session_id: str, body: str) -> bool:
        """Return True (and pop the matching entry) if ``body`` matches a
        recent VD send for ``session_id`` within the configured window."""
        q = self._recent_vd_sends.get(session_id)
        if not q:
            return False
        window = self.config.direct_user_message_dedup_window_seconds
        now = time.monotonic()
        while q and now - q[0][0] > window:
            q.popleft()
        for i, (_ts, recorded) in enumerate(q):
            if recorded == body:
                del q[i]
                return True
        return False

    async def _maybe_mirror_user_message(self, session_id: str, msg: dict) -> None:
        """Post role=user events into the bound MM channel when they
        represent direct input to the agent (typed in the local CLI/UI),
        not bridge-originated sends or tool results."""
        if not self.config.mirror_direct_user_messages:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return  # not bound — nothing to mirror to

        text_parts: list[str] = []
        for block in msg.get("blocks", []) or []:
            if block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            return  # tool_result or empty turn — skip

        if self._consume_dedup_match(session_id, text):
            return  # echo of a body we just sent

        body = f"_via coding agent:_ {text}"
        try:
            self.mm.post(
                anchor.channel_id, _truncate_for_mm(body), root_id=anchor.root_id,
            )
        except Exception:
            logger.exception(
                "Failed to mirror direct user message for session %s",
                session_id[:8],
            )

    async def _handle_assistant_text_block(
        self,
        session_id: str,
        channel_id: str,
        thread_root: str | None,
        text: str,
    ) -> None:
        """Real assistant response — clear the tool-use placeholder, then
        run the existing directives / attachments / post pipeline.
        """
        self._end_tool_use_run(session_id)

        cleaned, dirs = directives.extract(text)

        # <leaveChannel/> takes precedence.
        leave = next((d for d in dirs if d.kind == "leave_channel"), None)
        if leave:
            reason = leave.attrs.get("reason", "").strip()
            body = cleaned.strip()
            if not body and reason:
                body = f"_Leaving: {reason}_"
            elif not body:
                body = "_Leaving._"
            if thread_root:
                # Only remove the thread mapping, not the channel.
                try:
                    self.mm.post(channel_id, _truncate_for_mm(body), root_id=thread_root)
                except Exception:
                    pass
                self.mapping.unlink(Anchor(channel_id, thread_root))
                self._forget_thread_silent_drops(channel_id, thread_root)
                self.posters.forget(session_id)
                self._session_triggerer.pop(session_id, None)
                self._recent_vd_sends.pop(session_id, None)
                if self.typing:
                    await self.typing.stop(session_id)
            else:
                await self._leave_channel(channel_id, session_id, _truncate_for_mm(body))
            return

        # <openFile/> directives → attachments
        file_ids: list[str] = []
        warnings: list[str] = []
        line_hints: list[str] = []
        open_files = [d for d in dirs if d.kind == "open_file"]
        if open_files:
            project_path = await self._project_path_for(session_id)
            max_size = self._get_max_file_size()
            for d in open_files:
                raw_path = d.attrs.get("path", "")
                if not raw_path:
                    continue
                resolved = resolve_attachment_path(
                    raw_path, project_path, self.config.allowed_attachment_roots,
                )
                if not resolved:
                    warnings.append(
                        f"_Could not attach `{raw_path}`: outside allowed roots._"
                    )
                    continue
                if not resolved.exists():
                    warnings.append(f"_Could not attach `{raw_path}`: file not found._")
                    continue
                try:
                    size = resolved.stat().st_size
                except OSError:
                    warnings.append(f"_Could not attach `{raw_path}`: permission denied._")
                    continue
                if size > max_size:
                    warnings.append(
                        f"_Could not attach `{raw_path}`: file too large ({size} bytes)._"
                    )
                    continue
                try:
                    fid = self.mm.upload_file(channel_id, resolved)
                    file_ids.append(fid)
                    if line := d.attrs.get("line"):
                        line_hints.append(f"`{raw_path}` (jump to line {line})")
                except Exception:
                    logger.exception("Upload failed for %s", raw_path)
                    warnings.append(f"_Could not attach `{raw_path}`: upload failed._")

        body = cleaned.strip()
        if line_hints:
            body = (body + "\n\n" + "\n".join(line_hints)).strip()
        if warnings:
            body = (body + "\n\n" + "\n".join(warnings)).strip()

        if not body and not file_ids:
            return

        try:
            self.mm.post(
                channel_id, _truncate_for_mm(body),
                file_ids=file_ids or None, root_id=thread_root,
            )
        except Exception:
            logger.exception("Failed to post to MM channel for session %s",
                             session_id[:8])

    # ----- Coalesced tool-use placeholder -----

    def _upsert_tool_use(
        self,
        session_id: str,
        channel_id: str,
        thread_root: str | None,
        tool: str,
    ) -> None:
        """Create or edit the per-turn tool-use placeholder post for
        `session_id`. Repeats of the last-used tool bump a counter; a
        different tool appends a new line.
        """
        run = self.tool_use_runs.get(session_id)
        if run is None:
            body = f"_Using tool: {tool}_"
            try:
                post = self.mm.post(channel_id, body, root_id=thread_root)
            except Exception:
                logger.exception(
                    "Failed to post tool-use placeholder for %s", session_id[:8],
                )
                return
            post_id = post.get("id")
            if not post_id:
                return
            self.tool_use_runs[session_id] = ToolUseRun(
                post_id=post_id, lines=[[tool, 1]],
            )
            return

        _bump_or_append(run, tool)
        try:
            self.mm.update_post(run.post_id, _format_tool_run(run))
        except Exception:
            logger.exception(
                "Failed to edit tool-use placeholder for %s", session_id[:8],
            )

    def _end_tool_use_run(self, session_id: str) -> None:
        """Drop per-session state so the next turn starts a fresh
        placeholder. The existing placeholder post is left in the channel
        as a compact, permanent record of the tools used this turn —
        intentionally not deleted (avoids tombstones, no system_admin
        permission needed).
        """
        self.tool_use_runs.pop(session_id, None)

    async def _project_path_for(self, session_id: str) -> str | None:
        meta = await self.vd.get_session_meta(session_id)
        return meta.get("projectPath") or None

    async def _save_mm_attachments(self, post: dict, cwd: str) -> list[str]:
        """Download MM file attachments into <cwd>/.mattermost-inbox/.

        Returns human-readable notes to prepend to the forwarded message.
        Each successful download yields `[User attached file: <abs-path>]`;
        failures yield a skipped-with-reason note so the LLM still sees them.
        """
        file_ids = post.get("file_ids") or []
        if not file_ids:
            return []

        try:
            inbox = Path(cwd).expanduser().resolve(strict=False) / MM_INBOX_DIRNAME
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Failed to prepare MM inbox in %s", cwd)
            return [f"[MM attachment skipped: cannot write to inbox in {cwd}]"]

        file_meta = {
            f.get("id"): f
            for f in ((post.get("metadata") or {}).get("files") or [])
            if isinstance(f, dict) and f.get("id")
        }

        notes: list[str] = []
        max_size = self._get_max_file_size()
        for fid in file_ids:
            meta = file_meta.get(fid) or {}
            name = _safe_inbox_filename(meta.get("name", ""), f"file-{fid[:8]}")
            size = int(meta.get("size") or 0)
            if size and size > max_size:
                notes.append(
                    f"[MM attachment skipped: `{name}` too large ({size} bytes)]"
                )
                continue
            try:
                data = await asyncio.to_thread(self.mm.download_file, fid)
            except Exception:
                logger.exception("Download failed for MM file %s", fid)
                notes.append(f"[MM attachment skipped: `{name}` download failed]")
                continue
            try:
                dest = _unique_inbox_path(inbox, name)
                dest.write_bytes(data)
            except OSError:
                logger.exception("Write failed for MM file %s", fid)
                notes.append(f"[MM attachment skipped: `{name}` write failed]")
                continue
            logger.info("Saved MM attachment %s → %s", fid[:8], dest)
            notes.append(f"[User attached file: {dest}]")
        return notes

    def _get_max_file_size(self) -> int:
        if self._max_file_size is None:
            self._max_file_size = self.mm.get_max_file_size()
        return self._max_file_size

    # ----- VibeDeck session_summary_updated → rename channel -----

    async def _on_vd_summary_updated(self, data: dict) -> None:
        session_id = data.get("session_id", "")
        title = data.get("summaryTitle", "") or ""
        if not session_id or not title:
            return
        anchor = self.mapping.get_anchor(session_id)
        # Only rename the MM channel for top-level channel sessions — for
        # thread forks there's no channel rename to perform.
        if not anchor or anchor.is_thread:
            return
        channel_id = anchor.channel_id
        if not self.name_sync.should_sync("vd", session_id):
            return
        try:
            self.mm.rename_channel(channel_id, title[:MM_DISPLAY_NAME_MAX])
            self.name_sync.note_remote_update("mm", channel_id)
            logger.info("VD summary → MM rename for %s", session_id[:8])
        except Exception:
            logger.warning("Failed to rename channel for %s", session_id[:8],
                           exc_info=True)

    # ----- VibeDeck session_status → typing indicator -----

    async def _on_vd_session_status(self, data: dict) -> None:
        session_id = data.get("session_id", "")
        running = bool(data.get("running"))
        if not session_id:
            return
        self.last_status_ts[session_id] = time.monotonic()

        if not running:
            # Safety net: clear any lingering tool-use placeholder when the
            # turn ends without a final assistant text (interrupt, crash).
            self._end_tool_use_run(session_id)
            if self.typing:
                await self.typing.stop(session_id)
            self._mention_triggerer_on_done(session_id)
            return

        if not self.typing:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return
        await self.typing.start(session_id, anchor.channel_id, anchor.root_id)

    def _mention_triggerer_on_done(self, session_id: str) -> None:
        """Post ``@<username>`` in the session's channel/thread so the user
        whose MM message triggered this run gets a push notification. No-op
        when the feature is disabled, no triggerer was tracked, or the
        anchor can't be resolved. The triggerer is consumed on use so a
        single run completion only pings once."""
        if not self.config.mention_user_when_done:
            return
        user_id = self._session_triggerer.pop(session_id, None)
        if not user_id:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return
        username = self._resolve_username(user_id)
        try:
            self.mm.post(
                anchor.channel_id,
                f"@{username}",
                root_id=anchor.root_id,
            )
        except Exception:
            logger.warning(
                "Failed to @-mention %s on session %s completion",
                username, session_id[:8], exc_info=True,
            )
