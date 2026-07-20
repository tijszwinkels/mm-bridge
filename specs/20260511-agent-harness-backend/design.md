# mm-bridge → agent-harness Backend Port — Design

## Module layout

| File                                    | Action                                                                      |
| --------------------------------------- | --------------------------------------------------------------------------- |
| `src/mm_bridge/vd_client.py`            | **DELETE**                                                                  |
| `src/mm_bridge/agent_harness_client.py` | **NEW** — public surface in §1                                              |
| `src/mm_bridge/bridge.py`               | **EDIT** — rename handlers, rewire call sites, add typing-pulse state (§2)  |
| `src/mm_bridge/config.py`               | **EDIT** — rename block, fields, env vars (§3)                              |
| `src/mm_bridge/purpose.py`              | **NO CHANGE** to the public parse signature; one caller-side tweak (§5)     |
| `src/mm_bridge/resume_header.py`        | **NO CHANGE** — `normalize_backend` already handles `claude-code`           |
| `tests/test_vd_client.py`               | **DELETE** if present (replaced by `tests/test_agent_harness_client.py`)    |
| `tests/test_bridge.py`                  | **EDIT** — swap mock surface; drop deleted-flow assertions                  |
| `tests/test_agent_harness_client.py`    | **NEW** — see §6                                                            |

## 1. `agent_harness_client.py` public surface

Async client; mirrors `httpx.AsyncClient` style of the deleted module. Public
method list and the OpenAPI endpoint each one hits:

```python
class AgentHarnessClient:
    def __init__(self, base_url: str) -> None: ...
    async def close(self) -> None: ...

    # ── Session lifecycle ───────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        backend: str,            # purpose token; mapped to harness wire name internally
        model: str | None,       # passed verbatim; None drops the field
        cwd: str,
        title: str | None = None,
    ) -> dict:
        """POST /v1/sessions
        Body: {"backend": <wire_name>, "model": <model>, "project": {"path": cwd, "name": Path(cwd).name}, "title": title?}
        Returns the full Session dict from the 201 response.
        Raises httpx.HTTPStatusError on 4xx/5xx (with `error.detail` surfaced).
        """

    async def create_run(self, session_id: str, message: str) -> dict:
        """POST /v1/sessions/{id}/runs
        Body: {"message": message}  — NEVER cwd/model/backend.
        Returns {"session_id", "run_id"} from the 202 response.
        Maps 409 (resume unsupported on this external session) to a
        well-typed exception so the bridge can surface a friendly message.
        """

    async def fork_session(
        self, session_id: str, *, message: str | None,
        title: str | None = None,
    ) -> dict:
        """POST /v1/sessions/{id}/forks
        Body: {"message": message?, "title": title?}
        Returns {"session": Session, "run": Run | None} from the 201 response.
        Synchronous — caller gets the new session id immediately.
        404/409 raise typed exceptions so bridge.py can dead-thread.
        """

    async def interrupt_run(self, session_id: str, run_id: str) -> dict:
        """DELETE /v1/sessions/{id}/runs/{run_id}
        Returns the terminal Run dict on 200.
        409 (external_interrupt_unsupported) → typed exception.
        404 (run already terminal / unknown) → typed exception; caller swallows.
        """

    async def get_session(self, session_id: str) -> dict | None:
        """GET /v1/sessions/{id}
        Returns the Session dict, or None on 404.
        """

    async def list_sessions(self) -> list[dict]:
        """GET /v1/sessions → response.json()["data"]"""

    async def list_backend_models(self, backend: str) -> list[str]:
        """GET /v1/backends/{name}/models
        Returns the model-name list, or [] on 404 / network error.
        Wire-name aware: caller passes purpose token, client maps to wire.
        """

    async def list_session_messages(self, session_id: str) -> list[dict]:
        """GET /v1/sessions/{id}/messages → response.json()["data"]
        Not used by the bridge today; included so tests have a probe.
        """

    async def health(self) -> dict:
        """GET /v1/health → {"status": "ok"} on a healthy harness.
        Raises on 4xx/5xx so callers can fall back to list_sessions as a
        legacy-build compatibility probe (see US-5.5).
        """

    # ── SSE ─────────────────────────────────────────────────────────────────

    async def stream_events(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        after_sequence: int | None = None,
    ) -> None:
        """GET /v1/events (with ?after=<seq> on reconnect).
        Loops forever; reconnects on disconnect; tracks the highest sequence
        observed and passes it as ?after on every reconnect attempt.
        on_event is called with (event_name, data_dict) — the data dict is the
        SSE `data:` JSON parsed as-is (the harness wraps payloads inside
        `data.data`; the client does NOT unwrap — handlers do).
        """
```

