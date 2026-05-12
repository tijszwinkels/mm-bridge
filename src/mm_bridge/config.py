"""Bridge configuration (TOML + env) and channel/thread mapping state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import json
import logging
import os
import sys
import tomllib

from . import sidecar

logger = logging.getLogger(__name__)


def _expand(path: str) -> str:
    return str(Path(os.path.expandvars(path)).expanduser())


def _config_file_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "mm-bridge" / "config.toml"


@dataclass
class Config:
    # Mattermost
    mm_url: str = "localhost"
    mm_port: int = 8065
    mm_scheme: str = "http"
    mm_bot_token: str = ""
    mm_team: str = "workspace"

    # User-facing MM base URL (e.g. a Tailscale hostname) used for
    # permalinks embedded in channel headers / messages. Empty = fall back
    # to the daemon's own ``mm_scheme://mm_url:mm_port``.
    mm_public_url: str = ""

    # agent-harness
    agent_harness_url: str = "http://localhost:8877"

    # Session defaults
    default_backend: str = "claude"
    default_model: str | None = "opus"
    default_cwd: str = str(Path.home())
    default_autorespond: bool = False

    # Surface tool-use notifications in channel as a coalesced placeholder
    # post. On by default — gives visibility into what Claude is doing.
    # Disable to keep channels to only real assistant replies.
    show_tool_use: bool = True

    # Mirror user turns that arrive via the coding agent's own UI/CLI
    # (rather than via Mattermost) back into the channel, so MM watchers
    # see the conversation in full. Tool results (role=user, tool_result
    # blocks) are never mirrored. Bridge-originated sends are de-duped
    # against a recent-send window so MM-routed turns don't echo back.
    mirror_direct_user_messages: bool = True
    # Window inside which an outbound send is treated as the source of a
    # subsequent role=user echo. Claude Code startup can be slow, so the
    # default is generous.
    direct_user_message_dedup_window_seconds: float = 30.0

    # When a run finishes, post a standalone ``@<username>`` in the same
    # channel/thread to notify the user whose message triggered it.
    # No-op if the run had no tracked triggerer (e.g. autorespond loops
    # or queued messages flushed at session start).
    mention_user_when_done: bool = True

    # Auto-join all public channels (silent presence — sessions start on
    # first engagement, not on join).
    auto_join_public_channels: bool = False
    auto_join_reconcile_seconds: float = 5.0

    # State + config file paths
    state_file: str = str(Path.home() / ".config/mm-bridge/state.json")
    config_file: str = str(_config_file_path())
    sidecar_dir: str = str(Path.home() / ".mm-bridge/sessions")

    # Attachment safety
    allowed_attachment_roots: list[str] = field(default_factory=list)

    # Catch-up command
    catch_up_default_n: int = 50
    catch_up_max_n: int = 500
    # Auto-inject the last N channel messages on first session creation
    # (0 disables). Applies to both invite and engagement flows.
    initial_catch_up_n: int = 50

    # Typing indicator
    typing_refresh_seconds: float = 3.0
    typing_stop_after_silence_seconds: float = 15.0

    # Claim window for matching session_added → pending MM invite
    pending_session_merge_window_seconds: float = 30.0

    # Name-sync debounce window
    name_sync_window_seconds: float = 10.0

    # Mirrors the coding-agent elevated-permissions mode. When True,
    # the resume command written into each bridged channel's Purpose
    # includes the matching elevated-permission flag
    # (`--dangerously-skip-permissions` for claude,
    # `--dangerously-bypass-approvals-and-sandbox` for codex), so resuming
    # a session locally lands in the same permission state the daemon is
    # running in. The backend does not expose its own `_skip_permissions`
    # state via a public API, so this is operator-owned. Defaults to True
    # because the typical operator runs the bridge with elevated local
    # coding-agent sessions; set ``MM_BRIDGE_DANGEROUS_PERMISSIONS=0``
    # (or the TOML key to false) to
    # opt out for constrained deployments.
    dangerous_permissions: bool = True

    @classmethod
    def load(cls) -> "Config":
        """Precedence: class defaults < TOML file < env vars. Fatal on bad TOML."""
        cfg = cls()

        config_path_env = os.environ.get("MM_BRIDGE_CONFIG")
        if config_path_env:
            cfg.config_file = _expand(config_path_env)

        toml_path = Path(cfg.config_file)
        if toml_path.exists():
            try:
                with toml_path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                logger.error("Invalid TOML in %s: %s", toml_path, exc)
                sys.exit(1)
            cfg._apply_toml(data)
            logger.info("Loaded config from %s", toml_path)
        else:
            logger.info("No config file at %s — using defaults + env", toml_path)

        cfg._apply_env()
        cfg.default_cwd = _expand(cfg.default_cwd)
        cfg.state_file = _expand(cfg.state_file)
        cfg.sidecar_dir = _expand(cfg.sidecar_dir)
        cfg.allowed_attachment_roots = [_expand(p) for p in cfg.allowed_attachment_roots]
        return cfg

    # ----- internals -----

    def _apply_mm_url(self, raw: str) -> None:
        """Accept either a bare hostname or a full URL for MM_URL.

        A full URL (``http://host[:port]`` / ``https://host[:port]``) is
        split into scheme/host/port so callers can keep a single canonical
        value in their .env files. Explicit ``MM_PORT`` / ``MM_SCHEME``
        still override (applied after this by the caller).
        """
        if not (raw.startswith("http://") or raw.startswith("https://")):
            self.mm_url = raw
            return
        parsed = urlparse(raw)
        self.mm_scheme = parsed.scheme
        self.mm_url = parsed.hostname or raw
        if parsed.port is not None:
            self.mm_port = parsed.port
        else:
            self.mm_port = 443 if parsed.scheme == "https" else 80

    def _apply_toml(self, data: dict) -> None:
        for key in (
            "default_backend",
            "default_model",
            "default_cwd",
            "default_autorespond",
            "show_tool_use",
            "mirror_direct_user_messages",
            "direct_user_message_dedup_window_seconds",
            "mention_user_when_done",
            "auto_join_public_channels",
            "auto_join_reconcile_seconds",
            "state_file",
            "sidecar_dir",
            "allowed_attachment_roots",
            "catch_up_default_n",
            "catch_up_max_n",
            "initial_catch_up_n",
            "typing_refresh_seconds",
            "typing_stop_after_silence_seconds",
            "pending_session_merge_window_seconds",
            "name_sync_window_seconds",
            "dangerous_permissions",
        ):
            if key in data:
                setattr(self, key, data[key])

        mm = data.get("mattermost", {}) or {}
        if "url" in mm:
            self.mm_url = mm["url"]
        if "port" in mm:
            self.mm_port = int(mm["port"])
        if "scheme" in mm:
            self.mm_scheme = mm["scheme"]
        if "team" in mm:
            self.mm_team = mm["team"]
        if "public_url" in mm:
            self.mm_public_url = mm["public_url"]

        ah = data.get("agent_harness", {}) or {}
        if "url" in ah:
            self.agent_harness_url = ah["url"]

    def _apply_env(self) -> None:
        env = os.environ
        if "MM_URL" in env:
            self._apply_mm_url(env["MM_URL"])
        if "MM_PORT" in env:
            self.mm_port = int(env["MM_PORT"])
        if "MM_SCHEME" in env:
            self.mm_scheme = env["MM_SCHEME"]
        if "MM_BOT_TOKEN" in env:
            self.mm_bot_token = env["MM_BOT_TOKEN"]
        if "MM_TEAM" in env:
            self.mm_team = env["MM_TEAM"]
        if "MM_PUBLIC_URL" in env:
            self.mm_public_url = env["MM_PUBLIC_URL"]
        if "AH_URL" in env:
            self.agent_harness_url = env["AH_URL"]
        if "MM_BRIDGE_DEFAULT_CWD" in env:
            self.default_cwd = env["MM_BRIDGE_DEFAULT_CWD"]
        if "MM_BRIDGE_DEFAULT_BACKEND" in env:
            self.default_backend = env["MM_BRIDGE_DEFAULT_BACKEND"]
        if "MM_BRIDGE_DEFAULT_MODEL" in env:
            self.default_model = env["MM_BRIDGE_DEFAULT_MODEL"] or None
        if "MM_BRIDGE_DEFAULT_AUTORESPOND" in env:
            self.default_autorespond = env["MM_BRIDGE_DEFAULT_AUTORESPOND"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_SHOW_TOOL_USE" in env:
            self.show_tool_use = env["MM_SHOW_TOOL_USE"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_MIRROR_DIRECT_USER_MESSAGES" in env:
            self.mirror_direct_user_messages = (
                env["MM_MIRROR_DIRECT_USER_MESSAGES"].lower()
                in ("1", "true", "yes", "on")
            )
        if "MM_MENTION_USER_WHEN_DONE" in env:
            self.mention_user_when_done = env["MM_MENTION_USER_WHEN_DONE"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_AUTO_JOIN" in env:
            self.auto_join_public_channels = env["MM_AUTO_JOIN"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_BRIDGE_STATE" in env:
            self.state_file = env["MM_BRIDGE_STATE"]
        if "MM_BRIDGE_SIDECAR_DIR" in env:
            self.sidecar_dir = env["MM_BRIDGE_SIDECAR_DIR"]
        if "MM_BRIDGE_DANGEROUS_PERMISSIONS" in env:
            self.dangerous_permissions = (
                env["MM_BRIDGE_DANGEROUS_PERMISSIONS"].strip().lower()
                in ("1", "true", "yes", "on")
            )


@dataclass(frozen=True)
class Anchor:
    """A conversation anchor: a channel, optionally narrowed to a thread root.

    Channel sessions carry ``root_id=None``; thread-fork sessions carry the
    root post id of the thread they inhabit. The class is frozen so it's
    hashable and safe as a dict key.

    Empty-string ``root_id`` is normalised to ``None`` so JSON round-trips
    and caller shortcuts (``Anchor(ch, "")``) produce canonical anchors.
    """

    channel_id: str
    root_id: str | None = None

    def __post_init__(self) -> None:
        if self.root_id == "":
            # dataclass is frozen — bypass __setattr__ for normalisation.
            object.__setattr__(self, "root_id", None)

    @property
    def is_thread(self) -> bool:
        return self.root_id is not None


# JSON state-file schema version. v2→v3 collapsed channel_to_session +
# thread_mapping into `entries`. v3→v4 added `last_event_seq` for resuming
# the agent-harness SSE stream from a known cursor across bridge restarts.
STATE_SCHEMA_VERSION = 4


@dataclass
class ChannelMapping:
    """Persistent mapping: session ↔ conversation anchor.

    Every session has exactly one :class:`Anchor`. Channel sessions have
    ``root_id=None``; thread-fork sessions carry the root post id. One
    forward map (``anchor → session``) and one reverse (``session → anchor``)
    keep both kinds of session discoverable through the same API.

    On-disk schema:

    * v2 (legacy): ``{"channel_to_session": {...}, "thread_mapping": {...}}``
      — read transparently on load and re-emitted as the current version
      on the next save.
    * v3: ``{"version": 3, "entries": [{"channel_id", "root_id", "session_id"}]}``.
    * v4: adds ``"last_event_seq": <int|null>`` for SSE-cursor resume.
    """

    anchor_to_session: dict[Anchor, str] = field(default_factory=dict)
    session_to_anchor: dict[str, Anchor] = field(default_factory=dict)
    last_event_seq: int | None = None
    _path: str = ""
    _sidecar_dir: Path | None = None

    @classmethod
    def load(
        cls,
        path: str,
        sidecar_dir: Path | str | None = None,
        *,
        reconcile_sidecars: bool = True,
    ) -> "ChannelMapping":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        sd = Path(sidecar_dir) if sidecar_dir else sidecar.DEFAULT_DIR
        m = cls(_path=path, _sidecar_dir=sd)
        if p.exists():
            data = json.loads(p.read_text())
            m._ingest(data)
        if reconcile_sidecars:
            sidecar.reconcile(
                sd,
                {
                    sid: (a.channel_id, a.root_id)
                    for sid, a in m.session_to_anchor.items()
                },
            )
        return m

    def _ingest(self, data: dict) -> None:
        """Populate in-memory maps from any supported on-disk schema."""
        version = data.get("version")
        if version in (3, 4) and isinstance(data.get("entries"), list):
            for entry in data["entries"]:
                sid = entry.get("session_id")
                cid = entry.get("channel_id")
                if not sid or not cid:
                    continue
                rid = entry.get("root_id") or None
                self._add(Anchor(cid, rid), sid)
            if version >= 4:
                seq = data.get("last_event_seq")
                self.last_event_seq = seq if isinstance(seq, int) else None
            return
        # Legacy v2 (or the even older v1 that lacked thread_mapping).
        for cid, sid in (data.get("channel_to_session") or {}).items():
            if cid and sid:
                self._add(Anchor(cid), sid)
        for key, sid in (data.get("thread_mapping") or {}).items():
            if not sid or not key:
                continue
            cid, _, rid = key.partition(":")
            if cid and rid:
                self._add(Anchor(cid, rid), sid)

    def _add(self, anchor: Anchor, session_id: str) -> None:
        """Insert an (anchor, session) pair, clearing any stale reverse row."""
        prev_session = self.anchor_to_session.get(anchor)
        if prev_session and prev_session != session_id:
            self.session_to_anchor.pop(prev_session, None)
        self.anchor_to_session[anchor] = session_id
        self.session_to_anchor[session_id] = anchor

    def save(self) -> None:
        entries = [
            {
                "channel_id": a.channel_id,
                "root_id": a.root_id,
                "session_id": sid,
            }
            for a, sid in self.anchor_to_session.items()
        ]
        Path(self._path).write_text(
            json.dumps(
                {
                    "version": STATE_SCHEMA_VERSION,
                    "entries": entries,
                    "last_event_seq": self.last_event_seq,
                },
                indent=2,
            )
        )

    def set_event_seq(self, seq: int) -> None:
        """Update the persisted SSE cursor and flush to disk.

        Monotonic: callers that observe events out of order — or replay an
        event with a stale sequence on reconnect — won't accidentally
        rewind the cursor.
        """
        if self.last_event_seq is not None and seq <= self.last_event_seq:
            return
        self.last_event_seq = seq
        self.save()

    # ----- anchor API -----

    def link(self, anchor: Anchor, session_id: str) -> None:
        """Bind `session_id` to `anchor`, replacing any prior binding."""
        self._add(anchor, session_id)
        self.save()
        if self._sidecar_dir is not None:
            sidecar.write(
                self._sidecar_dir, session_id, anchor.channel_id, anchor.root_id,
            )

    def unlink(self, anchor: Anchor) -> str | None:
        """Remove `anchor`'s binding; return the session_id it held, or None."""
        session_id = self.anchor_to_session.pop(anchor, None)
        if session_id:
            self.session_to_anchor.pop(session_id, None)
            self.save()
            if self._sidecar_dir is not None:
                sidecar.delete(self._sidecar_dir, session_id)
        return session_id

    def get_session(self, anchor: Anchor) -> str | None:
        return self.anchor_to_session.get(anchor)

    def get_anchor(self, session_id: str) -> Anchor | None:
        return self.session_to_anchor.get(session_id)
