# Mattermost ↔ VibeDeck Bridge v2 — Design

## Implementation Context

### Source Files to Read

**Existing bridge (what v2 rewrites):**

| File | Why |
|---|---|
| `/home/claude/projects/mm-bridge/src/mm_bridge/main.py` | Entry point, env var parsing, `Bridge` construction |
| `/home/claude/projects/mm-bridge/src/mm_bridge/config.py` | `Config` + `ChannelMapping`; extend for TOML, thread mapping, new keys |
| `/home/claude/projects/mm-bridge/src/mm_bridge/bridge.py` | Core orchestration; the v1 `PendingMattermostSession` + claim logic is adapted, not deleted |
| `/home/claude/projects/mm-bridge/src/mm_bridge/mm_client.py` | REST+WS client; needs new methods (`publish_user_typing`, `upload_file`, `remove_channel_member`) and new WS event types (`user_added`, `user_removed`, `channel_updated`) |
| `/home/claude/projects/mm-bridge/src/mm_bridge/vd_client.py` | VibeDeck HTTP+SSE client; already supports `fork`, `source_session_id`; add `set_session_title`, handle `session_status` event |
| `/home/claude/projects/mm-bridge/CLAUDE.md` | Project-local notes about quirks (e.g. `firstMessage` truncation to 200 chars, `session_added` before file-read race) |
| `/home/claude/projects/mm-bridge/HANDOFF.md` | v1 design notes from the prior author |

**VibeDeck — read-only reference:**

| File | Why |
|---|---|
| `/home/claude/projects/VibeDeck/src/vibedeck/routes/sessions.py` | `POST /sessions/new`, `POST /sessions/{id}/fork`, `POST /sessions/{id}/send`, `POST /sessions/{id}/interrupt` — request/response shapes |
| `/home/claude/projects/VibeDeck/src/vibedeck/routes/titles.py` | `POST /api/session-titles/set` for MM → VD name sync |
| `/home/claude/projects/VibeDeck/src/vibedeck/broadcasting.py` | SSE event types: `session_added`, `message`, `session_summary_updated`, `session_status` |
| `/home/claude/projects/VibeDeck/src/vibedeck/models.py` | Pydantic schemas for session endpoints |
| `/home/claude/projects/VibeDeck/src/vibedeck/templates/static/js/commands.js` | Reference regex for `<openFile />` directive parsing |
| `/home/claude/projects/VibeDeck/src/vibedeck/backends/*/cli.py` | Each backend's `build_fork_command` / `supports_fork_session` — informs graceful fork-failure handling for opencode |

**Mattermost API reference:**

| File | Why |
|---|---|
| `/home/claude/projects/mm-bridge/.venv/lib/python3.12/site-packages/mattermostautodriver/endpoints/channels.py` | `remove_channel_member`, `patch_channel`, `get_channel`, `get_posts_for_channel` |
| `/home/claude/projects/mm-bridge/.venv/lib/python3.12/site-packages/mattermostautodriver/endpoints/users.py` | `publish_user_typing` |
| `/home/claude/projects/mm-bridge/.venv/lib/python3.12/site-packages/mattermostautodriver/endpoints/files.py` | `upload_file` (multipart) |

### Documentation to Review

- **Mattermost WebSocket event reference**: <https://developers.mattermost.com/integrate/reference/websocket/> — events `posted`, `user_added`, `user_removed`, `channel_updated`, payload shapes
- **Mattermost REST API — Channels**: <https://api.mattermost.com/#tag/channels> — especially `POST /channels/{id}/members`, `DELETE /channels/{id}/members/{user_id}`, `PATCH /channels/{id}`
- **Mattermost REST API — Files**: <https://api.mattermost.com/#tag/files> — `POST /files` (multipart), `GET /config/client?format=old` for `MaxFileSize`
- **Mattermost REST API — Posts**: <https://api.mattermost.com/#tag/posts> — `GET /channels/{id}/posts?per_page=N`, post `root_id` / `file_ids` semantics
- **`tomllib` stdlib docs**: <https://docs.python.org/3/library/tomllib.html>
- **`watchfiles` / `httpx` SSE**: already used in v1; no new docs needed

---

## Architecture Overview

### Module layout (post-refactor)

