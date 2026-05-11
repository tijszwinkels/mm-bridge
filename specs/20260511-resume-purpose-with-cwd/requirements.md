# Resume Block in Channel Purpose — Requirements

## 1. Purpose section layout

### US-1.1: Stable section separator
- `purpose.SECTION_SEPARATOR == "---"`.
- A "standalone separator" is a line whose stripped value equals
  `SECTION_SEPARATOR`. Lines containing `---` as part of longer text
  (e.g. `--- not really ---`) are NOT separators.

### US-1.2: `parse()` ignores the trailing section
- WHEN Purpose contains a standalone separator THEN `parse()` only
  tokenises text above the first such line.
- WHEN the trailing section contains arbitrary text (code fences, prose)
  THEN no warnings are emitted for that text.

### US-1.3: Round-trip helpers
- `split_config_section(text) → (config, rest)` — both halves separator-free.
- `join_sections(config, rest) → text` — omits the separator when either
  side is empty; emits blank lines around the separator otherwise.
- `split → join → split` is a no-op on canonical layouts.

## 2. Resume command formatter

### US-2.1: Backend mapping (incl. cwd + dangerous)
- WHEN `backend = claude` AND `cwd = /p` AND `dangerous = False` THEN
  `format_resume_command(...) == "cd /p && claude --resume <sid>"`.
- WHEN `dangerous = True` THEN ` --dangerously-skip-permissions` is
  appended (claude) or ` --dangerously-bypass-approvals-and-sandbox`
  (codex).
- WHEN `cwd` is None/empty THEN the `cd …` prefix is omitted.
- Paths are shell-quoted via `shlex.quote` so spaces survive.
- Unsupported backends / empty `session_id` → returns None.

### US-2.2: Resume block (fenced, with heading)
- `format_resume_block(...)` returns the multi-line string
  `Resume:\n\`\`\`\n<cmd>\n\`\`\``, or None if the command does.
- Triple-backtick fence so Mattermost renders a code block.

### US-2.3: Backend aliases
- `normalize_backend` accepts purpose tokens (`claude`, `codex`),
  `canon_backend` output (`claudecode`), and SSE display names
  (`Claude Code`, `Codex`). Unknown → None.

## 3. Purpose merge behaviour

### US-3.1: Adds block after separator
- Empty Purpose + block → `"---\n<block>"`.
- Config-only Purpose + block → `"<config>\n\n---\n\n<block>"`.

### US-3.2: Replaces existing block
- Purpose with prior `---\n<old-block>` + new block → config preserved,
  trailing block replaced.

### US-3.3: None block strips trailing section
- Caller passes `None` (unsupported backend) → return only the config
  section, no separator, no trailing content.

## 4. Lifecycle write-points

### US-4.1: Invite claim
- WHEN `_claim_pending_invite` binds a session THEN the bridge calls
  `_update_resume_purpose(channel_id, session_id, pending.backend, pending.cwd)`
  before returning.

### US-4.2: CLI-originated channel
- WHEN `_create_channel_for_session` creates a fresh MM channel for an
  unclaimed VD `session_added` event THEN the bridge calls
  `_update_resume_purpose(channel_id, session_id, data["backend"], data["projectPath"])`
  before returning.

### US-4.3: Startup reconcile (`_reconcile_resume_purposes`)
- WHEN the daemon starts THEN it iterates every channel-level mapping
  (thread anchors skipped) and refreshes the resume block.
- WHEN `vd.get_session_meta(session_id)` returns metadata THEN backend
  AND cwd come from that metadata.
- WHEN VD doesn't know the session THEN backend falls back to
  `_backend_for_channel` and cwd is omitted from the command.

### US-4.4: Failure isolation
- Per-channel `mm.get_channel` / `set_channel_purpose` errors are
  logged and swallowed — never break the calling claim/reconcile path.
- Unsupported backends produce NO MM write (assertable via the test
  fake's `purposes` list).

## 5. `_persist_purpose` preserves resume section

### US-5.1: Round-trip on config write
- WHEN an operator's autorespond/model edit triggers `_persist_purpose`
  THEN the existing resume section (everything below the separator) is
  preserved verbatim in the rewritten Purpose.

## 6. Dangerous-permissions default

### US-6.1: Default is True
- `Config()` → `dangerous_permissions is True`.
- `MM_BRIDGE_DANGEROUS_PERMISSIONS=0` (or `false/no/off`) → False.
- TOML `dangerous_permissions = false` → False.

## 7. Tests

- `tests/test_purpose.py` — split/join helpers, separator semantics,
  `parse` ignores trailing section. **+8 new tests.**
- `tests/test_resume_header.py` — formatter, block, merge_into_purpose,
  normalize_backend. **+~30 new tests.**
- `tests/test_config.py` — flip default + new behaviour. **3 tests updated.**
- `tests/test_bridge.py` — claim path, CLI path, reconcile path, persist
  preservation. **+7 new tests, 3 existing rewritten.**

## 8. Out of scope

- Header cleanup. The previous feature wrote `Resume: …` lines into
  Headers; the bridge no longer touches Headers, so old content stays
  until an operator clears it.
- Thread-fork resume blocks.
- VD-side changes (still no new VD endpoints — see overview "Non-goals").
