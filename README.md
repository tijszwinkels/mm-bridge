# mm-bridge

**Your coding agent, in your team chat.** Tag the bot in a Mattermost channel and a real
Claude Code, Codex, or pi session starts on your own machine — in your repos, with your
keys. Ask it a question, hand it a job, come back later. From your desk, your phone, or
the couch.

If you've seen [Claude Tag](https://www.anthropic.com/news/introducing-claude-tag) —
Anthropic's Claude-as-a-teammate in Slack — this is the self-hosted cousin: **your**
Mattermost (one you already run, or one the installer stands up for you), your hardware,
and whichever agent CLI you prefer. And where Claude Tag gives you one teammate, this gives
you as many as you care to start — see [Swarms](#swarms--many-agents-one-chat-server).

```
   you  (Mattermost — desktop, phone, watch-it-from-the-train)
    │
    ▼
 Mattermost  ⇄  mm-bridge  ⇄  agent-harness  ⇄  claude · codex · pi
                                                (your machine, your repos)
```

mm-bridge is the middle box: it maps **one channel (or thread) to one agent session** and
relays both directions.

## What it's like to use

**A channel is a conversation with an agent.** Invite the bot, say what you want. The
first message starts a session; replies stream back as posts. The channel keeps its
context for as long as it lives — scroll up and the whole project history is there.

**A thread is a side quest.** Reply in a thread and you get a *forked* session, so
tangents don't pollute the main conversation.

**Hand off work and walk away.** The agent can start *other* agents in *other* channels
and get on with something else — see [Swarms](#swarms--many-agents-one-chat-server).

**Watch the work, without drowning in it.** Tool calls collapse into a single
live-updating post per turn, so you can see it grepping and editing at a glance. A typing
indicator shows when it's thinking. If a run stalls or hits its runtime cap, the bridge
says so in plain language instead of going quiet.

**Files both ways.** Drop a screenshot, log, or PDF into the channel and the agent gets
the file. It attaches files back with a one-line directive in its reply.

**Agents can pull in humans.** When it needs a decision, the agent runs
`mm-bridge invite alice` and you're in the channel.

**Rescue a terminal session.** Started something in your terminal and want to continue it
from the train? `.sessions` lists recent sessions — including terminal ones the bridge
has never seen — and `.invite <session-id>` gives one a channel.

**Change your mind mid-conversation.** `.model sonnet`, `.backend codex`,
`.autorespond on`. Configuration lives in the channel; there's nothing to edit on the
server.

## Swarms — many agents, one chat server

Chat turns out to be a good substrate for running *several* agents at once, mostly because
the hard part of a swarm is having somewhere to watch it from.

**Agents spawn agents.** `mm-bridge spawn --title "Migrate the parser" "<brief>"` creates a
new channel with its own session and kicks it off. The parent channel gets a link to the
child; the child's header points back at the parent. A morning's work leaves a readable
tree of channels — one per unit of work, each with its full transcript, all searchable.

**Every agent can be a different agent.** Backend and model are per-channel, so a swarm can
be mixed: Claude Opus planning in one channel, Codex implementing in three, pi reviewing in
a fourth. Switch any of them at any time with `.backend` / `.model`.

**Agents talk to each other.** `mm-bridge post --channel <id>` and
`mm-bridge read --channel <id>` let one agent brief another, request a review, or settle a
disagreement — as ordinary posts, which is the whole point: you can read the negotiation
afterwards instead of guessing at it. Every cross-channel post is mirrored back into the
sender's own channel (`_→ also sent to ~channel~_`), so neither half of an exchange is
invisible. [`CLAUDE-include.md`](CLAUDE-include.md) ships a convention for this: each agent
picks a name and prefixes its posts, and a turn counter pauses the conversation and hands
it to a human if two agents get ~12 turns deep without one.

**You supervise from anywhere.** `.running` shows every session with a turn in flight right
now; `.sessions` lists recent ones across all agents. Any agent can pull you in with
`mm-bridge invite <you>` when it hits a decision it shouldn't make alone. It's all normal
Mattermost underneath — push notifications on your phone, one search box across every
agent's transcript.

## Where Claude Tag is better

Worth being straight about, since the comparison is the first thing this README makes:

- **It runs in Slack.** This doesn't, and won't. If your team lives in Slack, use Claude
  Tag. :slightly_smiling_face:
- **Per-thread memory.** Claude Tag gives each thread its own task-scoped context. Here a
  channel is *one long-running session*: everything ever said in it accumulates, and a
  thread fork **inherits** the channel's whole history rather than starting clean. In
  practice: open a new channel per task, and don't expect a two-month-old channel to stay
  sharp or cheap.
- **It's a managed product.** Claude Tag comes with a vendor, an SLA, and admin controls.
  mm-bridge is three moving parts on a box you own — bridge, harness, and agent CLIs — that
  you upgrade and debug yourself. There's no permission model beyond channel membership.
- **It's proactive.** Claude Tag can notice something and speak up. Agents here only act
  when a message reaches them (mention, or `.autorespond`).
- **Enterprise connectors.** Claude Tag reaches into your org's data sources. Here the
  agent gets whatever the local CLI already has — your repos, your shell, your MCP servers.
  A good trade for code, a bad one for tickets and docs.

What you get back for all that: your data stays on your machine, any backend you like, as
many agents at once as you want, and no per-seat bill.

## What you bring

mm-bridge itself is just a daemon and a CLI. The pieces around it are yours — though the
installer in the Quickstart can put most of them there for you:

- **A Mattermost server you administer.** *You* choose which one: a Mattermost you already
  run, or a fresh one the installer stands up in Docker on the same machine. Either way you
  need admin rights on it, because the bridge signs in as a **bot account** with a
  personal-access token. A hosted/Cloud Mattermost where you can't create bot tokens won't
  work. (These docs call the bot `@b3mo` by convention; you pick the name.)
- **A running [agent-harness](https://github.com/tijszwinkels/agent-harness)** — the local
  runtime that starts and supervises the agent processes. The bridge subscribes to its
  event stream and is useless without one. The installer sets this up as well.
- **At least one agent CLI, installed and logged in on the same host** as the harness —
  Claude Code, Codex, and/or pi. Sessions are ordinary local processes in your working
  directories, which is the whole point — and means they act with your credentials.
- **Linux, preferably.** One session-discovery trick (below) is Linux-only; macOS falls
  back to a less precise path.

One thing worth saying out loud: **the agent runs as you, on your machine, with your
credentials.** Anyone who can post in a bridged channel can drive it. Treat channel
membership like shell access.

## Quickstart — tell an agent to install it

Installing is itself an agent task. Clone this repo, open it in **Claude Code**, **Codex**,
or **pi** on the machine that should run your agents, and tell it what you've got.

**No Mattermost yet** — the common case, and fully supported:

> **Install this. I don't have a Mattermost server yet — set one up on this machine too.**

**You already run a Mattermost:**

> **Install this against my existing Mattermost at `https://chat.example.com` — I'm an
> admin there. Create the bot account and its token yourself.**

Either way the agent follows [`INSTALL.md`](INSTALL.md), a runbook written for agents: it
interviews you first (host user, install dir, which agent CLIs to enable, which directory
sessions should start in, bot name, where Mattermost should be reachable), then stands up
everything you don't already have — Mattermost + Postgres in Docker, agent-harness and
mm-bridge as user-level systemd services — and verifies each step with a checkpoint,
finishing on `mm-bridge doctor` and a live round-trip: you post in a channel, an agent
answers.

Nothing is guessed silently: when you say "use your judgement", it picks the documented
default and tells you which.

Prefer to drive it yourself? `INSTALL.md` is a plain numbered runbook for humans too. For
just the Python package (Python 3.11+):

```bash
uv sync && uv run mm-bridge --help
# or: uv pip install -e . && mm-bridge --help
```

Then run the daemon with `mm-bridge serve`.

## Talking to the bot

- **Mention it** (`@b3mo …`) — the default. Or `.autorespond on` and every message in the
  channel reaches the agent.
- **`.stop`** interrupts the running turn.
- **`@b3mo catch up 50`** feeds the last 50 channel messages into the session. (Happens
  automatically on the first message.)
- **`@b3mo leave`** sends the bot out of the channel.

A channel the bot has joined but nobody has engaged yet is **dormant**: no session, no
model, no cost. You can configure it (`.model`, `.backend`, `.autorespond`) before the
first real message; those settings are stored in the Channel Purpose and applied when the
session is finally created.

### In-channel dot-commands

The **bridge** handles these itself — they bypass the mention gate and are never
forwarded to the agent. An unknown `.word` gets a "try `.help`" reply.

| Command | What it does |
|---|---|
| `.help` | List these commands. |
| `.stop` | Interrupt the running turn in this channel. |
| `.autorespond [on\|off]` | Reply to every message, or only when mentioned (bare = toggle). |
| `.status` | Session id, backend, model, cwd, autorespond flag, run state, harness health. |
| `.model [<name>]` | Show or switch the model. Names are free text; a bad one fails loudly when the backend starts. |
| `.backend [<name>]` | Show or switch the backend (`claude`, `codex`, `pi`, …). Switching **resets the model** to that backend's default. |
| `.models` | Models available for this channel's backend, current one marked. |
| `.running` | Sessions with a run in flight right now. |
| `.sessions [N]` | The N most recent sessions across all agents, including terminal ones. Each shows its channel or an `.invite` hint. |
| `.invite <session-id>` | Get added to a session's channel, creating it for unmapped/terminal sessions. |

Switching model or backend in an **active** channel recreates the session, so `.stop` a
running turn first. Inside a **thread fork**, reading works but switching is refused — a
restart would replace the *channel's* session, not the thread's; switch from the channel.
The global listings (`.sessions`, `.running`, `.invite`) reveal operator-wide state, so in
a dormant channel they need an explicit mention.

## Commands the agent (or you) can run

These work inside any session that has a sidecar — i.e. an agent running on the same host
as the daemon. All of them accept `--channel <id>` to target another channel.

| Command | What it does |
|---|---|
| `mm-bridge serve` | Run the daemon (Mattermost WebSocket + REST ⇄ harness SSE). |
| `mm-bridge doctor` | Diagnose the local install: config, Mattermost auth, harness, sidecar dir. |
| `mm-bridge invite <user>` | Invite a Mattermost user into this session's channel. |
| `mm-bridge channel` | Print this session's `channel_id` (scripting/debug). |
| `mm-bridge channels [--title <kw>]` | List channels the bot can see, most recently active first. |
| `mm-bridge post [--file <path>] "<msg>"` | Post a message (`-` reads the body from stdin). |
| `mm-bridge read [-n N] [--since 1h]` | Print recent posts — how one agent reads another's channel. |
| `mm-bridge spawn "<prompt>"` | Start a sub-session in a new sibling channel. |

### `mm-bridge spawn`

```bash
mm-bridge spawn --title "Refactor the parser" --cwd ~/projects/foo --invite alice "…"
```

- `--title` — channel display name (default: derived from the prompt).
- `--cwd` — working directory for the new session.
- `--backend claude|codex|pi` and `--model <model>` — override the config defaults.
- `--invite <user>` — pull someone into the new channel.
- `--no-forward-prompt` — don't echo the kickoff message into the parent channel.

Pass `-` as the prompt to read it from stdin — the way to dispatch a long structured
brief without shell-quoting it:

```sh
mm-bridge spawn --title "Refactor" - <<'EOF'
Multi-line brief…
EOF
```

The full prompt reaches the sub-session verbatim; only the preview quoted into the
channels is capped (~12k chars) to stay under Mattermost's post limit. An empty or
non-piped stdin is rejected rather than dispatching a blank brief.

The parent channel gets a `:thread: Spawned **Title** in ~slug~` announcement, and the new
channel's header points back at its parent — so the tree is walkable from either end.

### Directives inside a reply

When the agent runs on the same host as the daemon, the bridge acts on directives in its
reply and strips them from the visible post:

- `<openFile path="/abs/path" [line="N"] />` — upload that file (must live under an
  allowed root; see `allowed_attachment_roots`).

[`CLAUDE-include.md`](CLAUDE-include.md) is the prompt snippet that teaches Claude how to
use all of this — drop it into your `CLAUDE.md`.

## Configure

Precedence: **class defaults < TOML file < environment variables**.

### TOML

Default path `~/.config/mm-bridge/config.toml` (override with `MM_BRIDGE_CONFIG`).

```toml
# ── Top-level session defaults ──────────────────────────────────────────────
# These keys are read from the TOP LEVEL of the file, so they MUST appear before
# the [mattermost] / [agent_harness] section headers further down. In TOML every
# key after a `[section]` header belongs to that section — put these under one
# and they're silently ignored (you fall back to the built-in defaults).

# Applied when a new session is created.
default_backend   = "claude"   # or "codex", "pi"
default_cwd       = "~/projects"   # your CODE root, not the install dir.
                                   # Unset, this falls back to your home directory —
                                   # set it explicitly. Must exist.
default_autorespond = false

# Per-backend default model, used when a channel / spawn doesn't pin one.
# This table also decides which backends get advertised in the welcome post —
# a backend with no default model here isn't offered to users.
# (The old scalar `default_model = "opus"` still works and maps onto `claude`.)
default_models = { claude = "opus", codex = "gpt-5.5" }

# Optional per-backend model catalog for the in-channel `.models` command.
# agent-harness's /v1/backends/{b}/models returns [] for every backend today,
# so this operator-maintained list is what `.models` shows (merged with the
# harness catalog once it's populated). `.model <name>` accepts free text
# regardless of this list.
models = { claude = ["opus", "sonnet", "haiku"], codex = ["gpt-5.5", "gpt-5.4-mini"] }

# Coalesce tool-use events into one per-turn placeholder post (edited as more
# tools run, left as a compact summary when the turn ends). Set false to hide
# them entirely — channels then carry only real replies and tool errors.
show_tool_use = true

# Mirror turns typed directly into the agent's own UI/CLI back into the bound
# channel as `_via coding agent:_ <body>` posts, so chat watchers see the full
# conversation. Bridge-originated sends and tool results are never mirrored.
mirror_direct_user_messages = true
direct_user_message_dedup_window_seconds = 30.0

# Auto-join: silently join every public channel the bot can see. Sessions are
# NOT created until someone actually engages the bot.
auto_join_public_channels  = false
auto_join_reconcile_seconds = 5.0

# Attachment safety — <openFile path="..."> only resolves files under these.
allowed_attachment_roots = ["~/projects"]

# State + sidecar paths.
state_file  = "~/.config/mm-bridge/state.json"
sidecar_dir = "~/.mm-bridge/sessions"

# Catch-up: inject the last N channel messages into a newly-created session
# so the model sees prior context (0 disables).
initial_catch_up_n = 50
catch_up_default_n = 50
catch_up_max_n     = 500

# ── Sections (must come last, after all the top-level keys above) ────────────
[mattermost]
url = "localhost"
port = 8065
scheme = "http"
team = "workspace"

# Optional user-facing base URL for permalinks the daemon embeds in headers and
# messages. Handy when the daemon reaches MM on localhost but humans reach it
# via a Tailscale hostname.
public_url = "http://mm.example.com:8065"

[agent_harness]
url = "http://localhost:8877"
```

### Environment

`.env` is not committed. All optional except `MM_BOT_TOKEN`:

| Variable | Purpose |
| --- | --- |
| `MM_BOT_TOKEN` | **Required.** Personal-access or bot token for the Mattermost bot. |
| `MM_URL` | Bare hostname or full URL (`http://host:port`). |
| `MM_PORT`, `MM_SCHEME` | Override parts of the URL. |
| `MM_TEAM` | Team slug the bot operates in. |
| `MM_PUBLIC_URL` | User-facing base URL for permalinks (see TOML `public_url`). |
| `AH_URL` | agent-harness server URL. |
| `MM_BRIDGE_DEFAULT_CWD` | Default working directory for new sessions. |
| `MM_BRIDGE_DEFAULT_BACKEND` | `claude`, `codex`, `pi`, … |
| `MM_BRIDGE_DEFAULT_MODEL` | Model slug (empty string → unset). |
| `MM_BRIDGE_DEFAULT_AUTORESPOND` | `1/true/yes/on` to enable autorespond by default. |
| `MM_SHOW_TOOL_USE` | Toggle `show_tool_use` without editing TOML. |
| `MM_MIRROR_DIRECT_USER_MESSAGES` | Toggle `mirror_direct_user_messages` without editing TOML. |
| `MM_AUTO_JOIN` | Toggle `auto_join_public_channels` without editing TOML. |
| `MM_BRIDGE_STATE` | Path to the state JSON. |
| `MM_BRIDGE_SIDECAR_DIR` | Sidecar directory. |
| `MM_BRIDGE_CONFIG` | Path to the TOML file. |

## Under the hood

**State file** — the canonical `session ↔ Anchor(channel_id, root_id?)` map. JSON, v3
schema; v2 is read transparently and re-emitted as v3 on the next save.

**Sidecar dir** — one file per session (`~/.mm-bridge/sessions/<session_id>`) holding the
channel id: one line for a channel session, two for a thread fork. `0700` directory,
`0600` files, reconciled from the state file at startup. This file is how an agent process
knows it's "live in Mattermost" and can use `invite` / `spawn` / `channel`.

<details>
<summary><b>How the CLI figures out which session it's running in</b> (four sources, in order)</summary>

1. **`CLAUDE_SESSION_ID`** — set by Claude Code's SessionStart hook
   (`~/.claude/hooks/export-session-id.sh`).
2. **`MM_BRIDGE_SESSION_ID`** — backend-agnostic env var. agent-harness pins it into
   backend tool-shell environments where it can.
3. **Live-codex parent (`/proc` tie-breaker)** — Linux-only. When the env vars miss, walk
   the parent-pid chain (depth ≤ 8) for a process whose `/proc/<pid>/comm` is `codex` and
   read the rollout filename out of its open fds; the UUID in that filename is adopted
   directly. This is what disambiguates *multiple codex sessions in the same cwd* — only
   the codex in our actual ancestor chain wins. Returns nothing on macOS (no `/proc`), for
   background tasks whose codex parent already exited, or when the ancestor holds no
   rollout fd — those fall through to step 4.
4. **Cwd-matched codex rollout** — scans `~/.codex/sessions/**/rollout-*.jsonl` in
   most-recently-active order and walks candidates whose `payload.cwd` matches the
   canonicalised caller cwd, adopting the first whose sidecar reads back as a valid
   channel anchor. Covers tool shells whose launcher couldn't pre-pin the env var
   (typically the first turn of a fresh session) and shells that outlive their parent.

There's a brief startup race between an agent starting and the daemon writing the sidecar.
Invoked in that window, the CLI fails cleanly with a "not in MM channel" error.

</details>

## Development

```bash
uv run -m pytest
```

Design docs for the current architecture live under [`specs/`](specs/) — one directory per
feature, overview + requirements + design.

## License

MIT — see [`LICENSE`](LICENSE).
