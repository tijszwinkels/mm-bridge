# mm-bridge

Mattermost ↔ agent-harness bridge — one channel (or thread) per Claude Code / Codex session.

A daemon connects a Mattermost bot account (conventionally `@claude`) to an agent-harness instance and maps conversations to sessions:

- **A channel** the bot is invited to → one agent-harness session. Messages in the channel become user turns; assistant replies stream back as posts.
- **A thread** inside a channel → its own *forked* session, so side-quests don't pollute the main conversation.
- **`mm-bridge spawn "<prompt>"`** from inside a session → a fresh sibling channel with its own session, with the parent announced via a post that links the child's `~channel~` header back upstream.

Each session gets a small **sidecar file** (`~/.mm-bridge/sessions/<session_id>`) so a Claude Code or codex session running on the same machine can self-identify as "live in Mattermost" and use the CLI helpers (`invite`, `spawn`, `channel`).

The CLI discovers the current session id from one of four sources, in order:

1. **`CLAUDE_SESSION_ID`** — populated by Claude Code's SessionStart hook (`~/.claude/hooks/export-session-id.sh`).
2. **`MM_BRIDGE_SESSION_ID`** — backend-agnostic env var. agent-harness pins this into backend tool-shell environments when available.
3. **Live-codex parent (`/proc` tie-breaker)** — Linux-only. When env vars miss, walk the parent-pid chain (depth ≤ 8) for a process whose `/proc/<pid>/comm` is `codex` and read the rollout filename out of its open fds. The UUID embedded in that filename is adopted directly. This is what disambiguates the "multiple codex sessions in the same cwd" case: only the codex actually in our ancestor chain is the one we belong to. Returns nothing on macOS (no `/proc`), background tasks where the codex parent already exited, or when the codex ancestor has no rollout fd held open — those cases fall through to step 4.
4. **Cwd-matched codex rollout** — final fallback that scans `~/.codex/sessions/.../rollout-*.jsonl` in most-recently-active order and walks candidates whose `payload.cwd` matches the (canonicalised) caller cwd, adopting the first one whose sidecar reads back as a valid channel anchor. Helps tool shells whose codex launcher couldn't pre-pin the env var (typically the very first turn of a fresh session) and tool shells that outlive their codex parent. Note: there's a brief startup race between codex starting and the daemon writing the sidecar — if mm-bridge is invoked in that window the fallback fails cleanly with a "not in MM channel" error, which is the same behaviour the Claude Code path has had.

## Install

