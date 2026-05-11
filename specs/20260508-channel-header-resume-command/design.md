# Channel Header Shows Harness Resume Command — Design

## Module layout

Add a single small module:

```
src/mm_bridge/resume_header.py
```

Pure helpers, no imports from `bridge.py` / `mm_client.py` / `vd_client.py`.

```python
# resume_header.py — sketch, implementor refines

RESUME_PREFIX = "Resume: "

def format_resume_command(
    backend: str,
    session_id: str,
    *,
    dangerous: bool,
) -> str | None:
    """Return the bare CLI command, or None for unsupported backends."""

def format_resume_line(
    backend: str, session_id: str, *, dangerous: bool,
) -> str | None:
    """`Resume: <cmd>` or None. Wraps format_resume_command."""

def merge_into_header(existing: str, resume_line: str | None) -> str:
    """Strip any prior `Resume:` line and append the new one.

    - existing == "" and resume_line is not None → resume_line
    - existing has `Parent:` line → keep it, replace/append resume line
    - existing has unrelated lines → keep them all, replace/append resume line
    - resume_line is None → return existing unchanged
    """
```

The merge is line-based when `resume_line` is non-None: split `existing` on
`\n`, drop lines that `startswith(RESUME_PREFIX)`, append the new resume_line,
join with `\n`. Trailing/leading whitespace per line is stripped after the
split to be tolerant of operator edits. When `resume_line` is None, return the
existing header unchanged so unsupported backends satisfy US-2.5.

## Wiring

### `bridge.py` — claim paths

`_claim_pending_invite` (around `bridge.py:1716`) ends with
`self.mapping.link(...)`. After a successful link, before the
follow-up posts:

```python
await self._update_resume_header(channel_id, session_id, pending_backend)
```

**Implementation note — fork claims do NOT update the header.** An
earlier draft of requirements US-3.1 listed `_claim_pending_fork` as a
second write-point. On implementation we found this is wrong: a
thread-fork session lives *inside* a thread, but the Mattermost header
field is a channel-level attribute. Writing the fork session's resume
command into the channel header would clobber the channel-session's
own resume line every time someone started a thread, leaving the
header pointing at a side-branch session rather than the primary
channel session. So the wiring only triggers from `_claim_pending_invite`.
Fork sessions remain addressable via the sidecar / `ig`-style lookups
and don't need their resume command in the parent channel's topbar.
`_reconcile_resume_headers` likewise skips thread anchors
(`anchor.is_thread`).

`_update_resume_header` is a small new method on the bridge:

1. Resolve backend: prefer the value from `pending.purpose_cfg.backend`,
   fall back to the SSE event's `data["backend"]` (already canonicalised
   via `vd_client.canon_backend`).
2. Map canonical name back to the formatter's expected token (`claude` or
   `codex` — `canon_backend` collapses `claude-code` → `claudecode`, so
   either re-canonicalise here or store/pass the original purpose token).
3. `format_resume_line(...)` → `merge_into_header(current_header, line)`
   → `mm.set_channel_header(...)` (best-effort, log+swallow on failure).

The current header comes from a fresh `mm.get_channel(channel_id)` so we
don't lose operator edits between claim events. On failure to fetch,
fall back to the merge against `""` — losing an operator line is worse
than losing a Resume line, so on fetch failure we **skip** instead.

### `bridge.py` — startup reconcile

After `ChannelMapping.load(...)` (the constructor already loads it,
around `bridge.py:226`), add an async startup hook (`run` /
`start` — wherever the daemon's main loop kicks off SSE listening):

```python
async def _reconcile_resume_headers(self) -> None:
    for session_id, anchor in self.mapping.iter_links():
        backend = self._backend_for_channel(anchor.channel_id)
        try:
            await self._update_resume_header(
                anchor.channel_id, session_id, backend,
            )
        except Exception:
            logger.warning("resume-header reconcile failed", exc_info=True)
```

Iteration helper on `ChannelMapping` may not exist yet — add it if
needed (`iter_links() -> Iterable[tuple[session_id, Anchor]]`).

`_backend_for_channel` reads `self.purpose_by_channel`, falls back to
`cfg.vd_default_backend`. If neither yields a known resume backend, the
formatter returns None and the merge strips any existing Resume line.

Reconcile runs once at startup; it does not subscribe to header drift.

## Dangerous-permissions decision

Investigation must be recorded in this design doc as part of the work
(append a "Findings" subsection below). Two paths:

**Path A — VD exposes it.** Search `~/projects/VibeDeck/src/vibedeck/server.py`
for any HTTP route or SSE event field carrying `_skip_permissions`. If a
`/status` / `/config` / `/sessions/<id>` response includes it, add a
small `vd.get_skip_permissions() -> bool` async method to `vd_client.py`
that fetches/caches it. Cache for the daemon's lifetime — VD restart
flips the value, but the bridge restarts alongside in normal ops.

**Path B — Operator config (preferred fallback).** Add to `config.py`:

```python
@dataclass
class Config:
    ...
    dangerous_permissions: bool = False
```

- TOML: `dangerous_permissions = true` under the top-level table
  (consistent with the rest of `Config`).
- Env: `MM_BRIDGE_DANGEROUS_PERMISSIONS` parsed with the existing
  bool-env helper (case-insensitive, accepts `1/true/yes/on`).

The bridge passes `cfg.dangerous_permissions` (or the cached VD value)
into `_update_resume_header`.

### Findings (filled in by implementor)

Path B (operator config) is the implementation path. On 2026-05-08 I
checked VibeDeck's server surface and found `_skip_permissions` is internal
state in `~/projects/VibeDeck/src/vibedeck/server.py` (`set_skip_permissions`
/ `is_skip_permissions`) and is only injected into command builders and
terminal commands. The public `/health` response, `/events` / `/events/json`
session payloads, `/sessions` / `/sessions/{id}/status` routes, and
`SessionInfo.to_dict()` do not expose that value; `routes/sessions.py` reads
`is_skip_permissions()` only while creating/sending/forking sessions. Because
the spec forbids adding a new VibeDeck endpoint for this feature, mm-bridge
uses a bridge-owned `dangerous_permissions` config/env knob instead.

## Failure modes

| Failure                              | Behaviour                                     |
| ------------------------------------ | --------------------------------------------- |
| `mm.get_channel` raises              | Skip header update, log warning               |
| `mm.set_channel_header` raises       | Log warning, claim still succeeds             |
| Backend unknown to formatter         | `format_resume_line` returns None; header update is skipped |
| Reconcile pass partially fails       | Per-channel try/except, continue              |
| Empty `session_id` (defensive)       | Formatter returns None                        |

## Testing

`tests/test_resume_header.py` — pure tests for `format_resume_command`,
`format_resume_line`, and `merge_into_header`. Table-driven; no fixtures
needed.

`tests/test_bridge.py` — extend the existing claim-path tests to assert
`set_channel_header` is called with the merged value. The tests already
mock `MattermostClient` — add `get_channel` returning a header dict and
assert against `set_channel_header.call_args`.

No integration test against a live MM is added — the merge logic is the
risky bit and is fully covered by unit tests.

## Migration / rollout

- No data migration. First daemon start after merge backfills headers
  via the reconcile pass.
- If operator wants to opt out: leave `dangerous_permissions` at default
  `False` and accept that channels show the non-elevated form, OR
  manually clear the `Resume:` line from a header (next claim will
  rewrite it — that's acceptable).