### Run-id tracking (split responsibility)

Run-id tracking lives in `bridge.py`, not in the client:

```python
# Bridge state, added near self.tool_use_runs / self.posters
self.current_run_id_by_session: dict[str, str] = {}
```

Set on the `create_run` / `fork_session` return path; cleared on
`run.completed | run.failed | run.interrupted` whose payload's `run_id`
matches. Reasoning: a synchronous DELETE is the only consumer; pushing it
into the client adds shared mutable state to a thin HTTP shim.

### Backend wire-name aliasing

The client owns one tiny helper:

```python
_BACKEND_WIRE: dict[str, str] = {"claude": "claude-code"}

def _wire(name: str) -> str:
    return _BACKEND_WIRE.get(name.lower(), name)
```

Used in `create_session.project.backend` and `list_backend_models`. All
other endpoints take the session id directly (which is already
backend-prefixed), so no aliasing needed.

The existing `canon_backend` collapsing function lives on inside
`resume_header.normalize_backend` — see §5.

### Error mapping

Translate the harness error envelope to typed exceptions so call sites can
branch without parsing `detail` strings:

```python
class HarnessError(Exception): ...
class HarnessSessionNotFound(HarnessError): ...      # 404 on a {id} route
class HarnessRunNotFound(HarnessError): ...          # 404 on a run route
class HarnessResumeUnsupported(HarnessError): ...    # 409 from create_run on external
class HarnessInterruptUnsupported(HarnessError): ... # 409 from interrupt_run on external
class HarnessForkUnsupported(HarnessError): ...      # 409 / 404 from forks on backend that lacks fork
```

The mapper inspects `response.json()["error"]["code"]` when present; falls
back to status-code-only matching when `error.code` is missing. Bridge call
sites catch the typed exceptions and post the friendly messages described
in requirements §4.

## 2. `bridge.py` change list

### 2.1 Identifier renames

| Old                                 | New                                          |
| ----------------------------------- | -------------------------------------------- |
| `self.vd: VibeDeckClient`           | `self.harness: AgentHarnessClient`           |
| `_run_vd_listener`                  | `_run_harness_listener`                      |
| `_on_vd_event`                      | `_on_harness_event`                          |
| `_on_vd_session_added`              | `_on_harness_session_seen` (note rename)     |
| `_on_vd_message`                    | `_on_harness_message`                        |
| `_on_vd_session_status`             | `_on_harness_run_lifecycle`                  |
| `_on_vd_summary_updated`            | **DELETED**                                  |
| `last_status_ts` dict               | `last_activity_ts` dict (semantic broadens)  |
| `pending_mm_sessions` (claim role)  | `warming_up_sessions` (queue-only role)      |
| `pending_forks`                     | **DELETED** entirely                         |

### 2.2 Event translation table — exhaustive

For every harness SSE event the bridge consumes, what it does:

