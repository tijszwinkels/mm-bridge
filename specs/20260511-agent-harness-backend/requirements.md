# mm-bridge → agent-harness Backend Port — Requirements

## 0. Background (read once, then skip)

`mm-bridge` currently talks to **VibeDeck** (`vd_client.py`, `_on_vd_*` SSE
handlers, `[vibedeck]` config block). The agent-harness project at
`/home/claude/projects/agent-harness-echo/` is the clean-slate replacement —
same conceptual shape (sessions, runs, SSE), different wire contract
(`/v1/...`, sequence-numbered events, run-id-addressable interrupts,
external-vs-harness origin split). Live harness: `http://pillar.tail72f2bc.ts.net:8877`.

This spec is a **hard replacement**, not a coexistence layer. After this work
lands, mm-bridge speaks only agent-harness; the VibeDeck client is deleted.

## 1. What's being replaced

### 1.1 Wire contract

The bridge stops issuing any of these VibeDeck calls:

- `POST /sessions/new` (replaced by `POST /v1/sessions` + `POST /v1/sessions/{id}/runs`)
- `POST /sessions/{id}/send` (→ `POST /v1/sessions/{id}/runs`)
- `POST /sessions/{id}/interrupt` (→ `DELETE /v1/sessions/{id}/runs/{run_id}`)
- `POST /sessions/{id}/fork` (→ `POST /v1/sessions/{id}/forks`, **synchronous**)
- `GET /sessions` and per-session metadata via list (→ `GET /v1/sessions` and `GET /v1/sessions/{id}`)
- `POST /api/session-titles/set` (**removed** — no auto-title in harness v1)
- `GET /backends/{n}/models` (**removed** — model strings pass through verbatim)
- `GET /events/json` SSE (→ `GET /v1/events` with `?after=<sequence>` reconnect)

### 1.2 Backend client module

`src/mm_bridge/vd_client.py` (224 LOC) is **deleted**. Replaced by
`src/mm_bridge/agent_harness_client.py`. The public method names match the
old client where the semantics are unchanged (`send_message`, `fork_session`,
…) so the call-site rewrite in `bridge.py` is mechanical.

### 1.3 SSE handlers

`_on_vd_event` / `_on_vd_session_added` / `_on_vd_message` /
`_on_vd_session_status` / `_on_vd_summary_updated` in `bridge.py` are renamed
`_on_harness_*` and rewired to the agent-harness event names. `_on_*_summary_updated`
is **deleted** outright (no equivalent harness event).

### 1.4 Config block + env vars

The TOML `[vibedeck]` block becomes `[agent_harness]`. `vd_url` → `agent_harness_url`.
Env-var prefix `VD_*` for backend-related config (`VD_URL`,
`VD_DEFAULT_CWD/BACKEND/MODEL/AUTORESPOND`) is renamed in §5.4. No backwards-compat
shim — operators update their `.env`/TOML once.

## 2. What stays exactly the same

The following are MM-side concerns that don't touch the backend wire and are
left untouched by this work:

- MM WebSocket plumbing and `mm_client.py`.
- All `_on_mm_*` handlers (posted, user_added, channel_updated, channel_created).
- Channel-Purpose parser (`purpose.py`), with the one model-list caveat in §5.3.
- Resume-header writing (`resume_header.py` — see US-4.2 for the small input change).
- Catch-up command, silent-drop replay, tool-use placeholders, attribution.
- Forward-flow attachments (MM upload → file dropped in `cwd/.mattermost-inbox/`).
- `<openFile>` / `<leaveChannel>` directive handling in assistant text.
- Mattermost ↔ session mapping persistence (`ChannelMapping`, sidecar dir).
- Channel-spawn CLI flow (`mm-bridge spawn`).

## 3. What's being deleted (dead-on-arrival)

- **`set_session_title`** call sites — harness has no title-write API.
- **Channel auto-rename** via `session_summary_updated` — harness has no auto-summary.
- **MM-display-name → backend title sync** in `_on_mm_channel_updated`
  (currently calls `self.vd.set_session_title`).
- **`name_sync` debounce machinery** for the title direction (the resume-header
  self-write guard in `_self_written_purpose` is retained — different problem).
