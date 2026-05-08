# Channel Header Shows Harness Resume Command — Requirements

## 1. Resume-command formatter

### US-1.1: Backend → command mapping
As a user, I get the right CLI invocation for the backend running in my channel.

**Acceptance Criteria:**
- WHEN backend = `claude` THEN command = `claude --resume <session_id>`
- WHEN backend = `codex` THEN command = `codex resume <session_id>` (or the
  canonical codex CLI form, verified against `codex --help`)
- WHEN backend is anything else (`pi`, `opencode`, unknown) THEN no resume
  line is emitted
- The function is pure: `(backend, session_id, dangerous: bool) → str | None`

### US-1.2: Dangerous-permissions flag
As an operator running with elevated permissions, the command I copy out
matches the permission level of the daemon.

**Acceptance Criteria:**
- WHEN `dangerous = True` AND backend = `claude` THEN the command ends with
  ` --dangerously-skip-permissions`
- WHEN `dangerous = True` AND backend = `codex` THEN the command ends with
  ` --dangerously-bypass-approvals-and-sandbox`
- WHEN `dangerous = False` THEN no flag is appended

### US-1.3: Header line format
The resume command lives on its own line, marked, so it can be parsed and
replaced without clobbering siblings.

**Acceptance Criteria:**
- The emitted header line is: `Resume: <command>`
  (single space, no markdown, no backticks — Mattermost renders headers
  as plain text + auto-links).
- An optional code-fence wrapper is acceptable if it renders better in MM;
  if used, it must be on the same logical line and unambiguous to parse out.
- Implementor picks one form and documents it as a single constant.

## 2. Header write/merge behaviour

### US-2.1: Add to empty header
WHEN a channel has no header AND a session is claimed THEN the header is set
to the resume line (and only the resume line).

### US-2.2: Coexist with parent-header
WHEN a channel already has `Parent: ~name~` (or `Parent: ~name~ ([thread](url))`)
AND a session is claimed THEN both lines coexist, parent line first, resume
line second, separated by `\n`.

### US-2.3: Replace stale resume line
WHEN a channel's header already contains a `Resume:` line AND a new session
is claimed THEN that prior `Resume:` line is replaced (not duplicated).

### US-2.4: Preserve unrelated header content
WHEN a channel header contains operator-set lines (anything that is neither
`Parent:` nor `Resume:`) THEN those lines are preserved; only the `Resume:`
line is added/updated.

### US-2.5: Skip when no command emits
WHEN backend has no resume command (US-1.1 fall-through) THEN the header is
not modified at all (no empty `Resume:` line, no clearing).

## 3. Lifecycle write-points

### US-3.1: On session claim
WHEN `_claim_pending_invite` or `_claim_pending_fork` successfully binds a
session_id to a channel THEN the bridge writes the resume header for that
channel before returning.

**Acceptance Criteria:**
- The header write is best-effort: failure logs a warning and does not
  raise out of the claim path (mirrors existing `set_channel_header`
  failure handling at `cli.py:870`).

### US-3.2: On bridge startup reconcile
WHEN the bridge daemon starts AND the persisted `ChannelMapping` already
binds channels to sessions THEN the bridge sets/refreshes the resume header
for each mapping during startup reconcile.

**Acceptance Criteria:**
- This is a fire-and-forget pass; per-channel failures log and continue.
- Reuses the same merge logic from US-2.x.

### US-3.3: Rebind = overwrite
WHEN a channel that previously had session A is re-bound to session B THEN
the header's `Resume:` line is updated to point at B.

(Covered by US-3.1; explicit so test coverage can assert it.)

## 4. Dangerous-permissions configuration

### US-4.1: Config knob exists
The bridge has exactly one source of truth for dangerous-permissions.

**Acceptance Criteria:**
- IF VibeDeck exposes its `_skip_permissions` state through an existing
  HTTP endpoint or SSE field THEN the bridge reads it from there
  (preferred — no operator config drift).
- ELSE the bridge adds a config key `dangerous_permissions: bool = False`
  to `Config` (`config.py`), readable from TOML and the env var
  `MM_BRIDGE_DANGEROUS_PERMISSIONS` (`true`/`false`/`1`/`0`).
- The decision MUST be documented in `design.md` with a one-paragraph
  justification referencing the VD code paths checked.

### US-4.2: Default = off
WHEN no operator config and no VD signal say otherwise THEN dangerous = False
and the resume command does NOT include the elevated-permission flag.

## 5. Tests

### US-5.1: Unit tests for the formatter
- Cover all (backend, dangerous) combinations in §1.
- Tests live in `tests/test_resume_header.py` (or extend `tests/test_spawn.py`
  if the formatter is colocated there).
- No MM/VD mocks needed — pure-function tests.

### US-5.2: Unit tests for the merge function
- US-2.1 through US-2.5 — empty header, parent-only, parent+stale-resume,
  stale-resume only, with-operator-line, no-resume-emitted.

### US-5.3: Integration tests for claim path
- Existing `tests/test_bridge.py` style: assert that
  `_claim_pending_invite` calls `set_channel_header` with the expected
  merged header, for both backends, both dangerous settings.

### US-5.4: Existing tests pass
- `uv run pytest` exits clean. No tests skipped/xfailed as part of this
  work.

## 6. Out of scope

- VibeDeck-side changes (no new VD endpoints — see Overview "Non-goals").
- Header cleanup when sessions terminate.
- Backends other than claude/codex.
- Refactoring `format_parent_header` (header writes from spawn keep their
  current shape — the merge logic accepts the existing parent line as input).