| Harness event           | `data` shape (relevant fields)                                                            | Bridge action                                                                                                                                                                                                                                  |
| ----------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session.updated`       | `{session: Session, run_id, session_id, created_at, backend, origin, transcript_path?}`   | (a) Update last-activity TTL for `session_id`. (b) If `session_id` is unknown to the bridge (not in `_known_sessions` AND not in mapping), treat as **session-seen**: run `_create_channel_for_session(data.session)`. Add id to `_known_sessions`. |
| `message`               | `{message: Message, session_id, run_id?, backend, origin}`                                | (a) Update last-activity TTL. (b) Dispatch to `_on_harness_message(session_id, message_dict)` — same downstream code path as today's `_on_vd_message`. The `message.blocks` shape is **identical** to VD (`text` / `tool_use` / `tool_result` / `thinking` / `image`). |
| `message.delta`         | `{message_id, delta: {text?, …}, session_id, run_id}`                                     | Update last-activity TTL only. Bridge does not surface partials in MM today (matches current behaviour).                                                                                                                                       |
| `tool.call`             | `{tool_use_id, name, input, session_id, run_id}`                                          | Update last-activity TTL. The tool-use UI is already driven from `message` blocks; this event is informational here.                                                                                                                            |
| `tool.result`           | `{tool_use_id, content, is_error, session_id, run_id}`                                    | Update last-activity TTL. Same rationale as `tool.call`.                                                                                                                                                                                       |
| `run.started`           | `{run_id, session_id, started_at}`                                                        | Update last-activity TTL. Start typing immediately if session is mapped (don't wait for the next message).                                                                                                                                     |
| `run.completed`         | `{run_id, session_id, stop_reason}`                                                       | Clear `current_run_id_by_session[session_id]` iff it matches `run_id`. Stop typing. Run `_mention_triggerer_on_done(session_id)` (mirrors current `_on_vd_session_status` `running=False`).                                                     |
| `run.failed`            | `{run_id, session_id, error?}`                                                            | Same as `run.completed`. Optionally post an error notice (out of scope for v1 — current behaviour swallows silently).                                                                                                                          |
| `run.interrupted`       | `{run_id, session_id}`                                                                    | Same as `run.completed`.                                                                                                                                                                                                                      |
| `permission.denied`     | `{tool_use_id, session_id, run_id, reason}`                                               | Update last-activity TTL. No MM surfacing in v1 (mirrors current behaviour).                                                                                                                                                                  |
| `ping`                  | `{}`                                                                                      | No-op. Used by harness as keep-alive on the SSE stream; bridge silently drops.                                                                                                                                                                |

**Important shape note**: the harness SSE wraps the bus event in an outer
`data` object whose own `data` field carries the bus payload (visible in
the live sample at §"Live shapes confirmed"). The dispatch loop must
respect that, e.g.:

```python
# Inside stream_events, after json.loads(data_buf):
sequence = parsed.get("sequence")
event_name = parsed.get("event")
inner = parsed.get("data", {})
session_id = inner.get("session_id") or (inner.get("session") or {}).get("id")
```

The handlers receive `(event_name, parsed)` and unwrap as needed.

### 2.3 Typing-indicator state machine (per session)

```
last_activity_ts: dict[session_id, float]       # monotonic time of latest event
typing.running_sessions(): set[session_id]      # already tracked
TTL = 15.0                                       # locked by Echo
WATCHDOG_INTERVAL = 5.0                          # check frequency

On any session-bound event arriving in _on_harness_event:
    last_activity_ts[sid] = time.monotonic()
    if event in {run.started, message, message.delta, session.updated,
                 tool.call, tool.result, permission.denied}:
        await typing.start(sid, anchor.channel_id, anchor.root_id)

On run.completed | run.failed | run.interrupted:
    last_activity_ts.pop(sid, None)
    await typing.stop(sid)

Watchdog (every WATCHDOG_INTERVAL):
    for sid in list(typing.running_sessions()):
        last = last_activity_ts.get(sid)
        if last is None or now - last > TTL:
            await typing.stop(sid)
            last_activity_ts.pop(sid, None)
```

The state machine starts typing eagerly (any session-bound activity → typing
on) and stops it on either definitive end (`run.completed|failed|interrupted`)
or 15s of silence. This handles externals — where we may never see a
definitive end — and harness-owned runs equally.

The existing `_run_typing_watchdog` loop is kept; only the timeout check
swaps from `last_status_ts` to `last_activity_ts` and the constant moves
from `config.typing_stop_after_silence_seconds` to a module-level `TTL = 15.0`
(or stays in config — see §3.2).

### 2.4 `session_added` synthesis from `session.updated`

Replaces the explicit `session_added` SSE in VibeDeck. Algorithm:

```python
# Bridge field, populated at startup from GET /v1/sessions
self._known_sessions: set[str] = set()

async def _bootstrap_known_sessions(self) -> None:
    try:
        sessions = await self.harness.list_sessions()
    except Exception:
        logger.warning("Bootstrap GET /v1/sessions failed — falling back to mapping", exc_info=True)
        sessions = []
    self._known_sessions = {s["id"] for s in sessions}
    # Also seed from the persisted mapping so a stale-list harness doesn't
    # cause re-spawn loops:
    self._known_sessions.update(self.mapping.session_to_anchor.keys())

async def _on_harness_session_seen(self, inner: dict) -> None:
    session = inner.get("session") or {}
    session_id = session.get("id") or inner.get("session_id")
    if not session_id:
        return
    if self.mapping.get_anchor(session_id):
        self._known_sessions.add(session_id)
        return
    if session_id in self._known_sessions:
        return  # benign re-emit
    self._known_sessions.add(session_id)
    await self._create_channel_for_session(session)