```
mm_bridge/
├── main.py                 # entry point, Config.load(), Bridge construction
├── config.py               # Config, ChannelMapping, ThreadMapping; TOML+env loader
├── bridge.py               # Orchestrator, glue between MM and VD clients
├── mm_client.py            # Mattermost REST + WS; adds typing/files/remove_member
├── vd_client.py            # VibeDeck HTTP + SSE; adds set_session_title, session_status
├── purpose.py              # NEW — Channel Purpose token parser
├── directives.py           # NEW — parses assistant output for <openFile/>, <leaveChannel/>
├── attribution.py          # NEW — per-session poster tracking; produces `username:` prefix
├── typing_indicator.py     # NEW — per-session refresh loop
└── name_sync.py            # NEW — bidirectional name sync with debounce set
```

Small, single-purpose modules keep `bridge.py` from ballooning the way v1's bridge did. Each module has one public entry the orchestrator calls; unit tests target these modules directly.

### Top-level data flow

```
┌──────────────┐                                   ┌──────────────┐
│  Mattermost  │◄── WS: posted / user_added /      │   VibeDeck   │
│              │       user_removed / channel_     │              │
│              │       updated                     │              │
│              │                                   │              │
│              │───► REST: create_channel /        │              │
│              │       patch_channel /             │              │
│              │       remove_member /             │              │
│              │       upload_file /               │              │
│              │       publish_user_typing         │              │
└──────▲───────┘                                   └──▲────────┬──┘
       │                                              │        │
       │ uses                                         │ uses   │ SSE:
       │                                              │        │  session_added /
       │        ┌───────────────────────────┐         │        │  message /
       │        │        bridge.py          │         │        │  session_summary_
       └────────┤                           ├─────────┘        │  updated /
                │ reads Config; mediates    │                  │  session_status
                │ between mm_client and     │                  │
                │ vd_client; delegates to   │                  │
                │ helper modules.           │                  │
                │                           │                  │
                │  purpose / directives /   │                  │
                │  attribution / typing /   │                  │
                │  name_sync                │                  │
                └───────────┬───────────────┘                  │
                            │                                  │
                            └──────────────────────────────────┘
                                     POST sessions, fork,
                                     send, set-title
```

### State (persistent and in-memory)

```python
# Persistent (state.json, extends v1)
{
  "channel_to_session": { "<channel_id>": "<session_id>" },
  "thread_mapping":     { "<channel_id>:<root_post_id>": "<session_id>" }
}

# In-memory only (rebuilt on each start)
- pending_mm_sessions: dict[channel_id, PendingMattermostSession]
  # Same as v1 — a channel that invited the bot is waiting for VD session_added to claim
- typing_tasks: dict[session_id, asyncio.Task]
- posters_by_session: dict[session_id, set[user_id]]  # attribution (§11.1)
- recent_renames: dict[(channel_id | session_id), (new_title, monotonic_ts)]  # debounce for name sync (§13.3)
- dead_threads: set[(channel_id, root_id)]  # fork failed, ignore further replies (§5.3)
```

### Event → Action map

Central reference. Everything in `bridge.py` is dispatch on these.

**Mattermost WebSocket events:**

