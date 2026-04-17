"""Core bridge logic: mirrors VibeDeck sessions as Mattermost channels."""

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time

from .config import Config, ChannelMapping
from .mm_client import MattermostClient
from .vd_client import VibeDeckClient

logger = logging.getLogger(__name__)
MM_CHANNEL_RECONCILE_SECONDS = 10
MM_PENDING_SESSION_MERGE_WINDOW_SECONDS = 30


def _extract_text_from_blocks(blocks: list[dict]) -> str:
    """Extract readable text from VibeDeck normalized message blocks."""
    parts = []
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
                parts.append(f"**Tool error:** {content[:500]}")
    return "\n".join(parts)


def _truncate_for_mm(text: str, max_len: int = 16000) -> str:
    """Truncate text to fit Mattermost's post limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 50] + "\n\n_(truncated)_"


def _session_to_channel_name(session_id: str) -> str:
    """Generate a valid MM channel name from a session ID."""
    short = session_id[:12].replace("-", "")
    return f"s-{short}"


def _is_mm_system_post(post: dict) -> bool:
    """Mattermost system posts should not be forwarded to VibeDeck."""
    return bool(post.get("type"))


def _normalize_path(path: str | None) -> str | None:
    """Normalize paths for matching pending MM channels to new VD sessions."""
    if not path:
        return None
    return str(path).rstrip("/")


@dataclass
class PendingMattermostSession:
    channel_id: str
    initial_message: str
    cwd: str
    backend: str | None
    requested_at: float
    queued_messages: list[str] = field(default_factory=list)


class Bridge:
    """Mirrors every VibeDeck session as a Mattermost channel."""

    def __init__(self, config: Config):
        self.config = config
        self.mapping = ChannelMapping.load(config.state_file)
        self.pending_mm_channels: dict[str, PendingMattermostSession] = {}
        self.mm = MattermostClient(
            url=config.mm_url,
            port=config.mm_port,
            scheme=config.mm_scheme,
            token=config.mm_bot_token,
            team_name=config.mm_team,
        )
        self.vd = VibeDeckClient(config.vd_url)

    async def start(self) -> None:
        """Connect to both services and start the bridge."""
        self.mm.login()

        health = await self.vd.health()
        logger.info("VibeDeck health: %s", health)

        # Optionally sync existing sessions → channels
        if self.config.sync_existing:
            await self._sync_existing_sessions()
        else:
            logger.info(
                "Skipping existing session sync (set MM_SYNC_EXISTING=1 to enable)"
            )

        # Start both listeners concurrently
        await asyncio.gather(
            self._run_mm_listener(),
            self._run_vd_listener(),
            self._run_mm_channel_membership_reconciler(),
        )

    async def stop(self) -> None:
        await self.vd.close()

    # ── Initial sync ───────────────────────────────────────────

    async def _sync_existing_sessions(self) -> None:
        """Create MM channels for any VibeDeck sessions that don't have one."""
        sessions = await self.vd.list_sessions()
        created = 0
        for s in sessions:
            session_id = s.get("id", "")
            if not session_id:
                continue
            if self.mapping.get_channel(session_id):
                continue  # already mapped
            self._create_channel_for_session(s)
            created += 1

        if created:
            logger.info("Created %d channels for existing sessions", created)

    def _create_channel_for_session(self, session_data: dict) -> str | None:
        """Create a MM channel for a session. Returns channel_id or None."""
        session_id = session_data.get("id", "")
        if not session_id:
            return None

        # Already mapped?
        existing = self.mapping.get_channel(session_id)
        if existing:
            return existing

        channel_name = _session_to_channel_name(session_id)

        # Build display name from summary or project path
        display_name = (
            session_data.get("summaryTitle")
            or session_data.get("project", "")
            or session_id[:12]
        )
        # MM display name max 64 chars
        display_name = display_name[:64]

        try:
            ch = self.mm.create_channel(
                name=channel_name,
                display_name=display_name,
                purpose=f"VibeDeck session {session_id}",
            )
            channel_id = ch["id"]
            self.mapping.link(channel_id, session_id)
            logger.info(
                "Created channel %s (%s) for session %s",
                display_name,
                channel_name,
                session_id[:12],
            )
            return channel_id
        except Exception:
            logger.exception("Failed to create channel for session %s", session_id[:12])
            return None

    # ── Mattermost → VibeDeck ──────────────────────────────────

    async def _run_mm_listener(self) -> None:
        """Listen for Mattermost messages and forward to VibeDeck."""
        logger.info("Starting Mattermost WebSocket listener...")
        await self.mm.listen_websocket(
            self._on_mm_message,
            self._on_mm_channel_created,
        )

    async def _run_mm_channel_membership_reconciler(self) -> None:
        """Continuously join public team channels the bot is missing."""
        while True:
            try:
                joined = self._reconcile_mm_channel_membership_once()
                if joined:
                    logger.info("Joined %d existing team channels", joined)
            except Exception:
                logger.exception("Failed to reconcile Mattermost channel membership")
            await asyncio.sleep(MM_CHANNEL_RECONCILE_SECONDS)

    def _reconcile_mm_channel_membership_once(self) -> int:
        """Join any public team channels the bot is not yet a member of."""
        return self.mm.join_all_team_channels()

    async def _on_mm_channel_created(self, channel_id: str) -> None:
        """Join newly created channels as soon as Mattermost notifies us."""
        try:
            self.mm.join_channel(channel_id)
            logger.info("Joined newly created channel %s", channel_id)
        except Exception:
            logger.warning("Failed to join newly created channel %s", channel_id)

    async def _on_mm_message(self, post: dict) -> None:
        """Handle a user message from Mattermost — forward to VibeDeck."""
        channel_id = post["channel_id"]
        if _is_mm_system_post(post):
            logger.info(
                "Ignoring Mattermost system post in channel %s (%s)",
                channel_id,
                post.get("type", ""),
            )
            return

        message = post.get("message", "").strip()
        if not message:
            return

        session_id = self.mapping.get_session(channel_id)
        if not session_id:
            await self._handle_new_mm_channel_message(channel_id, message)
            return

        logger.info("MM → VD [%s]: %s", session_id[:8], message[:80])

        try:
            result = await self.vd.send_message(session_id, message)
            logger.info("Sent to session %s: %s", session_id[:8], result.get("status"))
        except Exception:
            logger.exception("Failed to send to VibeDeck session %s", session_id[:8])
            self.mm.post_message(
                channel_id,
                ":warning: Failed to send message to Claude session.",
            )

    # ── VibeDeck → Mattermost ──────────────────────────────────

    async def _run_vd_listener(self) -> None:
        """Listen for VibeDeck SSE events and forward to Mattermost."""
        logger.info("Starting VibeDeck SSE listener...")
        await self.vd.stream_events(self._on_vd_event)

    async def _on_vd_event(self, event_type: str, data: dict) -> None:
        """Handle a VibeDeck SSE event."""
        if event_type == "session_added":
            if not await self._claim_pending_mm_channel_for_session(data):
                self._create_channel_for_session(data)
        elif event_type == "message":
            await self._on_vd_message(data)
        elif event_type == "session_summary_updated":
            self._on_summary_updated(data)

    async def _handle_new_mm_channel_message(self, channel_id: str, message: str) -> None:
        """Create or queue a VibeDeck session for an unmapped Mattermost channel."""
        self._expire_pending_mm_channels()

        pending = self.pending_mm_channels.get(channel_id)
        if pending:
            pending.queued_messages.append(message)
            logger.info(
                "Queued MM message while session starts for channel %s",
                channel_id,
            )
            return

        await self._start_vd_session_for_mm_channel(channel_id, message)

    async def _start_vd_session_for_mm_channel(
        self,
        channel_id: str,
        message: str,
    ) -> None:
        """Start a new VibeDeck session from the first MM message in a channel."""
        try:
            response = await self.vd.create_session(
                message=message,
                cwd=self.config.vd_default_cwd,
                backend=self.config.vd_new_session_backend,
                model_index=self.config.vd_new_session_model_index,
            )
        except Exception:
            logger.exception(
                "Failed to create VibeDeck session for Mattermost channel %s",
                channel_id,
            )
            self.mm.post_message(
                channel_id,
                ":warning: Failed to start a VibeDeck session for this channel.",
            )
            return

        status = response.get("status")
        if status == "permission_denied":
            logger.warning(
                "Permission denied while starting session for channel %s",
                channel_id,
            )
            self.mm.post_message(
                channel_id,
                ":warning: VibeDeck could not start the session because it needs additional permissions.",
            )
            return

        if status != "started":
            logger.warning(
                "Unexpected create_session status for channel %s: %s",
                channel_id,
                status,
            )
            self.mm.post_message(
                channel_id,
                ":warning: VibeDeck returned an unexpected status while starting the session.",
            )
            return

        pending = PendingMattermostSession(
            channel_id=channel_id,
            initial_message=message,
            cwd=response.get("cwd") or self.config.vd_default_cwd,
            backend=self.config.vd_new_session_backend,
            requested_at=time.monotonic(),
        )
        self.pending_mm_channels[channel_id] = pending
        logger.info(
            "Started pending VibeDeck session for MM channel %s (cwd=%s, backend=%s)",
            channel_id,
            pending.cwd,
            pending.backend or "default",
        )

    async def _claim_pending_mm_channel_for_session(self, session_data: dict) -> bool:
        """Attach a newly created VD session to its originating MM channel.

        Matching is resilient to two VibeDeck quirks (see CLAUDE.md in this repo):
        - ``session_added`` may fire before VibeDeck has read the session file,
          so ``firstMessage`` / ``backend`` may be empty.
        - ``firstMessage`` is truncated to 200 chars in VibeDeck, so we use
          prefix matching rather than equality.
        """
        self._expire_pending_mm_channels()

        session_id = session_data.get("id", "")
        if not session_id:
            return False

        incoming_cwd = _normalize_path(session_data.get("projectPath"))
        incoming_backend = session_data.get("backend") or None
        incoming_first_message = (session_data.get("firstMessage") or "").strip()

        # cwd is the strong identifier — the bridge always knows what it sent.
        # Without it, we can't confidently claim any pending channel.
        if not incoming_cwd:
            return False

        candidates: list[tuple[str, PendingMattermostSession]] = []
        for channel_id, pending in self.pending_mm_channels.items():
            if _normalize_path(pending.cwd) != incoming_cwd:
                continue
            if incoming_first_message:
                pending_msg = pending.initial_message.strip()
                # Prefix match either direction: VD may have truncated to 200
                # chars, or the bridge's stored message may itself be shorter.
                if not (
                    pending_msg.startswith(incoming_first_message)
                    or incoming_first_message.startswith(pending_msg)
                ):
                    continue
            if pending.backend and incoming_backend and pending.backend != incoming_backend:
                continue
            candidates.append((channel_id, pending))

        if len(candidates) != 1:
            # 0 = no match, >1 = ambiguous — fall back to creating a fresh channel
            return False

        channel_id, pending = candidates[0]
        self.mapping.link(channel_id, session_id)
        del self.pending_mm_channels[channel_id]
        logger.info(
            "Linked Mattermost channel %s to new VibeDeck session %s",
            channel_id,
            session_id[:12],
        )
        await self._flush_pending_mm_messages(channel_id, session_id, pending)
        return True

    async def _flush_pending_mm_messages(
        self,
        channel_id: str,
        session_id: str,
        pending: PendingMattermostSession,
    ) -> None:
        """Deliver queued MM follow-up messages after the new session appears."""
        for queued_message in pending.queued_messages:
            try:
                result = await self.vd.send_message(session_id, queued_message)
                logger.info(
                    "Flushed queued MM → VD [%s]: %s",
                    session_id[:8],
                    result.get("status"),
                )
            except Exception:
                logger.exception(
                    "Failed to flush queued message to VibeDeck session %s",
                    session_id[:8],
                )
                self.mm.post_message(
                    channel_id,
                    ":warning: Failed to deliver a queued Mattermost message to the new VibeDeck session.",
                )
                break

    def _expire_pending_mm_channels(self) -> None:
        """Drop stale pending MM session creations that never resolved."""
        now = time.monotonic()
        expired = [
            channel_id
            for channel_id, pending in self.pending_mm_channels.items()
            if (now - pending.requested_at) >= MM_PENDING_SESSION_MERGE_WINDOW_SECONDS
        ]
        for channel_id in expired:
            logger.warning(
                "Pending MM session creation expired for channel %s",
                channel_id,
            )
            del self.pending_mm_channels[channel_id]

    async def _on_vd_message(self, data: dict) -> None:
        """Assistant message from VibeDeck — forward to Mattermost."""
        session_id = data.get("session_id", "")
        msg = data.get("message", {})

        # Only forward assistant messages
        if msg.get("role") != "assistant":
            return

        blocks = msg.get("blocks", [])
        text = _extract_text_from_blocks(blocks)
        if not text.strip():
            return

        channel_id = self.mapping.get_channel(session_id)
        if not channel_id:
            return

        text = _truncate_for_mm(text)

        try:
            self.mm.post_message(channel_id, text)
        except Exception:
            logger.exception("Failed to post to MM channel for session %s", session_id[:8])

    def _on_summary_updated(self, data: dict) -> None:
        """Session got a title — rename the channel."""
        session_id = data.get("session_id", "")
        title = data.get("summaryTitle", "")
        if not session_id or not title:
            return

        channel_id = self.mapping.get_channel(session_id)
        if not channel_id:
            return

        try:
            self.mm.rename_channel(channel_id, title[:64])
            logger.info("Renamed channel for %s → %s", session_id[:8], title[:40])
        except Exception:
            logger.warning("Failed to rename channel for %s", session_id[:8])