```

Order matters: `_bootstrap_known_sessions` runs **before** the SSE listener
starts in `Bridge.start`, so race-conditions (a session_seen event arriving
mid-bootstrap) don't double-create channels.

### 2.5 Invite + fork are now synchronous — kill the claim dance

Old invite flow (`_start_invited_session`):
1. Build `PendingMattermostSession` (channel, cwd, backend, initial msg).
2. Call `vd.create_session(...)` → fire-and-forget; VD will emit `session_added`.
3. `_claim_pending_invite` matches the SSE event against the pending invite
   by `(cwd, backend, firstMessage prefix)`. On match → link channel ↔ session.

New invite flow:
1. Call `harness.create_session(backend, model, cwd)` → **201 response gives us the session id directly**.
2. `self.mapping.link(Anchor(channel_id), session.id)`.
3. Set `self._known_sessions.add(session.id)` so the subsequent
   `session.updated` SSE for our own id is treated as benign.
4. Call `harness.create_run(session.id, message=initial_message)` →
   **202 gives us the run id**.
5. `self.current_run_id_by_session[session.id] = run_id`.
6. Post welcome / resume-header / etc. (existing logic).

Old fork flow (`_handle_thread_post` → `vd.fork_session` + pending_forks):
1. Call `vd.fork_session(parent, message)`. VD emits `session_added` later.
2. `_claim_pending_fork` FIFO-claims by cwd.

New fork flow:
1. Call `harness.fork_session(parent_id, message=fork_message)` → 201 with
   `{session, run?}`.
2. `self.mapping.link(Anchor(channel_id, root_id), session.id)`.
3. If `run` is present, `current_run_id_by_session[session.id] = run.id`.
4. Post the fork-disclaimer notice.

**Result**: `pending_forks` is deleted. `pending_mm_sessions` shrinks to a
**warming-up dict** with just `queued_messages` and `requested_at` for the
brief HTTP-round-trip window between user-invite and session-link. See §2.6.

### 2.6 Warming-up dict (replacement for the claim-role `pending_mm_sessions`)

Two writes can race in the brief window between "user invites bot" and
"channel linked to session" — e.g. a quick follow-up MM message arrives
before `harness.create_session` returns. Keep the message-queueing role:

```python
@dataclass
class WarmingUpChannel:
    channel_id: str
    queued_messages: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

self.warming_up_sessions: dict[str, WarmingUpChannel] = {}  # keyed by channel_id

# In _on_mm_posted, before "no session" branch:
session_id = self.mapping.get_session(Anchor(channel_id))
if not session_id:
    warming = self.warming_up_sessions.get(channel_id)
    if warming:
        warming.queued_messages.append(message)
        return
    # … existing engagement / no-op paths

# In _start_invited_session, around the harness.create_session call:
self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)
try:
    session = await self.harness.create_session(...)
    self.mapping.link(Anchor(channel_id), session["id"])
    await self.harness.create_run(session["id"], message=initial)
finally:
    queued = self.warming_up_sessions.pop(channel_id, None)
    if queued and queued.queued_messages:
        await self._flush_queued(channel_id, session["id"], queued.queued_messages)
```

Implementor note: hold the warming-up entry **before** `await create_session`,
clear it **after** link + run start, then flush. The current
`_flush_queued` helper survives as-is; only its argument type changes (list
of strings instead of the old dataclass).

### 2.7 Interrupt path

```python
async def _run_stop_command(self, channel_id, session_id, thread_root):
    run_id = self.current_run_id_by_session.get(session_id)
    if not run_id:
        self.mm.post(channel_id, ":octagonal_sign: Nothing to stop.", root_id=thread_root)
        return
    try:
        await self.harness.interrupt_run(session_id, run_id)
    except HarnessInterruptUnsupported:
        self.mm.post(
            channel_id,
            ":warning: Can't interrupt this run — external session, not owned by the harness.",
            root_id=thread_root,
        )
        return
    except HarnessRunNotFound:
        # Run already terminal; treat as success (it's stopped, after all).
        pass
    except Exception:
        logger.exception("interrupt_run failed for %s/%s", session_id[:8], run_id[:8])
        self.mm.post(channel_id, ":warning: Couldn't interrupt the run.", root_id=thread_root)
        return
    self._end_tool_use_run(session_id)
    if self.typing:
        await self.typing.stop(session_id)
    self.mm.post(channel_id, ":octagonal_sign: Stopped.", root_id=thread_root)
