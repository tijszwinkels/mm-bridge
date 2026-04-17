"""Bridge orchestrator — dispatches Mattermost ↔ VibeDeck events to handlers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import attribution, directives, name_sync, purpose
from .config import ChannelMapping, Config
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


def _extract_text_from_blocks(blocks: list[dict]) -> str:
    """Extract readable text from VibeDeck normalized message blocks."""
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool = block.get("tool_name", "unknown")
            parts.append(f"_Using tool: {tool}_")
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


def _resolve_attachment_path(
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
        self.mapping = ChannelMapping.load(config.state_file)
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
        self._max_file_size: int | None = None

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
        })

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
        message = (post.get("message") or "").strip()
        if not message and not post.get("file_ids"):
            return
        channel_id = post["channel_id"]
        root_id = post.get("root_id") or None

        # Thread reply?
        if root_id:
            await self._handle_thread_post(channel_id, root_id, post, message)
            return

        session_id = self.mapping.get_session(channel_id)
        if not session_id:
            # v1's "create on first message" path is gone (§1.3). If a pending
            # session for this channel is still warming up, queue the message.
            pending = self.pending_mm_sessions.get(channel_id)
            if pending:
                pending.queued_messages.append(message)
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

        await self._forward_user_post(
            channel_id, session_id, post, message, thread_root=None,
        )

    async def _on_mm_user_added(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        if self.mapping.get_session(channel_id):
            logger.info("Bot already mapped for channel %s — skipping", channel_id)
            return
        if channel_id in self.pending_mm_sessions:
            return
        await self._start_invited_session(channel_id)

    async def _on_mm_user_removed(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        session_id = self.mapping.unlink_channel(channel_id)
        self.purpose_by_channel.pop(channel_id, None)
        self.pending_mm_sessions.pop(channel_id, None)
        if session_id:
            self.posters.forget(session_id)
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

        session_id = self.mapping.get_session(channel_id)
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

        # Purpose change (only emit notice if actually changed)
        if prev and prev.get("purpose") != new_purpose:
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

    async def _start_invited_session(self, channel_id: str) -> None:
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            logger.exception("Failed to fetch channel %s on invite", channel_id)
            return
        purpose_text = ch.get("purpose", "") or ""

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

        try:
            resp = await self.vd.create_session(
                message=INVITE_PLACEHOLDER,
                cwd=effective_cwd,
                backend=cfg.backend,
                model_index=model_index,
            )
        except Exception:
            logger.exception("Failed to create VD session for channel %s", channel_id)
            try:
                self.mm.post_message(
                    channel_id, ":warning: Failed to start a VibeDeck session.",
                )
            except Exception:
                pass
            return

        status = resp.get("status")
        if status == "permission_denied":
            self.mm.post_message(
                channel_id,
                ":warning: VibeDeck could not start the session — needs additional permissions.",
            )
            return
        if status != "started":
            self.mm.post_message(
                channel_id,
                f":warning: VibeDeck returned unexpected status `{status}` while starting the session.",
            )
            return

        cwd = resp.get("cwd") or effective_cwd
        self.pending_mm_sessions[channel_id] = PendingMattermostSession(
            channel_id=channel_id,
            cwd=cwd,
            backend=cfg.backend,
            initial_message=INVITE_PLACEHOLDER,
            requested_at=time.monotonic(),
            purpose_cfg=cfg,
        )
        self.purpose_by_channel[channel_id] = cfg

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
        `_resolve_attachment_path`).
        """
        if not cfg.cwd:
            return self.config.default_cwd
        roots = self.config.allowed_attachment_roots
        resolved = _resolve_attachment_path(cfg.cwd, project_path=None, allowed_roots=roots)
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
        return f"{head}\n\n{hint}"

    # ─────────────────────── Forwarding user posts ─────────────────────────

    async def _forward_user_post(
        self,
        channel_id: str,
        session_id: str,
        post: dict,
        message: str,
        thread_root: str | None,
    ) -> None:
        cfg = self.purpose_by_channel.get(channel_id)
        bot_mention = f"@{self.mm.bot_username}"

        if cfg and cfg.mention_only and bot_mention not in message:
            return  # Filtered out: nothing to forward, no warning.

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
        if attribute and cleaned:
            username = self._resolve_username(user_id)
            cleaned = self.posters.format(cleaned, username, True)

        body = cleaned
        if attachment_notes:
            notes_block = "\n".join(attachment_notes)
            body = f"{notes_block}\n\n{body}" if body else notes_block

        logger.info("MM → VD [%s]: %s", session_id[:8], body[:80])
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

    def _resolve_username(self, user_id: str) -> str:
        try:
            u = self.mm.get_user(user_id)
            return u.get("username") or user_id[:8]
        except Exception:
            return user_id[:8]

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

        thread_session = self.mapping.get_thread_session(channel_id, root_id)
        if thread_session:
            if cm := _CATCH_UP_RE.match(message):
                await self._run_catch_up(channel_id, thread_session, root_id, cm)
                return
            await self._forward_user_post(
                channel_id, thread_session, post, message, thread_root=root_id,
            )
            return

        # No mapping yet — try to fork.
        parent_session = self.mapping.get_session(channel_id)
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

        try:
            posts = self.mm.get_posts(channel_id, max(n + 1, 1))
        except Exception:
            logger.exception("get_posts failed for catch-up")
            return

        lines: list[str] = []
        for p in posts:
            if p.get("user_id") == self.mm.bot_user_id:
                continue
            if _is_mm_system_post(p):
                continue
            if _CATCH_UP_RE.match((p.get("message") or "").strip()):
                continue
            username = self._resolve_username(p.get("user_id", ""))
            lines.append(f"{username}: {p.get('message', '')}")
            if len(lines) >= n:
                break

        block = (
            f"[Catch-up context — last {len(lines)} messages from this channel, oldest first]\n"
            + "\n".join(lines) + "\n[End of catch-up]"
        )

        try:
            await self.vd.send_message(session_id, block)
        except Exception:
            logger.exception("Failed to send catch-up block")
            return

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
            removed = self.mapping.unlink_thread(channel_id, thread_root)
            self.dead_threads.add((channel_id, thread_root))
            if removed:
                self.posters.forget(removed)
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
        self.mapping.unlink_channel(channel_id)
        self.purpose_by_channel.pop(channel_id, None)
        self.posters.forget(session_id)
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
        if self.mapping.get_channel(session_id):
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
        incoming_backend = data.get("backend") or None
        first_msg = (data.get("firstMessage") or "").strip()

        if not incoming_cwd:
            return False

        candidates: list[tuple[str, PendingMattermostSession]] = []
        for channel_id, pending in self.pending_mm_sessions.items():
            if _normalize_path(pending.cwd) != incoming_cwd:
                continue
            if pending.backend and incoming_backend and pending.backend != incoming_backend:
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
        self.mapping.link(channel_id, session_id)
        self.pending_mm_sessions.pop(channel_id, None)
        logger.info("Claimed pending invite: channel %s → session %s",
                    channel_id, session_id[:8])
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
        self.mapping.link_thread(ch_id, root_id, session_id)
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
            self.mapping.link(channel_id, session_id)
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
        if msg.get("role") != "assistant":
            return

        thread_loc = self.mapping.get_thread_location(session_id)
        if thread_loc:
            channel_id, thread_root = thread_loc
        else:
            channel_id = self.mapping.get_channel(session_id) or ""
            thread_root = None
        if not channel_id:
            return

        text = _extract_text_from_blocks(msg.get("blocks", []))
        if not text.strip():
            return

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
                self.mapping.unlink_thread(channel_id, thread_root)
                self.posters.forget(session_id)
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
                resolved = _resolve_attachment_path(
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
        channel_id = self.mapping.get_channel(session_id)
        if not channel_id:
            return
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
        if not session_id or not self.typing:
            return
        self.last_status_ts[session_id] = time.monotonic()

        if not running:
            await self.typing.stop(session_id)
            return

        thread_loc = self.mapping.get_thread_location(session_id)
        if thread_loc:
            channel_id, root_id = thread_loc
        else:
            channel_id = self.mapping.get_channel(session_id) or ""
            root_id = None
        if not channel_id:
            return
        await self.typing.start(session_id, channel_id, root_id)
