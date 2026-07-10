"""Bridge orchestrator — dispatches Mattermost ↔ agent-harness events to handlers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import attribution, commands, directives, name_sync, purpose, resume_header
from .agent_harness_client import (
    AgentHarnessClient,
    HarnessForkUnsupported,
    HarnessInterruptUnsupported,
    HarnessResumeUnsupported,
    HarnessRunNotFound,
)
from .config import Anchor, ChannelMapping, Config
from .mm_client import MattermostClient
from .typing_indicator import TypingIndicator

logger = logging.getLogger(__name__)

# Placeholder used when creating an invite-driven session, since the backend
# needs a first user turn to start useful work.
INVITE_PLACEHOLDER = (
    "Hello! I've just been added to a Mattermost channel. "
    "I'll wait for the user to start the conversation."
)

MM_POST_MAX_LEN = 16000
MM_DISPLAY_NAME_MAX = 64
# Event types that unconditionally mean "the session is actively working" and
# should (re)start the typing indicator. ``session.updated`` is NOT here: it is
# status-driven (see ``_session_updated_is_activity``) because the harness reuses
# it for both running- and idle-flips.
HARNESS_ACTIVITY_EVENTS = {
    "message",
    "message.delta",
    "tool.call",
    "tool.result",
    "run.started",
    "permission.denied",
}
HARNESS_RUN_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.interrupted"}

# Run statuses (agent-harness ``RunStatus``) that mean the run is still in
# flight. The Run row is the authoritative "is the coding agent running?"
# signal: unlike ``session.status`` (freshness-based, flips to "idle" on
# rollout-file silence MID-RUN during long tool calls), run status only
# changes on real lifecycle transitions.
HARNESS_LIVE_RUN_STATUSES = {"queued", "running"}

# Session statuses (agent-harness ``SessionStatus``) that mean the session is
# QUIET, not actively producing output. The harness freshness fix emits a
# ``session.updated`` carrying one of these specifically to signal the session
# went idle (see agent-harness ``observer._maybe_publish_status_flip``); a
# bare event-type check would mis-read that "went quiet" flip as activity and
# keep the typing indicator stuck ON. Only ``status == "running"`` on a
# ``session.updated`` is treated as activity; missing/unknown status falls
# back to the SAFE non-activity choice (genuine output always ALSO emits
# ``message`` / ``message.delta`` / ``tool.*`` events, which keep typing alive
# on their own), and those QUIET flips additionally STOP typing.
HARNESS_QUIET_SESSION_STATUSES = {"idle", "waiting_for_input", "archived"}

# Watchdog events — emitted by the harness `RunProcess` watchdogs (see
# agent-harness PR #10). NOT in HARNESS_RUN_TERMINAL_EVENTS or
# HARNESS_ACTIVITY_EVENTS: they're supplemental signals that PRECEDE a
# normal terminal event (`run.completed`/`run.failed`/`run.interrupted`),
# which still does the typing-stop / run-id-pop cleanup.
HARNESS_WATCHDOG_EVENTS = {"run.terminated_after_end_turn", "run.timed_out_idle"}

# Body posted to the session's anchor channel/thread when the idle-timeout
# watchdog fires (after 30min without `message`/`message.delta`/`tool_use`).
# The harness force-stops the subprocess; the user's previous reply may be
# truncated. Markdown — preserve the leading emoji + italic block.
IDLE_TIMEOUT_WARNING = (
    "⚠️ _Session timed out after 30 minutes of inactivity. "
    "The harness force-stopped it; the previous reply may be incomplete. "
    "Send a new message to resume._"
)

# How often to flush the SSE cursor (`mapping.last_event_seq`) to disk.
# Each event triggers an in-memory update; the disk write is throttled to
# avoid burning IO on busy streams. A crash loses at most this many seconds
# of cursor progress — the bridge replays from the last flushed seq.
EVENT_SEQ_FLUSH_INTERVAL_SECONDS = 2.0

# Hard cap on channel-create retries per session. Some session ids cannot
# produce a valid MM channel name (collisions, invalid slug). Without a
# cap, every subsequent ``session.updated`` event re-tries creation in a
# tight loop. Adding to ``_known_sessions`` after the cap stops retrying.
MAX_CHANNEL_CREATE_ATTEMPTS = 3

# Session-id prefixes that should never get an MM channel. These are
# claude-code subagent transcripts (``agent-<hex>.jsonl``), surfaced by the
# harness observer alongside user-facing sessions. They're internal to the
# parent run and would just spam channels.
SUPPRESSED_SESSION_PREFIXES = ("claude_agent-",)

# Channel-join welcome — posted when the bot is added to a channel (auto-join
# OR manual /invite). Edit the template to change what new users see first.
# The `{backends}` and `{context}` placeholders are filled in by
# ``Bridge._format_channel_join_welcome`` so the message reflects the actually
# configured per-backend default models and (when known) the channel's
# effective config.
CHANNEL_JOIN_WELCOME_PROP = "from_bridge"
CHANNEL_JOIN_WELCOME_PROP_VALUE = "welcome"

CHANNEL_JOIN_WELCOME_TEMPLATE = (
    ":wave: Hi, I'm **@{bot}** — an AI coding assistant. "
    "Tag `@{bot}` to talk to me (or set `autorespond` so every message "
    "reaches me).{context}\n"
    "\n"
    "Pick a backend/model by sending it as your **first message** "
    "(comma-separated, first token is the backend — "
    "e.g. `{example}, autorespond`). Backends: {backends}.\n"
    "\n"
    "Commands (no mention needed): `.help` for the full list, "
    "`.stop` to interrupt. Also `@{bot} catch up {catch_up_n}` · "
    "`@{bot} leave`. "
    "More: [README](https://github.com/tijszwinkels/mm-bridge#readme)."
)

_CATCH_UP_RE = re.compile(r"^@claude\s+catch\s+up(?:\s+(\d+))?\s*$", re.IGNORECASE)
_LEAVE_CMD_RE = re.compile(r"^@claude\s+leave\b(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_STOP_CMD_RE = re.compile(r"^(?P<mention>@claude\s+)?stop\s*$", re.IGNORECASE)
_RUNTIME_TOGGLE_RE = re.compile(
    r"^(autorespond|noautorespond|autoresponse|noautoresponse)$", re.IGNORECASE,
)


@dataclass
class WarmingUpChannel:
    """A channel with a session create/link HTTP round trip in progress."""

    channel_id: str
    queued_messages: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


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
    ToolUseRun coalescing path in `_on_harness_message`.
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


def _is_suppressed_session(session_id: str) -> bool:
    """Return True for ids the bridge should never attach a channel to.

    Currently: claude-code subagent transcripts (``claude_agent-<hex>``),
    which are internal to a parent run.
    """
    return any(session_id.startswith(p) for p in SUPPRESSED_SESSION_PREFIXES)


# `.sessions` list sizing (client-side — the harness endpoint has no limit).
_SESSIONS_DEFAULT_N = 15
_SESSIONS_MAX_N = 50


def _parse_count_arg(arg: str | None, *, default: int, maximum: int) -> int:
    """Parse an optional integer command argument, clamped to [1, maximum].

    Non-integer or absent args fall back to ``default``.
    """
    if not arg:
        return default
    try:
        n = int(arg.strip())
    except ValueError:
        return default
    return max(1, min(n, maximum))


def _session_to_channel_name(session_id: str) -> str:
    """Derive a stable, collision-resistant MM channel slug.

    Backend-prefixed ids (``claude_<uuid>``, ``codex_<uuid>``, ``ses_<uuid>``)
    have most of their entropy *after* the underscore. Naively taking
    ``session_id[:12]`` truncates that entropy, causing same-prefix ids to
    collide on the same slug. Strip the known backend prefix first so the
    12-char window samples the unique part.
    """
    BACKEND_PREFIXES = ("claude_agent-", "claude_", "codex_", "ses_")
    tail = session_id
    for prefix in BACKEND_PREFIXES:
        if session_id.startswith(prefix):
            tail = session_id[len(prefix):]
            break
    short = tail[:12].replace("-", "").lower()
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
    """Mediates Mattermost ↔ agent-harness traffic."""

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
        self.harness = AgentHarnessClient(config.agent_harness_url)
        self.posters = attribution.PosterTracker()
        self.name_sync = name_sync.NameSync(
            window_seconds=config.name_sync_window_seconds
        )
        self.typing: TypingIndicator | None = None  # created after login
        self.purpose_by_channel: dict[str, purpose.PurposeConfig] = {}
        self.warming_up_sessions: dict[str, WarmingUpChannel] = {}
        self.dead_threads: set[tuple[str, str]] = set()
        self.last_channel_state: dict[str, dict] = {}
        self.last_activity_ts: dict[str, float] = {}
        self.current_run_id_by_session: dict[str, str] = {}
        # Sessions with a run currently in flight, from SSE ``run.started``
        # → terminal events. Origin-agnostic (also tracks runs the bridge
        # didn't submit, unlike ``current_run_id_by_session``). Value is the
        # run_id, or None when the event omitted it. Read by the typing
        # watchdog (reconcile instead of stop) and the quiet-flip handler
        # (ignore freshness idle-flips mid-run).
        self.active_run_by_session: dict[str, str | None] = {}
        self._known_sessions: set[str] = set()
        # Sessions the harness reports as ``origin: external`` — i.e. NOT
        # launched by the harness itself, so it has no stdin / IPC channel
        # to inject new user turns into. The bridge forwards messages for
        # such sessions to a fresh harness-origin replacement instead;
        # see ``_replace_external_session``. Populated at bootstrap and
        # pruned as sessions are replaced.
        self._external_sessions: set[str] = set()
        # How many times we tried (and failed) to create an MM channel for a
        # given session_id. After ``MAX_CHANNEL_CREATE_ATTEMPTS`` the session
        # is added to ``_known_sessions`` to stop retrying.
        self._channel_create_attempts: dict[str, int] = {}
        # Monotonic timestamp of the last cursor flush to disk. Throttled by
        # ``EVENT_SEQ_FLUSH_INTERVAL_SECONDS`` so a busy stream doesn't beat
        # up the filesystem. The latest seq is kept in
        # ``self.mapping.last_event_seq``; only the flush is throttled.
        self._last_seq_flush_ts: float = 0.0
        self._pending_seq: int | None = None
        # SSE start cursor, resolved in ``start()`` from the persisted
        # ``mapping.last_event_seq`` or a cold-start probe of the harness.
        self._event_cursor: int | None = None
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
        # Recently-sent backend message bodies, keyed by session_id. Used to
        # de-duplicate the role=user echo the harness broadcasts when the bridge
        # itself shipped the message (MM forwards, catch-up blocks,
        # firstMessage on invite/fork claims). Capped per session.
        self._recent_harness_sends: dict[str, deque[tuple[float, str]]] = {}

    # ----- lifecycle -----

    async def start(self) -> None:
        self.mm.login()
        self.typing = TypingIndicator(self.mm, self.config.typing_refresh_seconds)

        try:
            health = await self.harness.health()
            logger.info("agent-harness health: %s", health)
        except Exception:
            try:
                await self.harness.list_sessions()
                logger.info("agent-harness session list probe succeeded")
            except Exception:
                logger.exception("agent-harness health check failed — continuing anyway")

        await self._bootstrap_known_sessions()
        self._event_cursor = await self._bootstrap_event_cursor()

        logger.info(
            "Connected — Mattermost (team=%s, bot=%s) + agent-harness (%s)",
            self.config.mm_team, self.mm.bot_username, self.config.agent_harness_url,
        )

        try:
            await self._reconcile_resume_purposes()
        except Exception:
            logger.warning(
                "resume-purpose: startup reconcile failed", exc_info=True,
            )

        await asyncio.gather(
            self._run_mm_listener(),
            self._run_harness_listener(),
            self._run_typing_watchdog(),
            self._run_auto_join_reconciler(),
        )

    async def stop(self) -> None:
        # Flush any throttled SSE cursor before tearing down — a clean
        # stop inside the 2s persist window would otherwise discard the
        # latest seq, and the next boot would replay up to ~2s of events.
        if self._pending_seq is not None:
            try:
                self.mapping.set_event_seq(self._pending_seq)
            except Exception:
                logger.exception("Failed to flush pending event seq on shutdown")
            self._pending_seq = None
        if self.typing:
            await self.typing.shutdown()
        await self.harness.close()

    async def _bootstrap_known_sessions(self) -> None:
        try:
            sessions = await self.harness.list_sessions()
        except Exception:
            logger.warning(
                "Bootstrap GET /v1/sessions failed — falling back to mapping",
                exc_info=True,
            )
            sessions = []
        self._known_sessions = set(self.mapping.session_to_anchor.keys())
        for session in sessions:
            session_id = session.get("id")
            if not session_id:
                continue
            is_external = session.get("origin") == "external"
            if is_external and self.mapping.get_anchor(session_id):
                # Inbound MM posts to this mapping can't be delivered:
                # the harness has no stdin for an externally-launched
                # session. ``_on_mm_posted`` will swap in a fresh
                # harness-origin session on the next user post.
                self._external_sessions.add(session_id)
            if self.mapping.get_anchor(session_id):
                self._known_sessions.add(session_id)
                continue
            if _is_suppressed_session(session_id):
                self._known_sessions.add(session_id)
                continue
            if session_id in self.mapping.adopted_session_ids:
                # Previously replaced by ``_replace_external_session``.
                # The harness still lists it (sessions aren't deleted),
                # but a fresh harness session has taken over its channel.
                # Don't auto-spawn a recovery channel.
                self._known_sessions.add(session_id)
                continue
            if not is_external:
                # Harness-origin sessions in the repo at bootstrap are
                # either pre-existing spawn-CLI / bridge-created records
                # whose channel was wired in a previous run, or test
                # leftovers from prior ``agent-harness`` runs. Either way,
                # we must NOT auto-create a channel for them on startup —
                # but we MUST mark them ``known`` so the SSE bootstrap-
                # replay of their ``session.updated`` event doesn't trip
                # the live-create path in ``_on_harness_session_seen``.
                # Without this guard the cursor-reset recovery flow would
                # re-spawn an MM channel for every leftover test session
                # in the harness DB (the 2026-05-12 ghost-channel burst).
                self._known_sessions.add(session_id)
                continue
            if await self._create_channel_for_session(session):
                self._known_sessions.add(session_id)

    async def _bootstrap_event_cursor(self) -> int | None:
        """Return the SSE cursor to resume from on this boot.

        Warm restart: use the persisted ``last_event_seq`` from state.json,
        but verify the harness's current max sequence first. The harness
        event bus is in-memory — if the harness was restarted, its sequence
        rolls back to 0 and the persisted cursor becomes a "future" value
        that would mask every event the new harness emits. Detected reset
        ⇒ reset cursor to 0 and replay whatever's currently in the new
        harness's bus.

        Cold start (no persisted seq): probe the harness for its current
        max sequence so the bridge only sees *new* events. Without this,
        agent-harness replays its full history on connect, which the bridge
        used to mirror back into Mattermost as a 1000+ post flood.
        """
        persisted = self.mapping.last_event_seq
        try:
            harness_max = await self.harness.probe_current_sequence()
        except Exception:
            if persisted is not None:
                logger.warning(
                    "Cursor probe failed — falling back to persisted seq=%d "
                    "(may skip recent events if harness was restarted)",
                    persisted,
                )
                return persisted
            logger.exception(
                "Cold-start cursor probe failed — starting from current end "
                "(SSE will resync once new events arrive)",
            )
            return None

        if persisted is None:
            self.mapping.set_event_seq(harness_max)
            logger.info(
                "Cold-start SSE cursor probed: starting from seq=%d", harness_max,
            )
            return harness_max

        if persisted > harness_max:
            logger.warning(
                "Detected harness sequence reset (persisted=%d > harness_max=%d). "
                "Resetting cursor to 0 to replay current in-memory events; "
                "already-mapped sessions are filtered by anchor lookup but a "
                "few duplicate posts are possible during recovery.",
                persisted,
                harness_max,
            )
            self.mapping.reset_event_seq(0)
            return 0

        logger.info(
            "Resuming SSE stream from persisted seq=%d (harness_max=%d)",
            persisted,
            harness_max,
        )
        return persisted

    async def _persist_event_seq(self, sequence: int) -> None:
        """Throttled write of the SSE cursor. Always updates in-memory."""
        if not isinstance(sequence, int) or sequence <= 0:
            return
        self._pending_seq = sequence
        now = time.monotonic()
        if now - self._last_seq_flush_ts < EVENT_SEQ_FLUSH_INTERVAL_SECONDS:
            return
        self._last_seq_flush_ts = now
        self.mapping.set_event_seq(sequence)
        self._pending_seq = None

    def _track_run_response(self, session_id: str, run: dict | None) -> None:
        if not run:
            return
        run_id = run.get("run_id") or run.get("id")
        if run_id:
            self.current_run_id_by_session[session_id] = run_id

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

    async def _run_harness_listener(self) -> None:
        logger.info("Starting agent-harness SSE listener...")
        await self.harness.stream_events(
            self._on_harness_event,
            after_sequence=getattr(self, "_event_cursor", None),
            on_progress=self._persist_event_seq,
            on_reset=self._on_harness_sequence_reset,
        )

    async def _on_harness_sequence_reset(self) -> None:
        """Called when the SSE client detects the harness restarted mid-session
        (its in-memory sequence rolled back below our cursor). Reset the
        persisted cursor too so a subsequent bridge restart picks the same
        recovery path on bootstrap."""
        logger.warning(
            "Harness restart detected mid-session — resetting persisted "
            "event cursor to 0",
        )
        self.mapping.reset_event_seq(0)

    async def _run_typing_watchdog(self) -> None:
        """Stop or reconcile typing loops whose event stream went silent."""
        while True:
            await asyncio.sleep(self.config.typing_refresh_seconds)
            await self._typing_watchdog_tick()

    async def _typing_watchdog_tick(self) -> None:
        """One watchdog pass over the typing sessions silent for longer than
        ``typing_stop_after_silence_seconds``.

        Silence alone is NOT proof the agent stopped: during a long tool
        call or an async subagent wait the harness emits no message/tool
        events for minutes while the run is very much alive. So a silent
        session WITH a tracked active run is reconciled against the harness
        Run row instead of blindly stopped; sessions without one (external/
        observer — no run lifecycle events exist for them) keep the old
        silence-stop behavior.
        """
        if not self.typing:
            return
        timeout = self.config.typing_stop_after_silence_seconds
        now = time.monotonic()
        for session_id in list(self.typing.running_sessions()):
            last = self.last_activity_ts.get(session_id)
            if last is not None and now - last <= timeout:
                continue
            if session_id not in self.active_run_by_session:
                logger.debug(
                    "No agent-harness activity for %s in %.0fs, stopping typing",
                    session_id[:8], timeout,
                )
                await self.typing.stop(session_id)
                self.last_activity_ts.pop(session_id, None)
                continue
            if await self._active_run_is_alive(session_id):
                # Run confirmed queued/running. Count the probe as activity
                # so the next reconcile only happens after another full
                # silence window (natural rate limit on harness GETs).
                self.last_activity_ts[session_id] = time.monotonic()
                continue
            # Terminal, unknown (404), or unreachable harness →
            # missed-terminal-event recovery. Conservative on errors so a
            # dead harness can't leave typing stuck ON.
            logger.info(
                "Tracked run for %s not alive after %.0fs silence, "
                "stopping typing",
                session_id[:8], timeout,
            )
            await self.typing.stop(session_id)
            self.last_activity_ts.pop(session_id, None)
            self.active_run_by_session.pop(session_id, None)

    async def _active_run_is_alive(self, session_id: str) -> bool:
        """Ask the harness whether the session's tracked run is still in
        flight (status queued/running).

        ``session.status`` is deliberately NOT consulted: it is freshness-
        based and flips to "idle" on rollout-file silence mid-run. When the
        tracked run_id is missing (event omitted it) fall back to the
        session's runs list — ANY live row counts. Every failure mode
        (404, HTTP error, dead harness) returns False.
        """
        run_id = self.active_run_by_session.get(session_id)
        try:
            if run_id:
                run = await self.harness.get_run(session_id, run_id)
                runs = [run] if run else []
            else:
                runs = await self.harness.list_session_runs(session_id)
        except Exception as exc:
            logger.info(
                "Typing reconcile probe failed for %s: %s",
                session_id[:8], exc,
            )
            return False
        return any(
            (run or {}).get("status") in HARNESS_LIVE_RUN_STATUSES
            for run in runs
        )

    # ─────────────────────── Mattermost WS handlers ───────────────────────

    async def _on_mm_posted(self, post: dict) -> None:
        if _is_mm_system_post(post):
            return
        # Bridge CLI subcommands stamp `props.from_bridge_cli` on posts
        # they author themselves. Marker taxonomy and dispatch:
        #
        #   * ``"post"`` — an explicit ``mm-bridge post`` call. Keyed on the
        #     stamped INTENT (``from_bridge_cli_target``), not just the session
        #     id: "self" = the default post-into-my-own-channel path; "explicit"
        #     = ``--channel``/``--thread`` was given. We drop ONLY a "self" post
        #     whose ``from_bridge_cli_session`` also equals the session the
        #     target anchor maps to (belt and braces) — that's a status update
        #     looping back to its author. Everything else forwards: "explicit"
        #     always (that's agentcom — a sender in channel X telling channel Y
        #     "hi"), a "self" post landing on a different session (anomalous),
        #     and posts with no target tag (old CLI, forwards-compat).
        #     Keying on intent — not session id alone — is deliberate: a
        #     poisoned resolver (the RC1 incident) that leaks a parent session
        #     id can then NEVER cause an explicit agentcom post to be silently
        #     dropped, because agentcom always carries ``--channel`` → "explicit".
        #     The one accepted gap: ``--channel <your own channel>`` is
        #     "explicit" and forwards (a deliberate override, harmless). The
        #     daemon's OWN outbound posts are separately dedup'd upstream by
        #     post id (`mm_client.py`'s `is_own_post`).
        #   * ``"cross-post-mirror"`` — the informational mirror that
        #     ``mm-bridge post --channel <other>`` lands in the
        #     SENDER's own channel for transcript visibility. Drop
        #     iff the recorded origin channel matches where the post
        #     landed (own-channel mirror); otherwise (shouldn't happen
        #     in practice) treat as agentcom and forward.
        #   * ``"spawn-announcement"`` / ``"spawn-kickoff"`` — bridge
        #     artifacts that the sender's or new channel's linked
        #     session must not consume as a user turn. Keep the
        #     channel-equality drop for these.
        #   * Any other / future marker with no channel field — drop
        #     conservatively (marker authors all live in this
        #     codebase, so an absent channel field is a forwards-compat
        #     hedge for a marker we don't recognise yet).
        props = post.get("props") or {}
        if isinstance(props, dict) and props.get("from_bridge_cli"):
            marker = props.get("from_bridge_cli")
            origin_channel = props.get("from_bridge_cli_channel")
            if marker == "post":
                # Drop only a default (target=self) post that loops back to its
                # own author session; explicit / mismatched / untagged → forward.
                origin_session = props.get("from_bridge_cli_session")
                if (
                    props.get("from_bridge_cli_target") == "self"
                    and origin_session
                    and self._cli_post_targets_own_session(post, origin_session)
                ):
                    logger.debug(
                        "Dropping self-post loop-back: session %s posted into "
                        "its own channel %s via `mm-bridge post`",
                        origin_session[:8], post["channel_id"],
                    )
                    return
            elif origin_channel is None or origin_channel == post["channel_id"]:
                # Mirror / spawn-artifact landing in the recorded
                # channel — drop so the linked session doesn't read it
                # back as a user turn.
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

        # Dot-commands (`.stop`, `.help`, ...) are handled by the bridge
        # itself. Dispatched here — after the channel→session lookup but
        # before every forward path — so they bypass the mention-only gate
        # and are never delivered to the agent. Global commands work even
        # without a mapped session; session-scoped ones reply "no session".
        parsed = commands.parse(message, mentions=self._command_mentions())
        if parsed is not None and self._commands_allowed_here(channel_id, session_id, message):
            await self._dispatch_command(
                channel_id, session_id, post, parsed, thread_root=None,
            )
            return

        if not session_id:
            # v1's "create on first message" path is gone (§1.3). If a warming
            # session for this channel is still warming up, queue the message.
            warming = self.warming_up_sessions.get(channel_id)
            if warming:
                warming.queued_messages.append(message)
                return
            # Auto-joined channel: session starts on first engagement.
            if self.config.auto_join_public_channels:
                await self._maybe_start_engagement_session(
                    channel_id, post, message,
                )
            return

        # Channels still mapped to a pre-cutover external session can't
        # receive injected user turns (harness has no stdin for them).
        # Replace with a fresh harness-origin session and let the new
        # session pick up the user's current message as its first turn.
        if session_id in self._external_sessions:
            await self._replace_external_session(
                channel_id, session_id, post, message,
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

    def _cli_post_targets_own_session(self, post: dict, origin_session: str) -> bool:
        """True when a ``mm-bridge post`` (``from_bridge_cli="post"``) landed
        in the anchor its OWN originating session maps to.

        The recipient is resolved exactly as the forward path would pick it:
        a threaded post targets ``Anchor(channel, root_id)``, a channel post
        targets ``Anchor(channel)``. When that recipient equals the stamped
        origin session, the post is a self-status-update looping back and must
        not be re-injected as a user turn. Cross-session (agentcom) posts have
        a different recipient and return False (→ forwarded).
        """
        root_id = post.get("root_id") or None
        anchor = (
            Anchor(post["channel_id"], root_id) if root_id
            else Anchor(post["channel_id"])
        )
        return self.mapping.get_session(anchor) == origin_session

    async def _on_mm_user_added(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        # Self-initiated auto-join — silent presence, no session yet, but
        # post a one-time manual so anyone browsing the channel sees how
        # to reach the bot.
        if channel_id in self._self_joined_channels:
            self._self_joined_channels.discard(channel_id)
            logger.info("Auto-joined %s — silent presence (no session until engagement)", channel_id)
            await self._post_channel_join_welcome(channel_id)
            return
        if self.mapping.get_session(Anchor(channel_id)):
            logger.info("Bot already mapped for channel %s — skipping", channel_id)
            return
        if channel_id in self.warming_up_sessions:
            return
        # Manual /invite path: welcome the channel before spinning up the
        # session. The session-start welcome (``_format_welcome``) fires
        # afterwards via ``_start_invited_session``; the two serve different
        # moments — this one is "what this bot is", that one is "what just
        # got configured".
        await self._post_channel_join_welcome(channel_id)
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
            # The per-backend default is applied at session-create time
            # (``_resolve_session_model``) so codex channels don't inherit
            # claude's ``opus`` here.
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
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
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
            # Same reasoning as ``_try_apply_first_message_config``: the
            # harness returns an empty model catalog, and without strict
            # mode any first word ("Hi Claude!") would silently become
            # ``model=hi claude!`` and the message would be swallowed
            # before a session is even started.
            strict_catalog=True,
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
        self.warming_up_sessions.pop(channel_id, None)
        self._forget_channel_silent_drops(channel_id)
        if session_id:
            self._end_tool_use_run(session_id)
            self.posters.forget(session_id)
            self._session_triggerer.pop(session_id, None)
            self._recent_harness_sends.pop(session_id, None)
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
        # Reserve the channel-id slot first so any MM post that lands while
        # we're still fetching channel metadata / model lists / catch-up
        # transcript is queued instead of dropped. The entry is cleared on
        # the success path (after the run starts) and on every error
        # branch below.
        if channel_id not in self.warming_up_sessions:
            self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)

        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            self.warming_up_sessions.pop(channel_id, None)
            logger.exception("Failed to fetch channel %s on invite", channel_id)
            return
        purpose_text = ch.get("purpose", "") or ""
        display_name = (ch.get("display_name") or "").strip() or None

        # purpose.parse takes a sync callable; preload model lists so the
        # callable can just dict-lookup.
        models_by_backend: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models_by_backend[b] = await self.harness.list_backend_models(b)
            except Exception:
                models_by_backend[b] = []

        cfg = purpose.parse(
            purpose_text,
            self.config.default_backend,
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )

        effective_cwd = self._resolve_purpose_cwd(cfg)

        for w in cfg.warnings:
            try:
                self.mm.post_message(channel_id, f":warning: {w}")
            except Exception:
                logger.debug("posting purpose warning failed", exc_info=True)

        # Prepend the last N messages as catch-up context so the session
        # doesn't start cold. Only applies to brand-new sessions (not restarts).
        effective_initial = self._prepend_catch_up(
            channel_id, initial_message, exclude_post_id=exclude_post_id,
        )

        # ``warming_up_sessions[channel_id]`` was set at function entry so
        # any race-arriving MM post is queued; reuse that entry here.
        self.purpose_by_channel[channel_id] = cfg
        session_id: str | None = None
        queued: WarmingUpChannel | None = None

        try:
            session = await self.harness.create_session(
                backend=cfg.backend,
                model=self._resolve_session_model(cfg),
                cwd=effective_cwd,
                title=display_name,
            )
            session_id = session.get("id")
            if not session_id:
                raise RuntimeError("agent-harness create_session response missing id")
            self.mapping.link(Anchor(channel_id), session_id)
            self._known_sessions.add(session_id)
            if allow_first_message_config:
                self.awaiting_first_message.add(channel_id)
            run = await self.harness.create_run(session_id, effective_initial)
            self._track_run_response(session_id, run)
            self._record_harness_send(session_id, effective_initial)
            await self._update_resume_purpose(
                channel_id, session_id, cfg.backend, effective_cwd,
            )
        except Exception:
            logger.exception("Failed to create agent-harness session for channel %s", channel_id)
            self.purpose_by_channel.pop(channel_id, None)
            try:
                self.mm.post_message(
                    channel_id, ":warning: Failed to start an agent-harness session.",
                )
            except Exception:
                pass
            return
        finally:
            queued = self.warming_up_sessions.pop(channel_id, None)

        if post_welcome:
            welcome = self._format_welcome(cfg, effective_cwd)
            try:
                self.mm.post_message(channel_id, welcome)
            except Exception:
                logger.warning("Failed to post welcome message", exc_info=True)

        if session_id and queued and queued.queued_messages:
            await self._flush_queued(channel_id, session_id, queued.queued_messages)

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
                models_by_backend[b] = await self.harness.list_backend_models(b)
            except Exception:
                models_by_backend[b] = []

        parsed = purpose.parse(
            candidate,
            self.config.default_backend,
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
            # The harness's claude-code backend currently returns an empty
            # model catalog. Without strict mode any first word in a chat
            # message ("Hi!") would be silently accepted as a model name
            # and the message swallowed instead of forwarded as a turn.
            strict_catalog=True,
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
        new parse wins; omitted fields fall back to current.

        Exception: when the backend changes, the carried model is dropped.
        Models are backend-specific (``sonnet`` is claude-only, ``gpt-5.5``
        is codex-only) — letting one leak across a backend swap crashes
        session-create on the new backend. With ``model=None`` the
        per-backend default in ``Config.default_models`` kicks in at
        ``_resolve_session_model`` time.
        """
        if current is None:
            return new
        backend_changed = new.backend != current.backend
        if new.model is not None:
            model = new.model
        elif backend_changed:
            model = None
        else:
            model = current.model
        return purpose.PurposeConfig(
            backend=new.backend,
            model=model,
            mention_only=new.mention_only,
            cwd=new.cwd if new.cwd is not None else current.cwd,
            warnings=[],
        )

    async def _replace_external_session(
        self,
        channel_id: str,
        old_session_id: str,
        post: dict,
        message: str,
    ) -> None:
        """Adopt a channel currently mapped to an ``origin: external``
        harness session.

        Externally-launched sessions (e.g. ones created by VibeDeck before
        the harness cutover) appear in ``GET /v1/sessions`` but the harness
        has no stdin / IPC channel into them — ``POST /v1/sessions/<id>/runs``
        returns 200 but the message goes nowhere. The user's current MM
        post would silently vanish.

        Recovery: drop the stale mapping, post a brief adoption notice in
        the channel, and start a fresh harness-origin session whose first
        turn is the user's actual message. Channel scrollback stays
        visible as context for the human; the new session gets the bridge's
        existing catch-up preamble via ``_start_invited_session``.
        """
        logger.info(
            "Adopting channel %s — replacing external session %s with a fresh harness session",
            channel_id, old_session_id[:12],
        )
        # Unlink the old mapping up front so concurrent MM posts hit the
        # ``warming_up_sessions`` queue rather than re-entering this path
        # against the same dead session. Per-session bookkeeping for the
        # old id is safe to drop now: even if replacement fails, the old
        # session is by definition unreachable from MM.
        self.mapping.unlink(Anchor(channel_id))
        self._end_tool_use_run(old_session_id)
        self.posters.forget(old_session_id)
        self._forget_channel_silent_drops(channel_id)
        self._session_triggerer.pop(old_session_id, None)
        self._recent_harness_sends.pop(old_session_id, None)
        self._external_sessions.discard(old_session_id)
        if self.typing:
            await self.typing.stop(old_session_id)
        try:
            self.mm.post_message(
                channel_id,
                ":arrows_counterclockwise: Previous session is no longer reachable from "
                "Mattermost. Starting a fresh session for this channel.",
            )
        except Exception:
            logger.debug("Failed to post adoption notice", exc_info=True)
        await self._start_invited_session(
            channel_id,
            initial_message=message,
            allow_first_message_config=False,
            post_welcome=False,
            exclude_post_id=post.get("id"),
        )

        # Commit the "old session is gone" state ONLY if the replacement
        # actually produced a fresh mapping. ``_start_invited_session``
        # logs+returns on harness/MM failure without raising and without
        # linking; if we'd ``mark_adopted``'d the old id eagerly, the
        # next bootstrap would skip auto-recovery for the still-mapped
        # channel and the operator would have to surgically edit
        # ``state.json`` to recover.
        new_session_id = self.mapping.get_session(Anchor(channel_id))
        if new_session_id and new_session_id != old_session_id:
            self.mapping.mark_adopted(old_session_id)
            # Stop the bootstrap recovery path from re-spawning a fresh
            # channel for this session id on the next restart.
            self._known_sessions.add(old_session_id)
        else:
            # Replacement failed — restore the old mapping so the next MM
            # post re-enters this path and retries. ``_start_invited_session``
            # has already posted a user-visible warning on its own error path.
            self.mapping.link(Anchor(channel_id), old_session_id)
            self._external_sessions.add(old_session_id)
            logger.warning(
                "Replacement of external session %s failed — restored old "
                "mapping for retry on next MM post",
                old_session_id[:12],
            )

    async def _restart_session_with_config(
        self,
        channel_id: str,
        old_session_id: str,
        cfg: purpose.PurposeConfig,
    ) -> str | None:
        """Tear down the current session for `channel_id` and start a new one.

        We keep the MM channel and mapping slot; only the harness session is
        replaced. The old session is abandoned (no explicit backend delete).

        Returns the new session id on success, or ``None`` if the restart
        failed — in which case the channel's prior (still-live) session
        mapping and config are restored so it isn't left session-less, and a
        ``:warning:`` is posted. Callers must not claim success on ``None``.
        """
        previous_cfg = self.purpose_by_channel.get(channel_id)
        self.mapping.unlink(Anchor(channel_id))
        self._end_tool_use_run(old_session_id)
        self.posters.forget(old_session_id)
        self._forget_channel_silent_drops(channel_id)
        self._session_triggerer.pop(old_session_id, None)
        self._recent_harness_sends.pop(old_session_id, None)
        if self.typing:
            await self.typing.stop(old_session_id)
        effective_cwd = self._resolve_purpose_cwd(cfg)

        self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)
        self.purpose_by_channel[channel_id] = cfg
        session_id: str | None = None

        try:
            session = await self.harness.create_session(
                backend=cfg.backend,
                model=self._resolve_session_model(cfg),
                cwd=effective_cwd,
            )
            session_id = session.get("id")
            if not session_id:
                raise RuntimeError("agent-harness create_session response missing id")
            self.mapping.link(Anchor(channel_id), session_id)
            self._known_sessions.add(session_id)
            run = await self.harness.create_run(session_id, INVITE_PLACEHOLDER)
            self._track_run_response(session_id, run)
            self._record_harness_send(session_id, INVITE_PLACEHOLDER)
            await self._update_resume_purpose(
                channel_id, session_id, cfg.backend, effective_cwd,
            )
        except Exception:
            logger.exception("Failed to restart agent-harness session for %s", channel_id)
            # Restore the prior mapping/config so the channel keeps talking to
            # its old (still-live) session instead of being orphaned — a lost
            # session would silently drop every subsequent message.
            self.mapping.link(Anchor(channel_id), old_session_id)
            if previous_cfg is not None:
                self.purpose_by_channel[channel_id] = previous_cfg
            else:
                self.purpose_by_channel.pop(channel_id, None)
            try:
                self.mm.post_message(
                    channel_id,
                    ":warning: Failed to restart the agent-harness session.",
                )
            except Exception:
                pass
            return None
        finally:
            queued = self.warming_up_sessions.pop(channel_id, None)
        if session_id and queued and queued.queued_messages:
            await self._flush_queued(channel_id, session_id, queued.queued_messages)
        return session_id

    def _resolve_session_model(self, cfg: purpose.PurposeConfig) -> str | None:
        """Pick the model to send to ``harness.create_session``.

        Operators who set a model token in Channel Purpose win. Otherwise we
        fall back to the per-backend default (``opus`` for claude,
        ``gpt-5.5`` for codex; ``None`` for backends without an explicit
        default — letting the harness pick). This is the only place that
        knows the backend → model mapping; the parser layer always sees
        ``default_model=None`` so a claude default never leaks into a codex
        session.
        """
        if cfg.model:
            return cfg.model
        return self.config.default_model_for(cfg.backend)

    async def _models_for_known_backends(self) -> dict[str, list[str]]:
        models: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models[b] = await self.harness.list_backend_models(b)
            except Exception:
                models[b] = []
        return models

    async def _run_runtime_toggle(self, channel_id: str, message: str) -> None:
        """Handle a literal `autorespond` / `noautorespond` message.

        Flips the channel's mention_only flag and persists to Channel Purpose.
        Does not forward the token.
        """
        turn_on_autorespond = message.strip().lower() in purpose.AUTORESPOND_ALIASES
        current = self.purpose_by_channel.get(channel_id) or purpose.PurposeConfig(
            backend=self.config.default_backend,
            # Don't bake a model token into the persisted Purpose — the
            # per-backend default applies at session-create time.
            model=None,
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

        Preserves any trailing resume block below the section separator —
        that block is owned by ``_update_resume_purpose`` and lives
        independently of the config tokens. Without the preserve step,
        any operator-triggered config change (autorespond toggle, model
        swap) would silently strip the resume block until the next daemon
        restart.

        Marks the resulting `channel_updated` event as self-triggered so
        the bridge doesn't post a "purpose changed" notice to itself.
        """
        config_section = purpose.to_purpose_string(
            cfg, default_autorespond=self.config.default_autorespond,
        )
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            resume_section = ""
        else:
            _, resume_section = purpose.split_config_section(
                ch.get("purpose") or "",
            )
        serialized = purpose.join_sections(config_section, resume_section)
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

    def _format_channel_join_welcome(
        self, cfg: purpose.PurposeConfig | None,
    ) -> str:
        """Render the channel-join welcome.

        Backend list comes from ``Config.default_models`` keys — only
        backends the operator has actually configured a default model for
        appear in the welcome (so unimplemented entries in
        ``KNOWN_BACKENDS`` don't get advertised to users). When ``cfg``
        is given (Purpose was parsed), append a one-line "this channel"
        summary so users see what they landed on.
        """
        configured = [
            (b, m) for b, m in sorted(self.config.default_models.items()) if m
        ]
        backends = (
            ", ".join(f"`{b}` (default `{m}`)" for b, m in configured)
            if configured else "none configured"
        )
        # Pick the operator's primary backend for the inline example so
        # we never advertise an unconfigured one (e.g. don't say `codex`
        # if only `claude` has a default model). The template appends
        # `, autorespond` after this, so keep ``example`` to the backend
        # name only.
        primary = self.config.default_backend
        if primary not in self.config.default_models and configured:
            primary = configured[0][0]
        example = primary

        context = ""
        if cfg is not None:
            model = (
                cfg.model
                or self.config.default_models.get(cfg.backend)
                or "default"
            )
            flag = "mention-only" if cfg.mention_only else "autorespond"
            context = (
                f" _This channel: `{cfg.backend}` / `{model}` / {flag}._"
            )

        return CHANNEL_JOIN_WELCOME_TEMPLATE.format(
            bot=self.mm.bot_username,
            backends=backends,
            example=example,
            catch_up_n=self.config.catch_up_default_n,
            context=context,
        )

    async def _post_channel_join_welcome(self, channel_id: str) -> None:
        """Post the channel-join welcome. Best-effort, never raises.

        Idempotent at the call site: a re-add posts the welcome again on
        purpose — a re-invite implies fresh contact. The post is tagged
        with ``props.{CHANNEL_JOIN_WELCOME_PROP}=welcome`` so operators
        can filter / dedupe historically.
        """
        cfg: purpose.PurposeConfig | None = None
        try:
            ch = await asyncio.to_thread(self.mm.get_channel, channel_id)
            purpose_text = (ch.get("purpose") or "")
            models_by_backend = await self._models_for_known_backends()
            cfg = purpose.parse(
                purpose_text,
                self.config.default_backend,
                default_model=None,
                available_models_for=lambda b: models_by_backend.get(b, []),
                default_autorespond=self.config.default_autorespond,
            )
        except Exception:
            logger.debug(
                "failed to derive cfg for join welcome on %s",
                channel_id, exc_info=True,
            )

        body = self._format_channel_join_welcome(cfg)
        try:
            self.mm.post(
                channel_id, body,
                props={CHANNEL_JOIN_WELCOME_PROP: CHANNEL_JOIN_WELCOME_PROP_VALUE},
            )
        except Exception:
            logger.warning(
                "failed to post channel-join welcome on %s",
                channel_id, exc_info=True,
            )

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

        logger.info("MM → agent-harness [%s]: %s", session_id[:8], body[:80])
        self._record_harness_send(session_id, body)
        try:
            run = await self.harness.create_run(session_id, body)
            self._track_run_response(session_id, run)
        except HarnessResumeUnsupported:
            logger.warning("agent-harness resume unsupported for %s", session_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Can't resume this external session from Mattermost.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            self._enqueue_silent_drop(channel_id, thread_root, post)
        except Exception:
            logger.exception("Failed to create run for harness session %s", session_id[:8])
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

        thread_session = self.mapping.get_session(Anchor(channel_id, root_id))

        # Dot-commands inside a thread — same interception as the channel
        # path. Dispatched before the leave/fork logic so they never trigger
        # a fork or get forwarded; session-scoped ones resolve the thread
        # session (may be None → "no session" reply).
        parsed = commands.parse(message, mentions=self._command_mentions())
        if parsed is not None:
            await self._dispatch_command(
                channel_id, thread_session, post, parsed, thread_root=root_id,
            )
            return

        # Leave command inside a thread only removes the thread mapping.
        if m := _LEAVE_CMD_RE.match(message):
            await self._run_leave_command(
                channel_id, session_id=None, thread_root=root_id,
                reason=(m.group(1) or "").strip(),
            )
            return

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

        parent_meta = await self.harness.get_session(parent_session) or {}
        cwd = (parent_meta.get("project") or {}).get("path") or self.config.default_cwd

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
            resp = await self.harness.fork_session(parent_session, message=fork_message)
        except HarnessForkUnsupported as exc:
            self._mark_dead_thread(
                channel_id, root_id,
                f"Couldn't fork ({exc or 'unsupported'}).",
            )
            return
        except Exception:
            logger.exception("fork_session error for session %s", parent_session[:8])
            self._mark_dead_thread(channel_id, root_id,
                                   "Couldn't fork this conversation.")
            return

        session = resp.get("session") or {}
        session_id = session.get("id")
        if not session_id:
            logger.warning("fork_session response missing session id: %r", resp)
            self._mark_dead_thread(channel_id, root_id, "Couldn't fork this conversation.")
            return

        self.mapping.link(Anchor(channel_id, root_id), session_id)
        self._known_sessions.add(session_id)
        self._record_harness_send(session_id, fork_message)
        self._track_run_response(session_id, resp.get("run"))
        logger.info("Thread fork linked for %s:%s from parent %s → %s",
                    channel_id, root_id[:8], parent_session[:8], session_id[:8])
        try:
            self.mm.post(
                channel_id,
                ":information_source: _Forked conversation. The full history of the "
                "parent session up to its current state is included — not only up to "
                "the message you replied on._",
                root_id=root_id,
            )
        except Exception:
            logger.debug("Failed to post fork disclaimer", exc_info=True)

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
        message has been successfully delivered to agent-harness, so a transient
        send failure doesn't discard queued conversation.
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

        self._record_harness_send(session_id, block)
        try:
            run = await self.harness.create_run(session_id, block)
            self._track_run_response(session_id, run)
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
                self._recent_harness_sends.pop(removed, None)
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

    # ─────────────────────── Dot-command dispatch ─────────────────────────

    def _command_mentions(self) -> tuple[str, ...]:
        """Bot handles whose leading ``@mention`` ``commands.parse`` strips."""
        return ("claude", self.mm.bot_username)

    def _message_mentions_bot(self, message: str) -> bool:
        """True if ``message`` @-mentions this bot (matches engagement logic)."""
        bot_mention = f"@{self.mm.bot_username}"
        return bot_mention in message or "@claude" in message.lower()

    def _commands_allowed_here(
        self, channel_id: str, session_id: str | None, message: str,
    ) -> bool:
        """Whether a dot-command should be honored in this channel.

        Always in channels with a mapped session, and always when auto-join is
        disabled (the bot is only ever in channels it was explicitly invited
        to). In auto-join "silent presence" channels that have no session yet,
        require an explicit @mention — otherwise a bare dot-shaped message
        (e.g. ``.gitignore ...``) could pull the lurking bot out of silence or
        leak internal listings (`.sessions`/`.running`).
        """
        if session_id or not self.config.auto_join_public_channels:
            return True
        return self._message_mentions_bot(message)

    def _post_cmd_reply(
        self, channel_id: str, text: str, thread_root: str | None,
    ) -> None:
        """Post a dot-command reply, swallowing MM errors (never fatal)."""
        try:
            self.mm.post(channel_id, text, root_id=thread_root)
        except Exception:
            logger.debug("Failed posting dot-command reply", exc_info=True)

    async def _dispatch_command(
        self,
        channel_id: str,
        session_id: str | None,
        post: dict,
        parsed: commands.ParsedCommand,
        *,
        thread_root: str | None,
    ) -> None:
        """Execute an intercepted dot-command. Never forwards to the agent.

        Unknown dot-words and session-scoped commands in a session-less
        channel get a helpful reply instead of silence.
        """
        spec = parsed.spec
        if spec is None:
            self._post_cmd_reply(
                channel_id,
                f":grey_question: Unknown command `.{parsed.name}` — try `.help`.",
                thread_root,
            )
            return
        if spec.session_scoped and not session_id:
            self._post_cmd_reply(
                channel_id,
                ":information_source: No session in this channel.",
                thread_root,
            )
            return

        if spec.name == "help":
            await self._cmd_help(channel_id, thread_root)
        elif spec.name == "stop":
            await self._run_stop_command(
                channel_id, session_id, thread_root=thread_root,
            )
        elif spec.name == "autorespond":
            await self._cmd_autorespond(channel_id, parsed.arg, thread_root)
        elif spec.name == "status":
            await self._cmd_status(channel_id, session_id, thread_root)
        elif spec.name == "model":
            await self._cmd_model(channel_id, session_id, parsed.arg, thread_root)
        elif spec.name == "models":
            await self._cmd_models(channel_id, session_id, thread_root)
        elif spec.name == "running":
            await self._cmd_running(channel_id, thread_root)
        elif spec.name == "sessions":
            await self._cmd_sessions(channel_id, parsed.arg, thread_root)
        elif spec.name == "invite":
            await self._cmd_invite(channel_id, post, parsed.arg, thread_root)
        else:  # registered but not wired — defensive, shouldn't happen
            self._post_cmd_reply(
                channel_id,
                f":warning: `.{spec.name}` isn't available yet.",
                thread_root,
            )

    async def _cmd_help(self, channel_id: str, thread_root: str | None) -> None:
        self._post_cmd_reply(channel_id, commands.help_text(), thread_root)

    async def _cmd_autorespond(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """`.autorespond [on|off]` — bare toggles relative to current state.

        Reuses ``_run_runtime_toggle`` by translating the intent into the
        literal token it already understands (autorespond / noautorespond).
        """
        arg_lc = (arg or "").strip().lower()
        if arg_lc == "on":
            turn_on = True
        elif arg_lc == "off":
            turn_on = False
        elif arg_lc == "":
            cur = self.purpose_by_channel.get(channel_id)
            mention_only = (
                cur.mention_only if cur
                else not self.config.default_autorespond
            )
            turn_on = mention_only  # currently mention-only → turn autorespond on
        else:
            self._post_cmd_reply(
                channel_id,
                ":grey_question: Usage: `.autorespond [on|off]`",
                thread_root,
            )
            return
        token = (
            purpose.AUTORESPOND_TOKEN if turn_on else purpose.NOAUTORESPOND_TOKEN
        )
        await self._run_runtime_toggle(channel_id, token)

    async def _cmd_status(
        self, channel_id: str, session_id: str, thread_root: str | None,
    ) -> None:
        """`.status` — session, model, autorespond flag and run state."""
        cfg = self.purpose_by_channel.get(channel_id)
        try:
            meta = await self.harness.get_session(session_id)
        except Exception:
            logger.debug("`.status` harness get_session failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        meta = meta or {}
        backend = meta.get("backend") or (cfg.backend if cfg else "?")
        model = meta.get("model") or (cfg.model if cfg else None) or "default"
        cwd = (meta.get("project") or {}).get("path") or "?"
        autorespond = "mention-only" if (cfg and cfg.mention_only) else "on"

        run_id = self.current_run_id_by_session.get(session_id)
        if run_id:
            run_state = f"running (`{run_id}`)"
        elif session_id in self.active_run_by_session:
            run_state = "running"
        else:
            run_state = "idle"

        lines = [
            f"**Status** — session `{session_id[:12]}`",
            f"• backend: `{backend}`  ·  model: `{model}`",
            f"• cwd: `{cwd}`",
            f"• autorespond: `{autorespond}`",
            f"• run: {run_state}",
        ]
        hstatus = meta.get("status")
        if hstatus:
            lines.append(f"• harness session: `{hstatus}`")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    def _session_has_active_run(self, session_id: str) -> bool:
        """True if a run is in flight for ``session_id`` (bridge-tracked)."""
        return (
            session_id in self.active_run_by_session
            or bool(self.current_run_id_by_session.get(session_id))
        )

    async def _cmd_model(
        self,
        channel_id: str,
        session_id: str,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.model [<name>]` — show the current model, or switch it.

        Names are free text (no catalog validation): a bad one fails loudly
        with the backend error when the restarted session's first run runs.
        Switching recreates the session (the harness has no model-mutate
        endpoint), so it's refused while a run is active.
        """
        cfg = self.purpose_by_channel.get(channel_id)
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            logger.debug("`.model` harness get_session failed", exc_info=True)
            meta = {}
        backend = purpose.canonical_backend(
            meta.get("backend") or (cfg.backend if cfg else None)
        ) or self.config.default_backend

        # Bare `.model` → report the current model + hint `.models`.
        if not arg:
            model = (
                meta.get("model")
                or (cfg.model if cfg else None)
                or self.config.default_model_for(backend)
                or "default"
            )
            self._post_cmd_reply(
                channel_id,
                f":robot_face: Current model: `{model}` (backend `{backend}`). "
                f"Switch with `.model <name>`; see options with `.models`.",
                thread_root,
            )
            return

        # Switching mid-run would orphan the active run — refuse.
        if self._session_has_active_run(session_id):
            self._post_cmd_reply(
                channel_id,
                ":warning: A run is active — `.stop` it first, then `.model <name>`.",
                thread_root,
            )
            return

        base = cfg or purpose.PurposeConfig(
            backend=backend,
            model=None,
            mention_only=not self.config.default_autorespond,
        )
        new_cfg = purpose.PurposeConfig(
            backend=base.backend,
            model=arg,
            mention_only=base.mention_only,
            cwd=base.cwd,
            warnings=[],
        )
        new_session = await self._restart_session_with_config(
            channel_id, session_id, new_cfg,
        )
        if not new_session:
            # The restart failed; `_restart_session_with_config` already posted
            # a warning and restored the prior session. Don't claim success or
            # persist the unreachable model.
            return
        self._persist_purpose(channel_id, new_cfg)
        self._post_cmd_reply(
            channel_id,
            f":gear: Model set to `{arg}` — session restarted. "
            "If the backend rejects it, the error will appear above.",
            thread_root,
        )

    async def _cmd_models(
        self, channel_id: str, session_id: str | None, thread_root: str | None,
    ) -> None:
        """`.models` — list this channel's backend's models, marking the
        current one. Merges the operator `[models]` catalog with the harness
        catalog (empty today). Works without a session (uses default backend).
        """
        cfg = self.purpose_by_channel.get(channel_id)
        backend = cfg.backend if cfg else None
        current_model = cfg.model if cfg else None
        if session_id:
            try:
                meta = await self.harness.get_session(session_id) or {}
            except Exception:
                logger.debug("`.models` harness get_session failed", exc_info=True)
                meta = {}
            backend = meta.get("backend") or backend
            current_model = meta.get("model") or current_model
        backend = purpose.canonical_backend(backend) or self.config.default_backend

        configured = self.config.configured_models_for(backend)
        try:
            catalog = await self.harness.list_backend_models(backend)
        except Exception:
            logger.debug("`.models` list_backend_models failed", exc_info=True)
            catalog = []
        # Dedup preserving order: configured first, then harness extras.
        models = list(dict.fromkeys([*configured, *(catalog or [])]))

        if not models:
            self._post_cmd_reply(
                channel_id,
                f":information_source: No models listed for `{backend}`. "
                "You can still switch to any name with `.model <name>` — "
                "a bad one fails loudly.",
                thread_root,
            )
            return

        lines = [f"**Models for `{backend}`:**"]
        for m in models:
            mark = (
                "  ← current"
                if current_model and m.lower() == current_model.lower()
                else ""
            )
            lines.append(f"• `{m}`{mark}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    async def _cmd_running(
        self, channel_id: str, thread_root: str | None,
    ) -> None:
        """`.running` — sessions with a run in flight right now.

        Uses the bridge's in-memory ``active_run_by_session`` (populated from
        SSE ``run.started`` → terminal, origin-agnostic — so it covers runs
        the bridge didn't submit too).
        """
        active = [
            sid for sid in self.active_run_by_session
            if not _is_suppressed_session(sid)
        ]
        if not active:
            self._post_cmd_reply(
                channel_id, ":zzz: No runs are active right now.", thread_root,
            )
            return
        lines = ["**Running now:**"]
        for sid in active:
            try:
                meta = await self.harness.get_session(sid) or {}
            except Exception:
                meta = {}
            label = self._session_label(meta, sid)
            backend = meta.get("backend") or "?"
            lines.append(f"• {label} · `{backend}`{self._channel_suffix(sid)}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    def _session_label(self, meta: dict, session_id: str) -> str:
        """A human label for a session row: title/project name + short id."""
        title = meta.get("title") or (meta.get("project") or {}).get("name")
        if title:
            return f"{title} (`{session_id[:8]}`)"
        return f"`{session_id[:12]}`"

    def _channel_name_for_session(self, session_id: str) -> str | None:
        """Display name of the MM channel mapped to ``session_id``, or None."""
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return None
        try:
            ch = self.mm.get_channel(anchor.channel_id)
        except Exception:
            ch = None
        ch = ch or {}
        return ch.get("display_name") or ch.get("name") or anchor.channel_id

    def _channel_suffix(self, session_id: str) -> str:
        """`" → ~channel~"` when mapped, else a not-on-Mattermost note."""
        name = self._channel_name_for_session(session_id)
        return f" → ~{name}~" if name else " · not on Mattermost"

    async def _cmd_sessions(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """`.sessions [N]` — the N most recent sessions across the harness,
        including terminal TUI sessions (``origin:"external"`` rows the
        transcript observer discovers). Sorted client-side by ``updated_at``.
        """
        n = _parse_count_arg(
            arg, default=_SESSIONS_DEFAULT_N, maximum=_SESSIONS_MAX_N,
        )
        try:
            sessions = await self.harness.list_sessions()
        except Exception:
            logger.debug("`.sessions` list_sessions failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        rows = [
            s for s in (sessions or [])
            if not _is_suppressed_session(s.get("id") or "")
        ]
        # The endpoint returns everything in insertion order with no limit —
        # sort newest-first (ISO8601 strings sort lexically) and truncate.
        rows.sort(
            key=lambda s: s.get("updated_at") or s.get("created_at") or "",
            reverse=True,
        )
        rows = rows[:n]
        if not rows:
            self._post_cmd_reply(
                channel_id, ":information_source: No sessions found.", thread_root,
            )
            return
        lines = [f"**Recent sessions** (top {len(rows)}):"]
        for s in rows:
            sid = s.get("id") or "?"
            label = self._session_label(s, sid)
            backend = s.get("backend") or "?"
            name = self._channel_name_for_session(sid)
            hint = (
                f" → ~{name}~" if name
                else f" · not on Mattermost — `.invite {sid}`"
            )
            lines.append(f"• {label} · `{backend}`{hint}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    async def _cmd_invite(
        self,
        channel_id: str,
        post: dict,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.invite <session-id>` — get the requester into the session's MM
        channel, creating it for unmapped/external sessions first.
        """
        session_id = (arg or "").strip()
        if not session_id:
            self._post_cmd_reply(
                channel_id, ":grey_question: Usage: `.invite <session-id>`",
                thread_root,
            )
            return
        user_id = post.get("user_id") or ""

        # Already mapped → just add the requester to its channel.
        anchor = self.mapping.get_anchor(session_id)
        if anchor:
            await self._invite_requester(
                anchor.channel_id, user_id, session_id, channel_id, thread_root,
            )
            return

        # Unmapped — look the session up so we can create a channel for it.
        try:
            meta = await self.harness.get_session(session_id)
        except Exception:
            logger.debug("`.invite` get_session failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        if not meta:
            self._post_cmd_reply(
                channel_id,
                f":warning: No session `{session_id}` — check `.sessions`.",
                thread_root,
            )
            return

        # External pi sessions aren't resumable (harness 409), so a channel
        # would be a dead end — reject up front.
        backend = purpose.canonical_backend(meta.get("backend"))
        if meta.get("origin") == "external" and backend == "pi":
            self._post_cmd_reply(
                channel_id,
                f":warning: Session `{session_id[:12]}` is an external `pi` "
                "session — it can't be resumed from Mattermost.",
                thread_root,
            )
            return

        new_channel_id = await self._create_channel_for_session(meta)
        if not new_channel_id:
            self._post_cmd_reply(
                channel_id,
                ":warning: Couldn't create a channel for that session.",
                thread_root,
            )
            return

        # Resuming a still-open TUI session forks it — the MM channel becomes
        # a parallel branch, not a remote control of the live terminal.
        if meta.get("origin") == "external":
            try:
                self.mm.post(
                    new_channel_id,
                    ":information_source: _Heads-up: posting here resumes this "
                    "session via `--resume`. If it's still open in a terminal, "
                    "this channel becomes a **fork** of the conversation, not a "
                    "remote control of the live TUI._",
                )
            except Exception:
                logger.debug("failed posting resume-fork warning", exc_info=True)

        await self._invite_requester(
            new_channel_id, user_id, session_id, channel_id, thread_root,
        )

    async def _invite_requester(
        self,
        target_channel_id: str,
        user_id: str,
        session_id: str,
        reply_channel_id: str,
        thread_root: str | None,
    ) -> None:
        """Add ``user_id`` to ``target_channel_id`` and confirm in the channel
        the command was issued from."""
        if not user_id:
            self._post_cmd_reply(
                reply_channel_id,
                ":warning: Couldn't tell who to invite.",
                thread_root,
            )
            return
        try:
            self.mm.invite_user(target_channel_id, user_id)
        except Exception:
            logger.exception(
                "Failed to invite %s to channel %s", user_id, target_channel_id,
            )
            self._post_cmd_reply(
                reply_channel_id,
                ":warning: Failed to invite you to the channel.",
                thread_root,
            )
            return
        name = self._channel_name_for_session(session_id) or target_channel_id
        self._post_cmd_reply(
            reply_channel_id,
            f":white_check_mark: Invited you to ~{name}~ "
            f"for session `{session_id[:12]}`.",
            thread_root,
        )

    async def _run_stop_command(
        self,
        channel_id: str,
        session_id: str,
        thread_root: str | None,
    ) -> None:
        run_id = self.current_run_id_by_session.get(session_id)
        if not run_id:
            try:
                self.mm.post(
                    channel_id,
                    ":octagonal_sign: Nothing to stop.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        try:
            await self.harness.interrupt_run(session_id, run_id)
        except HarnessInterruptUnsupported:
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Can't interrupt this run — external session, not owned by the harness.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        except HarnessRunNotFound:
            pass
        except Exception:
            logger.exception("Failed to interrupt harness run %s/%s", session_id[:8], run_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Couldn't interrupt the run.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        self._end_tool_use_run(session_id)
        if self.typing:
            await self.typing.stop(session_id)
        logger.info("MM → agent-harness [%s]: stop (interrupt)", session_id[:8])
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
        self._recent_harness_sends.pop(session_id, None)
        if self.typing:
            await self.typing.stop(session_id)

    # ─────────────────────── agent-harness SSE handlers ───────────────────

    async def _on_harness_event(self, event_type: str, data: dict) -> None:
        # The harness SSE Event envelope (see agent_harness/models.py: Event)
        # carries ``session_id`` and ``run_id`` at the TOP level, alongside the
        # event-specific ``data`` payload. Flatten them into the dict handed
        # to handlers so a single ``data.get("session_id")`` works regardless
        # of whether the field came from the envelope or the inner payload.
        inner = dict(data.get("data") or {})
        for key in ("session_id", "run_id"):
            if inner.get(key) is None and data.get(key) is not None:
                inner[key] = data[key]

        session_id = (
            inner.get("session_id")
            or (inner.get("session") or {}).get("id")
        )
        if session_id and event_type in HARNESS_ACTIVITY_EVENTS:
            self.last_activity_ts[session_id] = time.monotonic()
            await self._start_typing_for_activity(session_id)
        elif session_id and event_type == "session.updated":
            # The harness reuses ``session.updated`` for both running- and
            # idle-flips. Read the status payload to decide: a running-flip is
            # activity (keep typing); a quiet-flip is the explicit "went quiet"
            # signal and must STOP typing — for external/observer sessions
            # there is no run-terminal event to do that cleanup, and repeated
            # freshness ticks would otherwise keep the silence watchdog from
            # ever firing, leaving typing stuck ON.
            if self._session_updated_is_activity(inner):
                self.last_activity_ts[session_id] = time.monotonic()
                await self._start_typing_for_activity(session_id)
            else:
                await self._stop_typing_for_idle(session_id)

        if event_type == "session.updated":
            await self._on_harness_session_seen(inner)
        elif event_type == "message":
            await self._on_harness_message(inner)
        elif event_type in {
            "message.delta",
            "tool.call",
            "tool.result",
            "permission.denied",
            "ping",
        }:
            return
        elif event_type in HARNESS_RUN_TERMINAL_EVENTS or event_type == "run.started":
            await self._on_harness_run_lifecycle(event_type, inner)
        elif event_type in HARNESS_WATCHDOG_EVENTS:
            await self._on_harness_watchdog_event(event_type, inner)
        else:
            logger.debug("Unhandled agent-harness event %s", event_type)

    @staticmethod
    def _session_updated_is_activity(inner: dict) -> bool:
        """Decide whether a ``session.updated`` payload represents the session
        actively working (→ typing) or having gone quiet (→ no typing / stop).

        The canonical status location is ``data.session.status`` (the harness
        dumps the full ``Session`` row there); ``data.status`` is accepted as a
        fallback. Only ``status == "running"`` counts as activity. Anything in
        ``HARNESS_QUIET_SESSION_STATUSES`` — or a missing/unknown status — is
        treated as NON-activity (the SAFE default): real output always also
        emits ``message`` / ``message.delta`` / ``tool.*`` events that keep
        typing alive independently, so a status-less freshness tick must not.
        """
        status = (inner.get("session") or {}).get("status") or inner.get("status")
        return status == "running"

    async def _stop_typing_for_idle(self, session_id: str) -> None:
        """A quiet ``session.updated`` flip is the explicit "went quiet"
        signal: drop the activity timestamp and stop the typing loop so the
        indicator clears immediately rather than waiting on the silence
        watchdog (which the prior freshness ticks would have kept resetting).

        EXCEPT while a run is in flight: the harness observer flips
        ``session.status`` to "idle" on pure rollout-file silence, which
        fires MID-RUN during long quiet tool calls. Run lifecycle events
        are authoritative — the terminal event (or the watchdog's Run-row
        reconcile, for a missed one) does the cleanup then. Sessions
        without a tracked run (external/observer) keep the immediate stop.
        """
        if session_id in self.active_run_by_session:
            logger.debug(
                "quiet status flip ignored: run %s still active — rollout "
                "freshness noise during long tool calls",
                self.active_run_by_session.get(session_id),
            )
            return
        self.last_activity_ts.pop(session_id, None)
        if self.typing:
            await self.typing.stop(session_id)

    async def _start_typing_for_activity(self, session_id: str) -> None:
        if not self.typing:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return
        await self.typing.start(session_id, anchor.channel_id, anchor.root_id)

    async def _on_harness_session_seen(self, data: dict) -> None:
        session = data.get("session") or data
        session_id = session.get("id") or data.get("session_id") or ""
        if not session_id:
            return
        if self.mapping.get_anchor(session_id):
            self._known_sessions.add(session_id)
            return  # already mapped
        if session_id in self._known_sessions:
            return
        # Suppress claude-code subagent transcripts — internal to a parent
        # run, would just churn channels.
        if _is_suppressed_session(session_id):
            self._known_sessions.add(session_id)
            return
        # Both external and harness-origin sessions surface as channels
        # here. External sessions are CLI processes the operator started
        # outside the harness. Harness-origin sessions reach this branch
        # when something outside the daemon created them (``mm-bridge
        # spawn``, integration tests against a live harness, or a future
        # IPC client) — the bridge daemon's own creations register the
        # mapping synchronously via ``mapping.link`` and hit the early
        # ``get_anchor`` exit above. Pre-existing test ghosts are filtered
        # by ``_bootstrap_known_sessions`` marking them ``known``, so the
        # bootstrap SSE replay of their stale ``session.updated`` events
        # is skipped before reaching this line.
        # Only mark known after the channel actually exists; otherwise a
        # transient MM error means we'd silently skip future
        # ``session.updated`` events for this session and never spawn its
        # channel. ``_create_channel_for_session`` returns None on failure
        # and logs at error level. Bound retries so a permanently-failing
        # slug (collision, invalid name) doesn't loop forever on every SSE
        # event for the session.
        if await self._create_channel_for_session(session):
            self._known_sessions.add(session_id)
            self._channel_create_attempts.pop(session_id, None)
            return
        attempts = self._channel_create_attempts.get(session_id, 0) + 1
        self._channel_create_attempts[session_id] = attempts
        if attempts >= MAX_CHANNEL_CREATE_ATTEMPTS:
            logger.error(
                "Giving up on channel creation for session %s after %d attempts",
                session_id[:24], attempts,
            )
            self._known_sessions.add(session_id)
            self._channel_create_attempts.pop(session_id, None)

    async def _update_resume_purpose(
        self,
        channel_id: str,
        session_id: str,
        backend: str | None,
        cwd: str | None,
    ) -> None:
        """Best-effort: refresh the Resume block in the channel Purpose.

        Reads current Purpose, swaps the trailing resume section for a
        fresh one (preserving the config section above the separator),
        and writes back. Skipped (no MM call) when the backend has no
        resume command, the channel fetch fails, or the merge produces
        no change. All errors are logged and swallowed — Purpose writes
        must never break the calling claim/reconcile path.

        The backend argument accepts purpose tokens, canon names, and
        raw SSE display strings; normalisation happens inside
        :func:`resume_header.format_resume_block`.
        """
        if not session_id:
            return
        # Unsupported backend → fall through with block=None so any
        # *stale* resume block from a previous (supported) binding is
        # stripped from Purpose. The merge layer is a no-op when
        # Purpose has no resume section, so this path is safe even for
        # channels that never had one.
        canonical = resume_header.normalize_backend(backend)
        if canonical is None:
            block: str | None = None
        else:
            block = resume_header.format_resume_block(
                canonical, session_id, cwd,
                dangerous=self.config.dangerous_permissions,
            )
            if block is None:
                return  # defensive — covered by the canonical guard above
        try:
            channel = self.mm.get_channel(channel_id)
        except Exception:
            logger.warning(
                "resume-purpose: could not fetch channel %s", channel_id,
                exc_info=True,
            )
            return
        current = channel.get("purpose") or ""
        merged = resume_header.merge_into_purpose(current, block)
        if merged == current:
            return
        # The MM channel_updated event we receive after our own write would
        # otherwise be mistaken for an operator edit; tag it as self-written
        # so the bridge skips its "purpose changed" notice post.
        self._note_self_wrote_purpose(channel_id, merged)
        try:
            self.mm.set_channel_purpose(channel_id, merged)
        except Exception:
            logger.warning(
                "resume-purpose: set_channel_purpose failed for %s", channel_id,
                exc_info=True,
            )

    async def _reconcile_resume_purposes(self) -> None:
        """Refresh the Resume block on every channel-level mapping.

        Runs once at startup. Pulls each session's metadata from agent-harness
        so the resume command points at the right backend AND cwd even
        after a daemon restart (when ``purpose_by_channel`` is empty and
        the persisted MM Purpose may not name a backend either). Falls
        back to the MM Purpose for backend resolution when the harness doesn't
        know the session. Thread-fork anchors are skipped — a fork
        session lives inside a thread; writing its resume command into
        the parent channel's Purpose would clobber the channel session's
        own block.
        """
        for anchor, session_id in list(self.mapping.anchor_to_session.items()):
            if anchor.is_thread:
                continue
            backend, cwd = await self._resume_meta_for(anchor.channel_id, session_id)
            try:
                await self._update_resume_purpose(
                    anchor.channel_id, session_id, backend, cwd,
                )
            except Exception:
                logger.warning(
                    "resume-purpose: reconcile failed for %s", anchor.channel_id,
                    exc_info=True,
                )

    async def _resume_meta_for(
        self, channel_id: str, session_id: str,
    ) -> tuple[str | None, str | None]:
        """Resolve (backend, cwd) for a reconcile-time resume write.

        Prefers agent-harness session metadata (the source of truth for cwd).
        Falls back to the MM-side backend resolution when the harness doesn't know
        the session (stale mapping, harness restart since the mapping was
        persisted). Cwd is left None in the fallback path because we don't
        have a trustworthy source — the resume command stays runnable but
        omits the `cd` prefix.
        """
        backend: str | None = None
        cwd: str | None = None
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            meta = {}
            logger.debug(
                "resume-purpose: agent-harness meta lookup failed for %s",
                session_id[:8], exc_info=True,
            )
        if meta:
            backend = meta.get("backend") or meta.get("backendName") or None
            cwd = (meta.get("project") or {}).get("path") or meta.get("cwd") or None
        if not backend:
            backend = self._backend_for_channel(channel_id)
        return backend, cwd

    def _backend_for_channel(self, channel_id: str) -> str | None:
        """Backend used to resume a session in `channel_id`.

        Resolution order:

        1. The cached ``PurposeConfig`` (populated when the bridge
           handled an invite/fork/spawn in this daemon's lifetime).
        2. Re-parse the channel's persisted Mattermost Purpose. The
           cache is empty after a daemon restart, and falling straight
           to ``default_backend`` here would write the wrong Resume
           command for codex/pi channels whose Purpose was set during
           a previous run.
        3. ``config.default_backend`` as a last resort (channels with
           no Purpose at all).
        """
        cached = self.purpose_by_channel.get(channel_id)
        if cached and cached.backend:
            return cached.backend
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            return self.config.default_backend or None
        raw = (ch.get("purpose") or "").strip()
        if not raw:
            return self.config.default_backend or None
        parsed = purpose.parse(
            raw,
            default_backend=self.config.default_backend,
            default_model=None,
            available_models_for=lambda _b: [],
            default_autorespond=self.config.default_autorespond,
        )
        return parsed.backend or self.config.default_backend or None

    async def _flush_queued(
        self,
        channel_id: str,
        session_id: str,
        queued_messages: list[str],
    ) -> None:
        for msg in queued_messages:
            self._record_harness_send(session_id, msg)
            try:
                run = await self.harness.create_run(session_id, msg)
                self._track_run_response(session_id, run)
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

    async def _create_channel_for_session(self, data: dict) -> str | None:
        session_id = data.get("id") or data.get("session_id") or ""
        if not session_id:
            return None
        channel_name = _session_to_channel_name(session_id)
        project = data.get("project") or {}
        display_name = (
            data.get("title")
            or project.get("name")
            or session_id[:12]
        )
        display_name = str(display_name)[:MM_DISPLAY_NAME_MAX]
        try:
            ch = self.mm.create_channel(
                name=channel_name,
                display_name=display_name,
                purpose=f"agent-harness session {session_id}",
            )
            channel_id = ch["id"]
            self.mapping.link(Anchor(channel_id), session_id)
            logger.info(
                "Created channel %s (%s) for agent-harness session %s",
                display_name, channel_name, session_id[:12],
            )
        except Exception:
            logger.exception("Failed to create channel for session %s", session_id[:12])
            return None
        # External-origin sessions have no harness stdin — a later MM post
        # must trigger ``_replace_external_session`` instead of a silent
        # ``create_run``. Tag the mapping so ``_on_mm_posted`` takes the
        # replacement path on the next inbound message.
        if data.get("origin") == "external":
            self._external_sessions.add(session_id)
        await self._update_resume_purpose(
            channel_id, session_id,
            data.get("backend"), project.get("path"),
        )
        return channel_id

    # ----- agent-harness message → Mattermost post -----

    async def _on_harness_message(self, data: dict) -> None:
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
                tool = block.get("name") or block.get("tool_name") or "unknown"
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

    _RECENT_HARNESS_SEND_MAX = 32

    def _record_harness_send(self, session_id: str, body: str) -> None:
        """Remember that we just shipped ``body`` to ``session_id`` so the
        SSE echo can be suppressed by ``_consume_dedup_match``."""
        if not session_id or not body:
            return
        q = self._recent_harness_sends.setdefault(
            session_id, deque(maxlen=self._RECENT_HARNESS_SEND_MAX),
        )
        q.append((time.monotonic(), body))

    def _consume_dedup_match(self, session_id: str, body: str) -> bool:
        """Return True (and pop the matching entry) if ``body`` matches a
        recent backend send for ``session_id`` within the configured window."""
        q = self._recent_harness_sends.get(session_id)
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
                self._recent_harness_sends.pop(session_id, None)
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
        meta = await self.harness.get_session(session_id) or {}
        return (meta.get("project") or {}).get("path") or None

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

    # ----- agent-harness run lifecycle → typing indicator -----

    async def _on_harness_run_lifecycle(self, event_type: str, data: dict) -> None:
        session_id = data.get("session_id", "")
        if not session_id:
            return

        if event_type == "run.started":
            # Track the in-flight run origin-agnostically (also for runs the
            # bridge didn't submit) so the silence watchdog and quiet-flip
            # handler can tell "run still active" from "gone quiet". The
            # run_id may legitimately be absent from the payload (None).
            self.active_run_by_session[session_id] = (
                data.get("run_id") or data.get("id")
            )
            await self._start_typing_for_activity(session_id)
            return

        run_id = data.get("run_id") or data.get("id")
        current = self.current_run_id_by_session.get(session_id)
        if run_id and current == run_id:
            self.current_run_id_by_session.pop(session_id, None)
        self.active_run_by_session.pop(session_id, None)
        self.last_activity_ts.pop(session_id, None)
        self._end_tool_use_run(session_id)
        if self.typing:
            await self.typing.stop(session_id)
        self._mention_triggerer_on_done(session_id)

    async def _on_harness_watchdog_event(self, event_type: str, data: dict) -> None:
        """Handle the two `RunProcess` watchdog events (agent-harness PR #10).

        Both events are SUPPLEMENTAL — the harness still emits a normal
        terminal event (`run.completed`/`run.failed`/`run.interrupted`)
        after the watchdog kill. This handler does NOT touch typing
        indicators or run-id state; the terminal-event handler does.

        - ``run.terminated_after_end_turn``: silent (INFO log only). The
          LLM already finished its turn; operator doesn't need noise.
        - ``run.timed_out_idle``: visible warning post to the session's
          anchor channel/thread, so the user knows the reply may be
          incomplete and can send a follow-up to resume.
        """
        session_id = data.get("session_id", "")
        sid_short = session_id[:8] if session_id else "?"

        if event_type == "run.terminated_after_end_turn":
            logger.info(
                "Harness watchdog terminated run after end_turn "
                "(session=%s hard_kill=%s returncode=%s grace_seconds=%s)",
                sid_short,
                data.get("hard_kill"),
                data.get("returncode"),
                data.get("grace_seconds"),
            )
            return

        if event_type == "run.timed_out_idle":
            logger.warning(
                "Harness watchdog killed idle run "
                "(session=%s idle_seconds=%s last_activity_event=%s "
                "last_activity_at=%s hard_kill=%s)",
                sid_short,
                data.get("idle_seconds"),
                data.get("last_activity_event"),
                data.get("last_activity_at"),
                data.get("hard_kill"),
            )
            if not session_id:
                return
            anchor = self.mapping.get_anchor(session_id)
            if not anchor:
                logger.debug(
                    "No anchor for idle-timeout session %s; skipping warning post",
                    sid_short,
                )
                return
            try:
                self.mm.post(
                    anchor.channel_id,
                    IDLE_TIMEOUT_WARNING,
                    root_id=anchor.root_id,
                )
            except Exception:
                logger.warning(
                    "Failed to post idle-timeout warning to channel %s (session %s)",
                    anchor.channel_id, sid_short, exc_info=True,
                )

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