```

`current_run_id_by_session[session_id]` is cleared by the
`run.completed|failed|interrupted` SSE event when it eventually arrives — no
need to clear here in the success path, the event will do it.

### 2.8 `_create_channel_for_session` updates

Today reads `data["projectPath"]`, `data["backend"]`,
`data["summaryTitle"] / projectName / project / session_id[:12]` for display
name. New shape from `session.updated.data.session`:

| New | Old field | Use |
| --- | --- | --- |
| `session.project.path` | `data.projectPath` | Resume header cwd, channel purpose stamp |
| `session.project.name` | `data.projectName` | Channel display name fallback (no summaryTitle in harness) |
| `session.backend` | `data.backend` | Resume header backend |
| `session.id` | `data.id` / `data.session_id` | Mapping link, channel name `s-<short>` |

`session.title` (string-or-null) is also available; prefer it when present
for the channel display name, fall back to `session.project.name` then
short id.

### 2.9 `_resume_meta_for` updates

```python
async def _resume_meta_for(self, channel_id, session_id):
    meta = await self.harness.get_session(session_id) or {}
    backend = meta.get("backend")            # `claude-code` / `codex` — resume_header normalises
    cwd = (meta.get("project") or {}).get("path")
    if not backend:
        backend = self._backend_for_channel(channel_id)
    return backend, cwd
```

`resume_header.normalize_backend` already collapses `claude-code` → `claude`
(via the existing `canon_backend` chain). No change needed in
`resume_header.py`.

**Important — Codex resume id format.** The codex CLI's `resume` subcommand
takes the bare rollout UUID, not the `codex_<uuid>` prefix the harness uses
externally. `resume_header.format_resume_block` must strip the `codex_` /
`claude_` prefix before substituting into the command template. Current
code uses `session_id` verbatim; the implementor adds a `_strip_external_prefix`
helper. (Confirmed against Echo's `hybrid-semantics.md` §"Resume And
Adoption" — the harness launches the backend with the raw uuid.)

### 2.10 MM display-name → backend title sync (DELETE)

The branch at `bridge.py:648-660` that calls `self.vd.set_session_title(...)`
on MM `display_name` changes is **removed entirely**. The harness has no
title-write API. Channel renames in MM stay local to MM; sessions keep
whatever title the harness assigned at create time.

The accompanying `name_sync.NameSync` debounce object stays in
`self.name_sync` for use by `_self_written_purpose` writes (different
problem). The single call site `self.name_sync.should_sync("mm", channel_id)`
and `self.name_sync.note_remote_update("vd", session_id)` go away.

## 3. `config.py` change

### 3.1 New `[agent_harness]` block

```python
@dataclass
class Config:
    ...
    # Replaces vd_url
    agent_harness_url: str = "http://localhost:8877"
    ...
```

TOML:
```toml
[agent_harness]
url = "http://harness.example.com:8877"
```

`_apply_toml`:
```python
ah = data.get("agent_harness", {}) or {}
if "url" in ah:
    self.agent_harness_url = ah["url"]
```

### 3.2 Env-var renames (clean break, no fallback shim)

```python
# In _apply_env:
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
```

All `VD_*` env reads at `config.py:225-236` are **removed**. Operators
update their `.env` once on the cutover.

### 3.3 `typing_stop_after_silence_seconds` becomes the activity TTL

The existing config field is repurposed (default already `10.0`s; raise to
`15.0`s to match Echo's spec). Stays a config knob for tuneability.
Rename is optional — `typing_stop_after_silence_seconds = 15.0` is still
accurate.

### 3.4 Defaults change

| Field | Old | New |
| --- | --- | --- |
| `vd_url` → `agent_harness_url` | `http://localhost:8765` | `http://localhost:8877` |
| `typing_stop_after_silence_seconds` | `10.0` | `15.0` |

## 4. Deletions list — explicit checklist

Belt-and-braces list so the implementor can grep and verify nothing dangling:

1. `src/mm_bridge/vd_client.py` (whole file).
2. `from .vd_client import VibeDeckClient` import and the `vd_client` package
   re-import at `bridge.py:13` and `:17`.