Requires Python 3.11+. Using [`uv`](https://github.com/astral-sh/uv):

```bash
uv sync
uv run mm-bridge --help
```

Or install the console script into a venv:

```bash
uv pip install -e .
mm-bridge --help
```

## Configure

The bridge reads, in order of precedence: **class defaults < TOML file < environment variables**.

### TOML

Default path: `~/.config/mm-bridge/config.toml` (override with `MM_BRIDGE_CONFIG=/path/to/config.toml`).

```toml
# Mattermost server the daemon talks to.
[mattermost]
url = "localhost"
port = 8065
scheme = "http"
team = "workspace"

# Optional user-facing base URL used when the daemon embeds permalinks
# in channel headers / messages. Handy when the daemon reaches MM at
# localhost but humans reach it via a Tailscale hostname.
public_url = "http://pillar.tail72f2bc.ts.net:8065"

[agent_harness]
url = "http://localhost:8877"

# Session defaults applied when a new session is created.
default_backend   = "claude"   # or "codex"
default_cwd       = "~/projects"
default_autorespond = false

# Per-backend default model, applied when a channel / spawn doesn't pin one
# explicitly. (The old scalar `default_model = "opus"` is deprecated but still
# honoured — it maps onto `claude`.)
default_models = { claude = "opus", codex = "gpt-5.5" }

# Optional per-backend model catalog surfaced by the in-channel `.models`
# command. The agent-harness `/v1/backends/{b}/models` endpoint returns []
# for every backend today, so this operator-maintained list is the source
# `.models` shows (merged with the harness catalog once it's populated).
# `.model <name>` accepts any free-text name regardless of this list.
models = { claude = ["opus", "sonnet", "haiku"], codex = ["gpt-5.5", "gpt-5.4-mini"] }

# Coalesce Claude's tool-use events into one per-turn placeholder post
# (edited as more tools run, left as a compact summary when the turn
# ends). Set false to hide them entirely — channels then carry only
# real assistant replies and tool errors.
show_tool_use = true

# Mirror user turns typed directly into the agent's UI/CLI back into the
# bound MM channel as ``_via coding agent:_ <body>`` posts so MM watchers
# see the full conversation. Bridge-originated sends and tool results are
# never mirrored. Set false to keep direct-typed turns invisible to MM.
mirror_direct_user_messages = true
direct_user_message_dedup_window_seconds = 30.0

# Auto-join: silently join every public channel the bot can see.
# Sessions are NOT created until someone actually engages the bot.
auto_join_public_channels  = false
auto_join_reconcile_seconds = 5.0

# Attachment safety — <openFile path="..."> directives only resolve
# files under these roots.
allowed_attachment_roots = ["~/projects"]

# State + sidecar paths.
state_file  = "~/.config/mm-bridge/state.json"
sidecar_dir = "~/.mm-bridge/sessions"

# Catch-up: inject the last N channel messages into a newly-created
# session so the model sees prior context (0 disables).
initial_catch_up_n = 50
catch_up_default_n = 50
catch_up_max_n     = 500
```

### Environment

`.env` is not committed. The daemon reads these env vars (all optional except `MM_BOT_TOKEN`):

| Variable                 | Purpose                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `MM_BOT_TOKEN`           | **Required.** Personal-access or bot token for the Mattermost bot. |
| `MM_URL`                 | Bare hostname or full URL (`http://host:port`).                    |
| `MM_PORT`, `MM_SCHEME`   | Override parts of the URL.                                         |
| `MM_TEAM`                | Team slug the bot operates in.                                     |
| `MM_PUBLIC_URL`          | User-facing base URL for permalinks (see TOML `public_url`).       |
| `AH_URL`                 | agent-harness server URL.                                          |
| `MM_BRIDGE_DEFAULT_CWD`  | Default working directory for new sessions.                        |
| `MM_BRIDGE_DEFAULT_BACKEND` | `claude` or `codex`.                                            |
| `MM_BRIDGE_DEFAULT_MODEL` | Model slug (empty string → unset).                                |
| `MM_BRIDGE_DEFAULT_AUTORESPOND` | `1/true/yes/on` to enable autorespond by default.           |
| `MM_SHOW_TOOL_USE`       | Toggle `show_tool_use` without editing TOML.                       |
| `MM_MIRROR_DIRECT_USER_MESSAGES` | Toggle `mirror_direct_user_messages` without editing TOML. |
| `MM_AUTO_JOIN`           | Toggle `auto_join_public_channels` without editing TOML.           |
| `MM_BRIDGE_STATE`        | Path to the state JSON.                                            |
| `MM_BRIDGE_SIDECAR_DIR`  | Sidecar directory.                                                 |
| `MM_BRIDGE_CONFIG`       | Path to the TOML file.                                             |

## Commands

### `mm-bridge serve`

Runs the daemon. Connects to Mattermost (WebSocket + REST), subscribes to agent-harness SSE, and relays messages in both directions.

```bash
mm-bridge serve
```

### `mm-bridge invite <username>`

Inside a Claude Code or codex session that already has a sidecar, invites a Mattermost user to the session's channel:

```bash
mm-bridge invite tijs
```

### `mm-bridge channel`

Prints the current session's `channel_id` (debug / scripting).

### `mm-bridge spawn [opts] "<prompt>"`

Creates a fresh sibling channel with its own agent-harness session and kicks off `<prompt>`. Options:

- `--title "<name>"` — display name for the new channel (default: derived from the prompt).
- `--cwd <path>` — working directory for the new session.
- `--backend claude|codex` — backend for the new session.
- `--model <model>` — model for the new session (e.g. `claude-fable-5`), overriding the per-backend config default.
- `--invite <user>` — invite a user to the new channel.
- `--no-forward-prompt` — don't post the kickoff message in the parent channel.

The parent channel gets a `:thread: Spawned **Title** in ~slug~` announcement (threaded under the originating thread when spawning from a thread-fork). The new channel's header is set to `Parent: ~parent-slug~`, with a `[thread](permalink)` suffix when spawned from a thread-fork.

## In-channel dot-commands

Type these in any bridged channel (or thread) — the **bridge** handles them
itself. They work with or without an `@claude` mention, bypass the
mention-only gate, and are never forwarded to the agent. An unknown `.word`
gets an "unknown command — try `.help`" reply rather than reaching the agent.

| Command | What it does |
|---|---|
| `.help` | List these commands. |
| `.stop` | Interrupt the running turn in this channel. |
| `.autorespond [on\|off]` | Reply to every message, or only when @mentioned (bare = toggle). Persisted in the Channel Purpose. |
| `.status` | Session id, backend, model, cwd, autorespond flag, run state, harness status. |
| `.model [<name>]` | Show the current model, or switch it. Names are free text — a bad one fails loudly with the backend error. Switching recreates the session, so `.stop` any active run first. |
| `.models` | List the available models for this channel's backend (from the `[models]` config table + the harness catalog), marking the current one. |
| `.running` | Sessions with a run in flight right now. |
| `.sessions [N]` | The N most recent sessions across all agents — including terminal (TUI) sessions not yet on Mattermost. Each shows its channel or an `.invite` hint. |
| `.invite <session-id>` | Get added to a session's Mattermost channel, creating it first for unmapped/terminal sessions. Posting into a resumed terminal session **forks** it (see the channel's bootstrap note). |

Session-scoped commands (`.stop`, `.status`, `.model`) reply "No session in
this channel" when the channel has none; the rest work regardless.

## Inside-a-session directives

When a Claude / Codex session runs on the same host as the daemon, a couple of directives are recognized inside the assistant's reply:

- `<openFile path="/abs/path" [line="N"] />` — the bridge uploads the file (after checking it's under an allowed root) and strips the directive from the visible post.

See `CLAUDE-include.md` for the prompt snippet that teaches Claude how to use these.

## State & sidecar layout

- **State file** — canonical `session ↔ Anchor(channel_id, root_id?)` map. JSON, v3 schema; v2 is read transparently and re-emitted as v3 on the next save.
- **Sidecar dir** — one file per session: `<session_id>` containing the `channel_id` (one line for channel sessions, two for thread-forks). `0700` directory, `0600` files. Reconciled from the state file at startup.

## Specs

Design docs for the current architecture live under `specs/`.

## Tests

```bash
uv run -m pytest
```
