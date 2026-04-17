"""Bridge configuration."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os


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
    vd_default_cwd: str = str(Path.home())
    vd_new_session_backend: str | None = None
    vd_new_session_model_index: int | None = None

    # Behavior
    sync_existing: bool = False  # Create channels for pre-existing sessions on startup

    # State
    state_file: str = str(Path.home() / ".config/mm-bridge/state.json")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            mm_url=os.environ.get("MM_URL", "localhost"),
            mm_port=int(os.environ.get("MM_PORT", "8065")),
            mm_scheme=os.environ.get("MM_SCHEME", "http"),
            mm_bot_token=os.environ.get("MM_BOT_TOKEN", ""),
            mm_team=os.environ.get("MM_TEAM", "workspace"),
            vd_url=os.environ.get("VD_URL", "http://localhost:8765"),
            vd_default_cwd=os.environ.get("VD_DEFAULT_CWD", str(Path.home())),
            vd_new_session_backend=os.environ.get("VD_NEW_SESSION_BACKEND") or None,
            vd_new_session_model_index=(
                int(os.environ["VD_NEW_SESSION_MODEL_INDEX"])
                if os.environ.get("VD_NEW_SESSION_MODEL_INDEX")
                else None
            ),
            sync_existing=os.environ.get("MM_SYNC_EXISTING", "").lower() in ("1", "true", "yes"),
            state_file=os.environ.get(
                "MM_BRIDGE_STATE",
                str(Path.home() / ".config/mm-bridge/state.json"),
            ),
        )


@dataclass
class ChannelMapping:
    """Persistent mapping between MM channels and VibeDeck sessions."""
    channel_to_session: dict[str, str] = field(default_factory=dict)
    session_to_channel: dict[str, str] = field(default_factory=dict)
    _path: str = ""

    @classmethod
    def load(cls, path: str) -> "ChannelMapping":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mapping = cls(_path=path)
        if p.exists():
            data = json.loads(p.read_text())
            mapping.channel_to_session = data.get("channel_to_session", {})
            mapping.session_to_channel = {
                v: k for k, v in mapping.channel_to_session.items()
            }
        return mapping

    def save(self) -> None:
        Path(self._path).write_text(
            json.dumps({"channel_to_session": self.channel_to_session}, indent=2)
        )

    def link(self, channel_id: str, session_id: str) -> None:
        self.channel_to_session[channel_id] = session_id
        self.session_to_channel[session_id] = channel_id
        self.save()

    def get_session(self, channel_id: str) -> str | None:
        return self.channel_to_session.get(channel_id)

    def get_channel(self, session_id: str) -> str | None:
        return self.session_to_channel.get(session_id)