3. `bridge.py:234` — `self.vd = VibeDeckClient(config.vd_url)` → replace.
4. `bridge.py:286` — `self.vd.health()` call → replace with
   `await self.harness.health()` (a thin `GET /v1/health` wrapper —
   live since 2026-05-11; returns `{"status": "ok"}`). On 404 (legacy
   harness build), fall back to `await self.harness.list_sessions()` as
   a connectivity probe. Both forms are best-effort and log-only per US-5.5.
5. `bridge.py:651` and `bridge.py:1948` — both `self.vd.set_session_title`
   calls **DELETE**.
6. `bridge.py:702`, `:729`, `:866`, `:985` — every `self.vd.list_models(...)`
   call replaced by `self.harness.list_backend_models(...)` (US-5.3
   tolerates `[]` returns).
7. `bridge.py:724-741` — the `model_index` resolution block in
   `_start_invited_session` is **DELETED**. The body of the harness call
   no longer accepts a `model_index`.
8. `bridge.py:766-770` — `vd.create_session(message=..., cwd=..., backend=...,
   model_index=...)` becomes a two-step
   `harness.create_session(backend=, model=, cwd=)` + `harness.create_run(
   session_id, message=...)`.
9. `bridge.py:946` — the entire `_resolve_model_index` helper is **DELETED**.
10. `bridge.py:959-980` — the `_restart_session_with_config` body is rewritten
    along the same lines as item 8 (two-step create).
11. `bridge.py:1290-1330` — `vd.get_session_meta` + `vd.fork_session` +
    `pending_forks.append` collapses to a synchronous fork:
    `resp = await self.harness.fork_session(parent_session, message=...)`,
    then immediate `mapping.link`. No `pending_forks` field.
12. `bridge.py:1584` — `vd.send_message(session_id, block)` (catch-up path)
    → `harness.create_run(session_id, message=block)`.
13. `bridge.py:1652` — `vd.interrupt_session(session_id)` → see §2.7.
14. `bridge.py:1707-1715` — `_on_vd_event` dispatch table rewritten for new
    event names; `_on_vd_summary_updated` entry deleted.
15. `bridge.py:1712-1713` — the entire `_on_vd_summary_updated` method (and
    the dispatcher branch that calls it) is **DELETED**.
16. `bridge.py:1899-1960` — `_claim_pending_invite` collapses to the
    synchronous link in §2.5. The function survives as an empty
    no-op (or is folded into `_start_invited_session`) — implementor's
    call. The `vd_client.canon_backend` references inside go away.
17. `bridge.py:1962-2017` — `_claim_pending_fork` is **DELETED** (no callers).
18. `bridge.py:2398-2419` — the `# ----- VibeDeck session_summary_updated`
    section, including `_on_vd_summary_updated`, is **DELETED**.
19. `bridge.py:2423-2444` — `_on_vd_session_status` body replaced by
    `_on_harness_run_lifecycle` per §2.2 + §2.3.
20. `tests/test_vd_client.py` (if present) — **DELETE**.
21. `tests/test_bridge.py` — every test referencing `session_summary_updated`,
    `set_session_title`, `model_index`, `pending_forks`, or the SSE-claim
    matcher path is **DELETED** (not migrated).
22. `Config.vd_url` field — replaced by `agent_harness_url` (§3.1).

## 5. `purpose.py` caller-side tweak (no public-signature change)

`purpose.parse` still takes `available_models_for: Callable[[str], list[str]]`.
The callable's implementation moves from `self.vd.list_models` to
`self.harness.list_backend_models` (same return shape). The parser already
tolerates `[]` returns — no parser change.

One sanity-test the implementor should add to `test_purpose.py`:

```python
def test_parse_passes_unknown_model_through():
    """When available_models_for returns [], an unknown model token is still
    recorded as model so the bridge can pass it to harness.create_session
    verbatim. This makes new model releases work without bridge edits."""
```

(Verify in the current parser body that this is already true; if it's not,
add the small acceptance change here.)

## 6. Test plan

### 6.1 Fake harness for unit tests — recommendation: `httpx.MockTransport`

Two viable options:

| Option | Pros | Cons |
| --- | --- | --- |
| **A. `httpx.MockTransport`** | Pure-Python, no asyncio server, fastest tests, no port conflicts in CI, matches the request/response shape directly | Mock SSE streams need a manual `aiter_bytes` generator — slightly fiddlier than a real server |
| **B. FastAPI test app + uvicorn fixture** | Real HTTP stack, real SSE — closest to production | Adds a fixture lifecycle, port allocation, async-loop interleaving with pytest-asyncio |