- **`model_index` resolution** — every call site that fetches model lists and
  walks them to find an index. Harness `POST /v1/sessions` takes `model` as a
  free string.
- **`VibeDeckClient.list_models` cache + force-refresh wiring**.
- **`canon_backend` helper** outside the client — see §5.3 for the small piece
  that survives in the purpose parser.
- **`pending_mm_sessions` claim-matcher fields** (`cwd`, `backend`,
  `initial_message` used only for the SSE claim). The dataclass survives but
  shrinks to just the message-queueing role (§US-1.5).
- **`pending_forks` list and the `_claim_pending_fork` path entirely** —
  forks are synchronous in agent-harness; the bridge gets the new session id
  from the 201 response.

## 4. Acceptance criteria (must-pass before sign-off)

### US-4.1: Externally-launched sessions auto-spawn a channel

**Given** the live harness has discovered a CLI-launched Claude Code or Codex
session (visible via `GET /v1/sessions` with `origin: external` and an id
the bridge has never seen), **when** the bridge receives the corresponding
`session.updated` SSE event, **then** the bridge creates a fresh MM channel,
links it to the session, and writes the `Resume:` header for the session's
project path.

**Verification:** Start the bridge connected to `pillar:8877`. Confirm a
channel is created for at least one of the 8 currently-observed external
sessions (or kick a new external session and watch).

### US-4.2: Resume header is correct for observed external sessions

**Given** the bridge has linked a channel to an external session, **then** the
channel's `Resume:` header shows the backend's resume command using the
external id verbatim. For Codex sessions the command is
`codex resume <codex_uuid>` (the bare uuid, not the `codex_` prefix — see
design.md for the prefix-strip rule). For Claude Code sessions the command
is `claude --resume <claude_uuid>`.

**Verification:** External-session smoke test on `pillar:8877` (Codex resume
smoke already passed in prior Aster session — re-verify after port).

### US-4.3: Channels still spawn from CLI-launched sessions

**Same as US-4.1** but stated as a negative: regression-guard that the
"externally-discovered → create channel" path is not lost when removing the
old SSE claim-matcher.

### US-4.4: Typing indicator works for external sessions

**Given** a channel mapped to an external session, **when** any session-bound
SSE event arrives (`session.updated`, `message`, `message.delta`, `tool.call`,
`tool.result`), **then** the bridge starts (or refreshes) typing in the MM
channel; **when** no such event arrives for ≥15s, **then** typing stops
without needing a definitive end-of-run event.

**Verification:** Observe a live external session that has assistant activity;
confirm typing indicator pulses in the MM channel for that session and dies
within ~15s of the transcript going quiet. Do NOT rely on
`GET /v1/sessions` `status: running` — that field sticks (per Echo).

### US-4.5: Typing indicator works for harness-owned runs

**Given** a channel mapped to a harness-owned session, **when** the bridge
posts `POST /v1/sessions/{id}/runs` and the server emits `run.started`,
**then** typing starts; **when** `run.completed | run.failed | run.interrupted`
arrives for that run, **then** typing stops promptly (without waiting for the
15s TTL).

### US-4.6: `@claude stop` interrupts harness-owned runs

**Given** a harness-originated run is in flight in a mapped channel, **when**
a user types `@claude stop` (or bare `stop` in autorespond mode), **then** the
bridge issues `DELETE /v1/sessions/{id}/runs/{current_run_id}` and posts a
confirmation message.

**Acceptance:**
- The bridge tracks `current_run_id` per session, updated on each
  `POST /v1/sessions/{id}/runs` response, cleared on the run's terminal SSE event.
- If there is no `current_run_id` for the session (no run in flight), the
  bridge posts `:octagonal_sign: Nothing to stop.` rather than calling DELETE.

### US-4.7: Interrupt on external runs degrades gracefully

**Given** a channel is mapped to an external session and the only in-flight
run is external (no harness-owned `current_run_id` tracked), **when** a user
runs `@claude stop`, **then** the bridge posts a friendly message — e.g.
`:warning: Can't interrupt this run — external session, not owned by the harness.`
— and does NOT call DELETE (or, if it does, treats the resulting `409
external_interrupt_unsupported` as the same friendly-message path).