| Event | Condition | Action |
|---|---|---|
| `posted` | `post.user_id == bot_user_id` | ignore (belt+suspenders) |
| `posted` | root_id set, no thread mapping yet | fork parent session → new session; post disclaimer; register mapping; send post to fork (§5) |
| `posted` | root_id set, thread mapping exists | forward to thread's forked session (with attribution if §11.1 triggered) |
| `posted` | no root_id, session mapped to channel | text matches `^@claude catch up( \d+)?$` → run catch-up (§10); text matches `^@claude leave\b.*$` → run leave (§12.4); else forward (with attribution, mention-only filter) |
| `posted` | root_id set, thread mapping exists, text matches `^@claude leave\b.*$` | run leave for thread mapping only (§12.4) — do not forward |
| `posted` | no root_id, no session mapped | ignore (v1's "first-message creates session" path removed; §1.3) |
| `user_added` | `data.user_id == bot_user_id`, no mapping for channel | read Purpose → create VD session → register pending → welcome post (§1.1) |
| `user_removed` | `data.user_id == bot_user_id`, mapping exists | delete mapping; leave VD session alive (§12.3) |
| `channel_updated` | `display_name` changed, mapping exists, not in debounce | POST `/api/session-titles/set` (§13.2); add (session_id, title) to debounce set |
| `channel_updated` | `purpose` changed, mapping exists | one-time notice *"Purpose changed — applies only to new sessions"* (§3.4) |

**VibeDeck SSE events:**

| Event | Action |
|---|---|
| `session_added` | try to claim a pending MM channel (match on cwd + firstMessage prefix, §2.2); if none → create a new MM channel (§2.1); link mapping |
| `message` | role=assistant → extract text, parse directives (`<openFile/>`, `<leaveChannel/>`), apply to MM: post with attachments / leave channel / strip disclaimer text |
| `session_summary_updated` | if not in debounce set → rename MM channel `display_name` (§13.1); add (channel_id, title) to debounce set |
| `session_status` | running=true → start `typing_tasks[session_id]` if not running; running=false → cancel it |

### Why separate modules (not one monolith)

v1's `bridge.py` is 468 lines and already awkward. Adding `openFile` parsing, attribution, typing loops, TOML config, and name-sync-with-debounce inline would bring it to ~1000 lines. Splitting into the helpers above:

- **testability** — `purpose.parse()`, `directives.extract()`, `attribution.format()` are pure functions
- **isolation** — a bug in typing indicators can't take out message forwarding
- **readability** — `bridge.py` reads as a dispatcher, the "what" not the "how"

---

## Component Designs

### 1. Config + state (`config.py`)

```python
@dataclass
class Config:
    mm_url: str = "localhost"
    mm_port: int = 8065
    mm_scheme: str = "http"
    mm_bot_token: str = ""
    mm_team: str = "workspace"
    vd_url: str = "http://localhost:8765"
    default_backend: str = "claude"
    default_model: str | None = "opus"
    default_cwd: str = str(Path.home())
    state_file: str = str(Path.home() / ".config/mm-bridge/state.json")
    config_file: str = str(Path.home() / ".config/mm-bridge/config.toml")
    allowed_attachment_roots: list[str] = field(default_factory=list)
    catch_up_default_n: int = 50
    catch_up_max_n: int = 500
    typing_refresh_seconds: float = 3.0
    typing_stop_after_silence_seconds: float = 10.0
    pending_session_merge_window_seconds: float = 30.0

    @classmethod
    def load(cls) -> "Config":
        """Precedence: TOML file → env var → built-in default."""
        ...
```

**Loading order:**
1. Start from class defaults.
2. If `$XDG_CONFIG_HOME/mm-bridge/config.toml` (or `~/.config/mm-bridge/config.toml`) exists, parse with `tomllib`, overlay values.
3. Apply env vars (MM_BOT_TOKEN, MM_URL, etc.) on top — env wins over TOML. This matches v1 and lets ops bypass the file.
4. TOML parse errors → log + `sys.exit(1)` (US-4.1).

**`ChannelMapping` extension:**

```python
@dataclass
class ChannelMapping:
    channel_to_session: dict[str, str]
    thread_mapping: dict[str, str]  # NEW, key = f"{channel_id}:{root_id}"

    def get_session_for_thread(self, channel_id, root_id) -> str | None: ...
    def link_thread(self, channel_id, root_id, session_id) -> None: ...
    def unlink_thread(self, channel_id, root_id) -> None: ...
```

Backwards-compatible: if state.json lacks `thread_mapping`, default to `{}`.

### 2. Channel Purpose parser (`purpose.py`)

```python
KNOWN_BACKENDS = {"claude", "codex", "pi", "opencode"}

@dataclass
class PurposeConfig:
    backend: str
    model: str | None
    mention_only: bool
    warnings: list[str]  # non-fatal; surfaced as channel message

def parse(
    purpose: str,
    default_backend: str,
    default_model: str | None,
    available_models_for: Callable[[str], list[str]],
) -> PurposeConfig:
    """Returns a PurposeConfig with warnings. Never raises."""
```

Algorithm:
1. Split on `,`; strip; lowercase; drop empty.
2. If first token is in `KNOWN_BACKENDS`, use it; else if it matches a known model, backend = default_backend; else → warn, backend = default_backend.
3. Remaining tokens: if matches a model for the chosen backend (via `available_models_for(backend)` which hits VibeDeck's `/backends/{name}/models` once and caches), set model; if equal to `mention-only`, set flag; else → warn.
4. `model_index` is computed from the model list at session-create time (VibeDeck takes an index, not a name).

**Edge cases:**
- Empty purpose → `PurposeConfig(default_backend, default_model, False, [])`.
- Two model tokens → last wins, warn on the first.
- Unknown backend → defaults used, warn with the unknown token verbatim.

### 3. Directive parser (`directives.py`)

```python
@dataclass
class Directive:
    kind: Literal["open_file", "leave_channel"]
    attrs: dict[str, str]

_OPEN_FILE_RE = re.compile(r"<openFile\s+([^>]*)/>", re.IGNORECASE)
_LEAVE_CHANNEL_RE = re.compile(r"<leaveChannel(?:\s+([^>]*))?\s*/>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

def extract(text: str) -> tuple[str, list[Directive]]:
    """Returns (cleaned_text, directives). The text has all directives removed."""
```

Single pass over the text, replace each match with empty string, collect directives in order. The regex for openFile mirrors VibeDeck's JS (`commands.js:114`).

### 4. Attribution (`attribution.py`)

```python
class PosterTracker:
    def __init__(self):
        self._posters_by_session: dict[str, set[str]] = {}

    def note_post(self, session_id: str, user_id: str) -> bool:
        """Record that user_id posted in session_id. Returns True if attribution
        should be applied (≥2 distinct human posters)."""
        posters = self._posters_by_session.setdefault(session_id, set())
        posters.add(user_id)
        return len(posters) >= 2

    def format(self, text: str, username: str, attribute: bool) -> str:
        return f"{username}: {text}" if attribute else text

    def forget(self, session_id: str) -> None:
        self._posters_by_session.pop(session_id, None)
```

**Subtlety**: we only apply the prefix once a *second* human has spoken. The way `note_post` + `format` work, the second human's *first* post already gets the prefix (because we add to the set before checking), and so do all subsequent posts from anyone. Earlier single-user posts are not retroactively re-sent (US-11.1 explicit).

On `<leaveChannel />` or user-kick: call `forget(session_id)` to free memory.

### 5. Typing indicator (`typing_indicator.py`)

```python
class TypingIndicator:
    def __init__(self, mm_client, refresh_s: float, silence_timeout_s: float):
        self._mm = mm_client
        self._refresh_s = refresh_s
        self._silence_s = silence_timeout_s
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self, session_id: str, channel_id: str, parent_id: str | None) -> None: ...
    async def stop(self, session_id: str) -> None: ...
    async def shutdown(self) -> None:
        for t in self._tasks.values(): t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
```

Each task:
```python
async def _loop(self, session_id, channel_id, parent_id):
    try:
        while True:
            try:
                self._mm.publish_user_typing(channel_id, parent_id)
            except Exception:
                logger.debug("publish_user_typing failed", exc_info=True)
            await asyncio.sleep(self._refresh_s)
    except asyncio.CancelledError:
        pass
```

The "10 s silence" requirement from US-8.1 is enforced by the caller: if we haven't seen a `session_status running=false` but also haven't seen any event for `session_id` in 10 s, bridge calls `stop(session_id)`. This is a safety net for VibeDeck SSE gaps; the happy path is just start/stop on the SSE events.

### 6. Name sync (`name_sync.py`)

```python
class NameSync:
    """Bidirectional sync with ping-pong debounce."""

    def __init__(self, window_seconds: float = 10.0):
        self._debounce: dict[tuple[str, str], float] = {}
        self._window = window_seconds

    def note_remote_update(self, kind: Literal["mm", "vd"], id_: str, title: str) -> None:
        """Remember that WE set this title so the reflected event gets ignored."""
        self._debounce[(kind, id_)] = time.monotonic()
        # Note: we key on (kind, id_) not (kind, id_, title) because the reflected
        # event may arrive with a normalized/truncated title.

    def should_sync(self, kind: Literal["mm", "vd"], id_: str) -> bool:
        ts = self._debounce.get((kind, id_))
        if ts is None:
            return True
        if time.monotonic() - ts > self._window:
            self._debounce.pop((kind, id_), None)
            return True
        return False
```

Flow:
- VD `session_summary_updated` arrives → `should_sync("vd", session_id)` — if True, call MM `patch_channel` and `note_remote_update("mm", channel_id, title)`.
- MM `channel_updated` arrives with changed `display_name` → `should_sync("mm", channel_id)` — if True, call VD `set_session_title` and `note_remote_update("vd", session_id, title)`.

10 s is long enough to cover the reflected event latency, short enough that a genuine user follow-up rename isn't accidentally swallowed. The set is stale-tolerant — misfires just drop one update.

### 7. Updated Mattermost client (`mm_client.py`)

**New methods:**

```python
def publish_user_typing(self, channel_id: str, parent_id: str | None = None) -> None:
    self._driver.users.publish_user_typing(
        self._bot_user_id, channel_id, parent_id=parent_id
    )

def remove_channel_member(self, channel_id: str) -> None:
    self._driver.channels.remove_channel_member(channel_id, self._bot_user_id)

def upload_file(self, channel_id: str, path: Path) -> str:
    """Upload a file to a channel, return file_id."""
    with path.open("rb") as f:
        resp = self._driver.files.upload_file(
            channel_id=channel_id, files={"files": (path.name, f)},
        )
    return resp["file_infos"][0]["id"]

def post_with_attachments(
    self, channel_id: str, message: str,
    file_ids: list[str] | None = None,
    root_id: str | None = None,
) -> dict:
    options = {"channel_id": channel_id, "message": message}
    if file_ids: options["file_ids"] = file_ids
    if root_id:  options["root_id"] = root_id
    return self._driver.posts.create_post(options=options)

def get_posts(self, channel_id: str, limit: int) -> list[dict]:
    """Most-recent N posts (oldest first in returned list)."""
    resp = self._driver.posts.get_posts_for_channel(
        channel_id, params={"per_page": limit}
    )
    order = resp["order"]  # newest first per MM
    return [resp["posts"][pid] for pid in reversed(order)]

def get_user(self, user_id: str) -> dict:
    return self._driver.users.get_user(user_id)

def get_max_file_size(self) -> int:
    """Read MaxFileSize from server config; fallback to 50 MB."""
    try:
        cfg = self._driver.system.get_client_configuration()
        return int(cfg.get("MaxFileSize", 50 * 1024 * 1024))
    except Exception:
        return 50 * 1024 * 1024
```

**WebSocket handler changes** — extend `_ws_connect` to dispatch four event types, not just `posted`:

```python
HANDLED_EVENTS = {"posted", "user_added", "user_removed", "channel_updated"}

async for msg in ws:
    ...
    event_type = event.get("event")
    if event_type not in HANDLED_EVENTS:
        continue
    data = event.get("data", {})

    if event_type == "posted":
        post = json.loads(data["post"])
        if post.get("user_id") == self._bot_user_id:
            continue
        await on_posted(post)
    elif event_type == "user_added":
        if data.get("user_id") == self._bot_user_id:
            channel_id = event.get("broadcast", {}).get("channel_id") \
                         or data.get("channel_id")
            await on_bot_added(channel_id)
    elif event_type == "user_removed":
        if data.get("user_id") == self._bot_user_id:
            channel_id = event.get("broadcast", {}).get("channel_id") \
                         or data.get("channel_id")
            await on_bot_removed(channel_id)
    elif event_type == "channel_updated":
        channel_data = json.loads(data["channel"])
        await on_channel_updated(channel_data)
```

The `listen_websocket` method's callback signature is generalized from the current `on_message, on_channel_created` to a single `handlers` dict so we don't grow the positional arg list.

**Removed methods:**
- `join_all_team_channels` (§1.2 — no more auto-join)
- `join_channel` (only referenced by the removed reconciler)

### 8. Updated VibeDeck client (`vd_client.py`)

**New methods:**

```python
async def set_session_title(self, session_id: str, title: str | None) -> None:
    await self._http.post(
        "/api/session-titles/set",
        json={"session_id": session_id, "title": title},
    )

async def list_models(self, backend: str) -> list[str]:
    resp = await self._http.get(f"/backends/{backend}/models")
    resp.raise_for_status()
    return resp.json().get("models", [])

async def fork_session(self, session_id: str, message: str) -> dict:
    resp = await self._http.post(
        f"/sessions/{session_id}/fork", json={"message": message},
    )
    # 403 = fork disabled; 501 = backend doesn't support it
    if resp.status_code in (403, 501):
        return {"status": "fork_unavailable", "reason": resp.json().get("detail", "")}
    resp.raise_for_status()
    return resp.json()

async def get_session_meta(self, session_id: str) -> dict:
    """List-sessions filtered to one. Used for projectPath lookup for openFile."""
    sessions = await self.list_sessions()
    for s in sessions:
        if s.get("id") == session_id: return s
    return {}
```

**SSE: no API change** — `stream_events` already accepts any event type. `bridge.py` adds a case for `session_status`.

### 9. Bridge orchestrator (`bridge.py`)

Dispatcher. Each event type routes to a handler method that:
1. Looks up state (channel/session/thread mapping).
2. Delegates work to helpers (`purpose.parse`, `directives.extract`, `attribution.format`, etc.).
3. Calls one or two client methods.
4. Updates state.

Key handler sketches (abbreviated):

```python
async def _on_bot_added(self, channel_id: str) -> None:
    if self.mapping.get_session(channel_id):
        return  # already mapped (v1 leftover or reconnect)

    ch = self.mm.get_channel(channel_id)
    cfg = self.purpose_parser.parse(ch.get("purpose", ""))
    cwd = self.config.default_cwd
    welcome = self._format_welcome(cfg, cwd)
    for w in cfg.warnings:
        self.mm.post_message(channel_id, f":warning: {w}")

    try:
        resp = await self.vd.create_session(
            message="",   # empty — §4.2 note
            cwd=cwd,
            backend=cfg.backend,
            model_index=self._resolve_model_index(cfg),
        )
    except Exception:
        logger.exception("session create failed for channel %s", channel_id)
        self.mm.post_message(channel_id, ":warning: Failed to start session.")
        return

    self.pending_mm_sessions[channel_id] = PendingMattermostSession(
        channel_id=channel_id, cwd=cwd, backend=cfg.backend,
        initial_message="", requested_at=time.monotonic(),
    )
    self.mm.post_message(channel_id, welcome)

async def _on_posted(self, post: dict) -> None:
    # Thread?
    if post.get("root_id"):
        await self._on_thread_post(post)
        return
    channel_id = post["channel_id"]
    session_id = self.mapping.get_session(channel_id)
    if not session_id:
        # v1's "create on first message" is gone (§1.3). Just check if a pending
        # session is still warming up for this channel.
        if channel_id in self.pending_mm_sessions:
            self.pending_mm_sessions[channel_id].queued_messages.append(post["message"])
        return
    await self._forward_user_post(channel_id, session_id, post, thread_root=None)

async def _forward_user_post(self, channel_id, session_id, post, thread_root) -> None:
    msg = post["message"].strip()
    # Catch-up command?
    if (m := _CATCH_UP_RE.match(msg)):
        await self._run_catch_up(channel_id, session_id, thread_root, m)
        return
    # mention-only filter?
    cfg = self._cfg_cache.get(channel_id)  # stored at session-create
    if cfg and cfg.mention_only and f"@{self.mm.bot_username}" not in msg:
        return
    msg = msg.replace(f"@{self.mm.bot_username}", "").strip()
    # attribution
    attribute = self.attribution.note_post(session_id, post["user_id"])
    if attribute:
        username = self._resolve_username(post["user_id"])
        msg = self.attribution.format(msg, username, True)
    await self.vd.send_message(session_id, msg)
```

Helpers like `_run_catch_up` and `_handle_openfile_directive` are small and self-contained; design spells out intent, code fills in the clerical detail.

### 10. `openFile` attachment flow

```python
async def _relay_assistant_message(self, session_id: str, msg: dict) -> None:
    channel_id = self.mapping.get_channel(session_id)
    thread_root = self._thread_root_for(session_id)  # may be None

    text = extract_text_from_blocks(msg["blocks"])
    cleaned, directives = directives.extract(text)

    # Leave-channel directive takes precedence over everything else
    leave = next((d for d in directives if d.kind == "leave_channel"), None)
    if leave:
        if cleaned.strip():
            self.mm.post_with_attachments(channel_id, _truncate_for_mm(cleaned),
                                          root_id=thread_root)
        await self._handle_leave(session_id, channel_id, leave.attrs.get("reason"))
        return

    # openFile directives → file uploads
    file_ids: list[str] = []
    warnings: list[str] = []
    for d in directives:
        if d.kind != "open_file": continue
        file_id, warning = await self._try_upload(channel_id, session_id, d.attrs)
        if file_id: file_ids.append(file_id)
        if warning: warnings.append(warning)

    message = _truncate_for_mm(cleaned)
    if warnings:
        message = (message + "\n\n" + "\n".join(warnings)).strip()

    # Don't post if we have nothing to post and no files
    if not message and not file_ids:
        return

    self.mm.post_with_attachments(channel_id, message, file_ids=file_ids,
                                  root_id=thread_root)

async def _try_upload(self, channel_id, session_id, attrs) -> tuple[str | None, str | None]:
    path_str = attrs.get("path")
    if not path_str: return None, None
    project_path = await self._project_path_for(session_id)
    resolved = _resolve_and_validate(path_str, project_path, self.config.allowed_attachment_roots)
    if not resolved:
        return None, f"_Could not attach `{path_str}`: outside allowed roots._"
    if not resolved.exists():
        return None, f"_Could not attach `{path_str}`: file not found._"
    size = resolved.stat().st_size
    if size > self.mm.get_max_file_size():
        return None, f"_Could not attach `{path_str}`: file too large ({size} bytes)._"
    try:
        file_id = self.mm.upload_file(channel_id, resolved)
        if line := attrs.get("line"):
            return file_id, None  # line hint appended elsewhere by caller if desired
        return file_id, None
    except Exception:
        logger.exception("upload failed")
        return None, f"_Could not attach `{path_str}`: upload failed._"
```

`_resolve_and_validate` handles US-6.4: `Path.resolve(strict=False)`, check that the real path is under `project_path` or one of `allowed_attachment_roots`; reject if escapes.

### 11. Thread forks

```python
async def _on_thread_post(self, post: dict) -> None:
    channel_id = post["channel_id"]
    root_id = post["root_id"]
    thread_key = f"{channel_id}:{root_id}"

    if thread_key in self._dead_threads:
        return

    session_id = self.mapping.thread_mapping.get(thread_key)
    if session_id:
        await self._forward_user_post(channel_id, session_id, post, thread_root=root_id)
        return

    parent_session = self.mapping.get_session(channel_id)
    if not parent_session:
        return  # thread in an unmapped channel → nothing to fork from

    # Fork
    try:
        resp = await self.vd.fork_session(parent_session, post["message"])
    except Exception:
        logger.exception("fork failed")
        self.mm.post_with_attachments(channel_id,
            ":warning: Couldn't fork this conversation. Reply in the main channel instead.",
            root_id=root_id)
        self._dead_threads.add((channel_id, root_id))
        return
    if resp.get("status") == "fork_unavailable":
        self.mm.post_with_attachments(channel_id,
            f":warning: Couldn't fork ({resp.get('reason')}). Reply in the main channel instead.",
            root_id=root_id)
        self._dead_threads.add((channel_id, root_id))
        return

    # fork returned status=forking; the new session_id will arrive via SSE session_added.
    # Store the thread_key in a *pending-fork* dict keyed on (channel_id, parent_session)
    # so the session_added claim logic can map it.
```

**Claim flow for fork-originated sessions:** when `session_added` arrives, we currently match pending_mm_sessions by `cwd + firstMessage prefix`. Forks inherit cwd from the parent, and the forked-session's firstMessage matches what we sent. So the existing claim mechanism works — we just need a second pending-dict for pending-forks and check it before pending_mm_sessions.

Disclaimer post happens after the fork is claimed (we need the forked session_id to post with `root_id` and to link the mapping in a consistent order).

---

## Error Handling Strategy

- **Network errors to MM or VD**: log with `logger.exception`, retry once for upload/publish operations, surface a single warning post to the channel. Do not crash the bridge. v1 already pattern-matches this; v2 extends it.
- **Auth errors (401 from MM or VD)**: treat as fatal. `sys.exit(1)` so systemd/operator notices. Silent degradation here is worse than a hard restart.
- **TOML parse error at startup**: fatal. Exit non-zero with a clear message (US-4.1).
- **Unknown SSE event types**: log at DEBUG and ignore. Lets VibeDeck evolve without breaking us.
- **Bot not a member of a channel it's trying to post in** (stale mapping, someone kicked the bot externally): catch the 403, drop the mapping, post nothing.
- **VibeDeck session that got deleted** (manual cleanup): `send_message` returns 404 → drop the mapping, post *":warning: The VibeDeck session this channel was bound to no longer exists. Re-invite the bot to start fresh."*

All error paths aim at "surface the error in the channel if cheap to do so, keep the bridge running otherwise."

---

## Testing Strategy

### Unit tests (pytest, `tests/`)

Fully local, no Mattermost or VibeDeck dependency:

| Module | What to test |
|---|---|
| `purpose.parse()` | default branch, all-tokens branch, unknown-backend warning, unknown-model warning, mention-only flag, `Claude,Opus` case-normalization |
| `directives.extract()` | single openFile, multiple openFiles, openFile with line, leaveChannel (with and without reason), mixed directives, no directives, directive inside markdown code block (should still match — VibeDeck JS does), leftover text |
| `attribution.PosterTracker` | one user 3 posts → no prefix, user A then user B → second post gets prefix, per-session isolation, forget() clears |
| `name_sync.NameSync` | debounce expires after window, key isolation between kinds, note_remote_update prevents reflected sync |
| `config.Config.load()` | TOML-only, env-only, TOML+env (env wins), missing file, invalid TOML (exits), unknown keys ignored |
| `ChannelMapping` | loads v1 state.json without thread_mapping; thread link/unlink; round-trips through save/load |

### Integration tests (`tests/integration/`)

Marked `@pytest.mark.integration`, opt-in via env. Use a real local Mattermost (already in `~/projects/mm-bridge` dev setup) and a real local VibeDeck.

| Flow | Assertion |
|---|---|
| Bot invited to empty channel | welcome message posted, MM channel not created twice when session_added arrives, mapping persisted |
| User types `@claude catch up 10` | last 10 non-bot posts forwarded as a context block, confirmation reply posted |
| User starts thread | fork called, disclaimer posted, thread-session mapping saved, reply in thread routes to forked session |
| OpenFile in assistant message | file attached to post, path outside project_path blocked, oversized file produces warning |
| `<leaveChannel reason="done"/>` | farewell posted, bot removed from channel, mapping deleted, re-invite creates fresh session |
| Typing indicator | MM typing event published every 3s while running, stops on session_status running=false |
| Channel renamed in MM | VibeDeck session title updates via `/api/session-titles/set`, no ping-pong |
| VibeDeck generates summary | MM channel display_name updates, no `channel_updated` feedback loop |
| v1 state.json migrates | existing mappings preserved, thread_mapping initialized empty |

### Manual smoke test (post-implementation)

Go through the user-story acceptance criteria in `requirements.md` by hand — the matrix is already the test plan. Most can be done in 15 minutes with one local MM + VibeDeck.

---

## Open Questions & Risks

1. **Empty initial message to VibeDeck on invite.** `create_session` requires a non-empty `message` (sessions.py:595 raises on empty). We can't start a session without one. Resolution options (design decision, pick one during implementation):
   - Send a minimal placeholder like `"(session initialized from Mattermost)"` so the backend has something to anchor on; Claude will likely just say hello back.
   - Change `create_session` to accept empty and just pipe the bot's first "hello" from the user as the real first message. Requires VibeDeck change — out of scope per §Out-of-Scope.
   - Defer session creation until the user's first message post-invite. But then the welcome message can't confirm backend/model/cwd because the session isn't created yet.

   **Lean**: placeholder message ("Hello! I've been added to a Mattermost channel. Waiting for the user to ask something."). Works today, no VibeDeck change.

2. **`channel_updated` frequency.** Mattermost emits `channel_updated` on *any* channel change (purpose, header, display_name, scheme). We need to diff against last-seen values to know what actually changed. Cache the last-seen `display_name` and `purpose` per channel, compare on event.

3. **Permission denial on session-create.** v1 handles `status == "permission_denied"` by posting a warning. v2 keeps the same behaviour; the new wrinkle is that Purpose-driven backend selection might trigger permission denials we didn't see with the old single-backend env var.

4. **Mattermost plugin coordination.** The separate "widget architecture" spec assumes a Mattermost plugin consumes structured events. v2 still posts plain text + attachments; when the plugin ships, `post_with_attachments` grows a `props` param for typed payloads without disrupting this bridge.