**Recommendation: Option A** for `tests/test_agent_harness_client.py`.

Sketch:

```python
import httpx
from mm_bridge.agent_harness_client import AgentHarnessClient

def _transport(handler):
    return httpx.MockTransport(handler)

async def test_create_session_request_shape():
    seen = {}
    async def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={
            "id": "test_abc", "backend": "claude-code", "model": "opus",
            "project": {"path": "/x", "name": "x"}, "title": None,
            "created_at": "...", "updated_at": "...", "status": "idle",
            "origin": "harness", "stats": {...},
        })
    client = AgentHarnessClient.with_transport("http://h", _transport(handler))
    sess = await client.create_session(backend="claude", model="opus", cwd="/x")
    assert seen["url"].endswith("/v1/sessions")
    assert seen["body"] == {"backend": "claude-code", "model": "opus",
                            "project": {"path": "/x", "name": "x"}}
    assert sess["id"] == "test_abc"
```

`AgentHarnessClient.__init__` needs a tiny seam — accept an optional
`transport: httpx.AsyncBaseTransport` for testing:

```python
def __init__(self, base_url: str, *, _transport=None):
    self._http = httpx.AsyncClient(base_url=base_url, timeout=30, transport=_transport)
```

Add `with_transport` classmethod for ergonomic test construction.

#### Cases to cover (table-driven where possible)

- `create_session`: project.path / project.name derivation, backend aliasing
  (`claude` → `claude-code`), 400 surfacing.
- `create_run`: body shape (no cwd/model/backend), 202 unwrap, 409 →
  `HarnessResumeUnsupported`.
- `fork_session`: 201 unwrap (session + optional run), 409 →
  `HarnessForkUnsupported`.
- `interrupt_run`: 200 unwrap, 409 → `HarnessInterruptUnsupported`, 404 →
  `HarnessRunNotFound`.
- `get_session`: 200, 404 → None.
- `list_sessions`: `data` array unwrap.
- `list_backend_models`: 200 unwrap, 404 → `[]`.
- `stream_events`: feed a static byte stream through the mock transport,
  assert events are dispatched in sequence order and `?after=` is set on
  reconnect.

### 6.2 Live integration test (gated)

`tests/test_agent_harness_integration.py`, marked
`@pytest.mark.skipif(not os.environ.get("HARNESS_LIVE_URL"), reason="…")`:

1. Connect to `os.environ["HARNESS_LIVE_URL"]` (e.g.
   `http://harness.example.com:8877`).
2. `GET /v1/sessions` — assert ≥1 external session visible.
3. `POST /v1/sessions` to create a harness-origin session with a temp cwd.
4. `POST /v1/sessions/{id}/runs` with a short prompt.
5. Subscribe to `/v1/sessions/{id}/events` and assert `run.started` arrives
   within 10s.
6. `DELETE /v1/sessions/{id}/runs/{run_id}` — assert 200.
7. Assert a subsequent `run.interrupted` event arrives within 5s.
8. `DELETE /v1/sessions/{id}` to clean up.

Not run in CI; documented in README as the smoke before a release cut.

### 6.3 Bridge-level tests

`tests/test_bridge.py` already mocks the backend client. The migration is:

- Replace `MagicMock(spec=VibeDeckClient)` with `MagicMock(spec=AgentHarnessClient)`.
- Update return values of `create_session` to the new dict shape (`{"id": ..., "project": {...}, ...}`).
- For every test that used to call `vd.fork_session` and then synthesise a
  `session_added` event, the new path returns the session id immediately —
  collapse the test to assert on `mapping.link` being called with the new id.
- Delete: tests for `_claim_pending_fork`, `_on_vd_summary_updated`,
  `set_session_title` direction sync, `model_index` resolution.
- Add: a test that on `session.updated` for an unknown id, the bridge calls
  `_create_channel_for_session` exactly once even if the event re-arrives
  (idempotency via `_known_sessions`).
- Add: a test that on `run.completed`, `current_run_id_by_session` is
  cleared.
- Add: a test that activity-pulse TTL stops typing after 15s of silence in
  the absence of a run-lifecycle event.

### 6.4 Live shapes confirmed (one-time)

Confirmed against `harness.example.com:8877` on 2026-05-11:

