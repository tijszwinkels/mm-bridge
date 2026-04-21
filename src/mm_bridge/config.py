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

    # VibeDeck
    vd_url: str = "http://localhost:8765"

    # Session defaults
    default_backend: str = "claude"
    default_model: str | None = "opus"
    default_cwd: str = str(Path.home())
    default_autorespond: bool = False

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
    typing_stop_after_silence_seconds: float = 10.0

    # Claim window for matching session_added → pending MM invite
    pending_session_merge_window_seconds: float = 30.0

    # Name-sync debounce window
    name_sync_window_seconds: float = 10.0

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

        vd = data.get("vibedeck", {}) or {}
        if "url" in vd:
            self.vd_url = vd["url"]

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
        if "VD_URL" in env:
            self.vd_url = env["VD_URL"]
        if "VD_DEFAULT_CWD" in env:
            self.default_cwd = env["VD_DEFAULT_CWD"]
        if "VD_DEFAULT_BACKEND" in env:
            self.default_backend = env["VD_DEFAULT_BACKEND"]
        if "VD_DEFAULT_MODEL" in env:
            self.default_model = env["VD_DEFAULT_MODEL"] or None
        if "VD_DEFAULT_AUTORESPOND" in env:
            self.default_autorespond = env["VD_DEFAULT_AUTORESPOND"].lower() in (
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


@dataclass
class ChannelMapping:
    """Persistent mapping: MM channel ↔ VD session, plus thread-fork mappings.

    Schema v1 (legacy) only had `channel_to_session`. Schema v2 adds
    `thread_mapping` keyed by `f"{channel_id}:{root_post_id}"`.
    Loading a v1 file initialises `thread_mapping` as empty.
    """

    channel_to_session: dict[str, str] = field(default_factory=dict)
    session_to_channel: dict[str, str] = field(default_factory=dict)
    thread_mapping: dict[str, str] = field(default_factory=dict)
    session_to_thread: dict[str, str] = field(default_factory=dict)
    _path: str = ""
    _sidecar_dir: Path | None = None

    @classmethod
    def load(
        cls,
        path: str,
        sidecar_dir: Path | str | None = None,
    ) -> "ChannelMapping":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        sd = Path(sidecar_dir) if sidecar_dir else sidecar.DEFAULT_DIR
        m = cls(_path=path, _sidecar_dir=sd)
        if p.exists():
            data = json.loads(p.read_text())
            m.channel_to_session = data.get("channel_to_session", {})
            m.thread_mapping = data.get("thread_mapping", {})
            m.session_to_channel = {v: k for k, v in m.channel_to_session.items()}
            m.session_to_thread = {v: k for k, v in m.thread_mapping.items()}
        sidecar.reconcile(sd, m.session_to_channel)
        return m

    def save(self) -> None:
        Path(self._path).write_text(
            json.dumps(
                {
                    "channel_to_session": self.channel_to_session,
                    "thread_mapping": self.thread_mapping,
                },
                indent=2,
            )
        )

    # channel ↔ session
    def link(self, channel_id: str, session_id: str) -> None:
        self.channel_to_session[channel_id] = session_id
        self.session_to_channel[session_id] = channel_id
        self.save()
        if self._sidecar_dir is not None:
            sidecar.write(self._sidecar_dir, session_id, channel_id)

    def unlink_channel(self, channel_id: str) -> str | None:
        session_id = self.channel_to_session.pop(channel_id, None)
        if session_id:
            self.session_to_channel.pop(session_id, None)
            self.save()
            if self._sidecar_dir is not None:
                sidecar.delete(self._sidecar_dir, session_id)
        return session_id

    def get_session(self, channel_id: str) -> str | None:
        return self.channel_to_session.get(channel_id)

    def get_channel(self, session_id: str) -> str | None:
        return self.session_to_channel.get(session_id)

    # thread fork mapping
    @staticmethod
    def _thread_key(channel_id: str, root_id: str) -> str:
        return f"{channel_id}:{root_id}"

    def link_thread(self, channel_id: str, root_id: str, session_id: str) -> None:
        key = self._thread_key(channel_id, root_id)
        self.thread_mapping[key] = session_id
        self.session_to_thread[session_id] = key
        self.save()

    def unlink_thread(self, channel_id: str, root_id: str) -> str | None:
        key = self._thread_key(channel_id, root_id)
        session_id = self.thread_mapping.pop(key, None)
        if session_id:
            self.session_to_thread.pop(session_id, None)
            self.save()
        return session_id

    def get_thread_session(self, channel_id: str, root_id: str) -> str | None:
        return self.thread_mapping.get(self._thread_key(channel_id, root_id))

    def get_thread_location(self, session_id: str) -> tuple[str, str] | None:
        """Returns (channel_id, root_id) if session_id is a thread-fork session."""
        key = self.session_to_thread.get(session_id)
        if not key:
            return None
        channel_id, _, root_id = key.partition(":")
        return channel_id, root_id