### US-4.8: Forwarded MM message creates a run

**Given** a channel is mapped to a session (harness or external), **when** a
user posts a message, **then** the bridge issues
`POST /v1/sessions/{id}/runs` with `{message: <body>}` and tracks the
returned `run_id` as `current_run_id` for that session.

**Acceptance:** Body is `{message}` only — no `cwd`/`model`/`backend` in the
request body (harness uses observed `session.project.path` + `session.model`).

### US-4.9: Thread fork creates a session synchronously

**Given** a user starts a thread on a post in a mapped channel, **when** the
bridge calls `POST /v1/sessions/{id}/forks {message}`, **then** the response
arrives with the new session and (optional) first run, and the bridge links
the thread anchor to the new session **before** the next message in that
thread is forwarded.

**Acceptance:**
- No `pending_forks` list.
- If the harness response says fork is not supported for that backend (404 /
  409 from the OpenAPI's documented codes), the bridge posts a dead-thread
  notice the same way it does today.

### US-4.10: Dot-command-driven session restart works

**Given** a channel has a session, **when** the user switches backend / model
via the `.backend <name>` / `.model <name>` dot-commands (the
`_restart_session_with_config` path — message content is no longer parsed as
config), **then** the bridge tears down the current session, calls
`POST /v1/sessions` + `POST /v1/sessions/{id}/runs` with the new config, and
links the channel to the new session id from the 201 response.

### US-4.11: Bridge survives harness restart

**Given** the bridge has live SSE connection to the harness, **when** the
harness restarts, **then** the bridge reconnects using `?after=<last_sequence>`
and replays any retained events with sequence above its last seen value
without re-creating channels for already-mapped sessions.

### US-4.12: Bridge startup reconciles existing mappings

**Given** the bridge restarts with a populated `state.json`, **when** it
starts, **then** it does NOT re-spawn channels for already-mapped sessions
(de-duplicated by checking `mapping.get_anchor(session_id)` before any
`_create_channel_for_session` path).

## 5. Other concrete requirements

### US-5.1: Hard cutover — no VD compatibility shim

The deleted `vd_client.py` is not re-exported, aliased, or stubbed. There is
no env flag to flip between backends. `import vd_client` anywhere outside
deleted files is a build failure after this work.

### US-5.2: Backend wire names use the harness names

The bridge maps purpose tokens to harness backend names: `claude` → `claude-code`,
`codex` → `codex`. The harness's `GET /v1/backends` reports `name` strings
verbatim (`claude-code`, `codex`); the bridge accepts both `claude` (legacy
purpose token) and `claude-code` (display/harness wire) at parse time and
canonicalises internally.

### US-5.3: Model tokens pass through verbatim

The purpose parser's `available_models_for(backend) → list[str]` callback
keeps its signature, but the body is reduced to:

1. Try `GET /v1/backends/{name}/models`. On 200, return `data: list[str]`.
2. On 404 (unknown backend name) or network error, return `[]`.

**Important — empty list semantics**: as of 2026-05-11 the live harness
returns `200 {"data": []}` for both `claude-code` and `codex` because it
has no authoritative model catalog yet (`unknown` backend names still
return 404). The bridge MUST treat an empty `data` list as **"no catalog
available"**, NOT **"no models supported"** — i.e. the parser still
records unknown tokens as `PurposeConfig.model` and passes them verbatim
to `POST /v1/sessions`. The parser MUST accept the raw token as the
model name (best-effort) so the operator can use models the harness
hasn't enumerated.

When the callback returns `[]`, the parser falls through its existing
"could not parse … using defaults" path while preserving the raw model
token. The `test_parse_passes_unknown_model_through` test in §5 below
guards this behaviour against future regression.

### US-5.4: Config + env rename

`Config.vd_url: str = "http://localhost:8765"` →
`Config.agent_harness_url: str = "http://localhost:8877"`.

TOML:
- `[vibedeck] url = "..."` → `[agent_harness] url = "..."`.

Env-var renames (clean break, no fallback):

| Old                              | New                                  |
| -------------------------------- | ------------------------------------ |
| `VD_URL`                         | `AH_URL`                             |
| `VD_DEFAULT_CWD`                 | `MM_BRIDGE_DEFAULT_CWD`              |
| `VD_DEFAULT_BACKEND`             | `MM_BRIDGE_DEFAULT_BACKEND`          |
| `VD_DEFAULT_MODEL`               | `MM_BRIDGE_DEFAULT_MODEL`            |
| `VD_DEFAULT_AUTORESPOND`         | `MM_BRIDGE_DEFAULT_AUTORESPOND`      |

Rationale: the `MM_BRIDGE_*` prefix already covers other bridge-owned settings
(`MM_BRIDGE_STATE`, `MM_BRIDGE_SIDECAR_DIR`, `MM_BRIDGE_DANGEROUS_PERMISSIONS`)
and these defaults aren't tied to a specific backend. `AH_URL` keeps the
URL knob short.

### US-5.5: Health probe is best-effort

Startup health log: try `GET /v1/health` (live as of 2026-05-11, returns
`200 {"status": "ok"}`) and log the status. Never fail startup on a
connection error (matches current VibeDeck behaviour at `bridge.py:285-289`).

If `/v1/health` returns 404 (legacy harness build), fall back to
`GET /v1/sessions` as a connectivity probe. Both forms are best-effort
and log-only.

### US-5.6: SSE reconnect uses `?after=<sequence>`

`AgentHarnessClient.stream_events` tracks the highest `sequence` it has
observed and supplies it as `?after=N` on reconnect. Retention is
implementation-defined per `hybrid-semantics.md`; the bridge accepts gaps
silently and just resumes following.

## 6. Test plan summary (design.md has the detail)

- **Unit tests** for `AgentHarnessClient`: pure shape contracts (request
  bodies, response unwrapping). Fake harness backed by an in-process FastAPI
  app fixture OR `httpx.MockTransport`-driven stubs — design.md picks one.
- **Integration test** against `http://pillar.tail72f2bc.ts.net:8877` —
  smoke that (a) creates a session + run, (b) interrupts it, (c) observes
  the resulting events through the SSE stream. Gated behind an env flag so
  it doesn't run in CI without the tailnet.
- **Bridge-level tests** in `tests/test_bridge.py` already mock the backend
  client. Update mocks to the new client surface and keep existing coverage
  green.
- **Deletions**: every test asserting against `vd_client.canon_backend`,
  `set_session_title`, `model_index`, `session_summary_updated`, or the
  `pending_forks` claim path is removed (not migrated).

## 7. Out of scope

- Adding new bridge features unrelated to the backend swap.
- Changing the Channel Purpose grammar.
- Migrating persisted `state.json` schema (mappings are session-id-string
  agnostic — `claude_<uuid>` and `codex_<uuid>` work as-is).
- Replacing the resume-header formatter (already backend-agnostic via
  `resume_header.normalize_backend`, which already handles `claude-code` →
  canonical `claude`).
- Supporting non-claude/codex backends. The harness exposes `Backend.fork`
  capability per backend; the bridge accepts the existing
  `KNOWN_BACKENDS = {claude, codex, pi, opencode}` set, but only claude /
  codex are tested.
- Implementing `/v1/backends/{name}/models` on the harness side. The bridge
  must handle 404 gracefully (US-5.3); shipping the endpoint is Echo's call.
- Persisting harness session ids across daemon restarts — already covered
  by the existing `ChannelMapping` state.

## 8. Resolved questions

All three open questions from the initial draft have been resolved by Echo
and Aster between 2026-05-11 spec submission and sign-off:

1. **`GET /v1/backends/{name}/models`** (Echo): live on `pillar:8877`,
   returns `200 {"data": []}` for known backends (no catalog yet) and 404
   for unknown names. Spec at US-5.3 covers the empty-list semantics.
2. **External-run interrupt UX** (Aster): friendly-text path with NO DELETE
   attempt when `current_run_id_by_session` is empty. design.md §2.7
   already matches.
3. **Health endpoint** (Echo): `GET /v1/health` live, returns
   `200 {"status": "ok"}`. Spec at US-5.5 uses it as the primary probe.