```
GET /v1/sessions
{"data": [{"id": "claude_<uuid>" | "codex_<uuid>",
           "backend": "claude-code" | "codex",
           "model": "<full_id>",                    # e.g. "claude-opus-4-7"
           "project": {"path": "...", "name": "..."},
           "title": null,
           "status": "running" | "idle" | "waiting_for_input" | "archived",
           "origin": "external" | "harness",
           "stats": {"messages": int, ...}},
          ...]}

GET /v1/backends
[{"name": "claude-code", "display_name": "Claude Code",
  "capabilities": {"interrupt_external_runs": false, "fork": true, ...}},
 {"name": "codex", "display_name": "Codex",
  "capabilities": {"interrupt_external_runs": false, "fork": true, ...}}]

GET /v1/events?from=beginning
id: 1
event: session.updated
data: {"sequence": 1, "event": "session.updated",
       "data": {"backend": "codex", "origin": "external",
                "transcript_path": "...", "offset": 48151,
                "session": {<Session>}},
       "run_id": null,
       "session_id": "codex_<uuid>",
       "created_at": "..."}

GET /v1/backends/claude-code/models → 404 (not yet implemented)
GET /v1/backends/codex/models       → 404 (not yet implemented)
```

The `/v1/backends/{name}/models` 404 is the only divergence from the spec —
handled by US-5.3.

## 7. Failure modes

| Failure                                                  | Behaviour                                                                                                                       |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `harness.create_session` raises                          | `_start_invited_session` posts `:warning: Failed to start an agent-harness session.`, clears warming-up entry. Mirrors current. |
| `harness.create_run` raises after a successful create    | Post warning, session stays linked, current_run_id_by_session left empty so a follow-up post can retry.                         |
| `harness.fork_session` raises                            | `_mark_dead_thread` with reason from the typed exception or generic message.                                                    |
| `harness.interrupt_run` 409 `external_interrupt_unsupported` | Friendly post per §2.7. No retry.                                                                                          |
| `harness.interrupt_run` 404 (run already terminal)       | Treat as success — post `Stopped.` since the user's intent was satisfied.                                                       |
| SSE disconnect                                           | `stream_events` retry loop reconnects after 2s with `?after=<last_sequence>` (matches current VD client).                       |
| SSE event with unknown `event` name                      | Logged at DEBUG, dropped. (Forward-compatible — future events don't crash.)                                                     |
| `GET /v1/sessions` at startup fails                      | Log warning, continue with empty `_known_sessions`; rely on persisted mapping to dedup. Worst case: a few duplicate channels for sessions seen for the first time. |
| `current_run_id_by_session` mismatch on `run.completed`  | Don't clear; log at DEBUG. Stops typing regardless.                                                                             |

## 8. Migration / rollout

1. **Bridge restart on cutover.** No data migration. The persisted
   `state.json` uses session id strings — `claude_<uuid>` and `codex_<uuid>`
   both work as-is. Channels mapped to legacy `vd_*` ids (if any exist in
   testing state) become orphans; operator can `@claude leave` to clean up.
2. **Env update**. Operators update `.env` per §3.2 once. Bridge logs at
   startup which env vars it consumed, so missed renames surface quickly.
3. **Resume header backfill**. The existing `_reconcile_resume_purposes`
   startup pass already re-writes resume blocks on boot — it pulls the
   cwd/backend from the (new) harness client. First boot after cutover
   re-stamps every channel's `Resume:` header with the correct command for
   the new client.
4. **No feature flag.** Hard cutover per Aster's directive.

## 9. Sequencing for the implementor

Suggested order so each commit can be reviewed standalone:

1. **`agent_harness_client.py` + tests** (no bridge wiring yet) — pure
   addition, can land green.
2. **`config.py` rename** — small, mechanical, gated by env-update doc.
3. **`bridge.py` swap + handler rename**, one logical chunk:
   - Replace `self.vd` field, rewire every call site by mechanical rename
     (use the deletions checklist in §4 as the punch list).
   - Land the synchronous invite/fork flow (§2.5).
   - Delete `_on_vd_summary_updated`, `_claim_pending_fork`,
     `_resolve_model_index`, and the title-sync branch in
     `_on_mm_channel_updated`.
4. **Activity-pulse typing-indicator state machine** (§2.3).
5. **`vd_client.py` + dead tests deletion**.
6. **Live integration test** (gated; documented for release ritual).

Each step yields a runnable bridge that talks to the live harness for the
flows it covers, so smoke testing can ride along with the work.
