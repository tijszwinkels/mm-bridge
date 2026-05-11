# Resume Block in Channel Purpose (cwd-aware, fenced) — Overview

## Problem Statement

The first iteration (`20260508-channel-header-resume-command/`) wrote a
single-line `Resume: <cmd>` into each bridged channel's Mattermost
**Header**. After running it for a few hours we found three things:

1. **The Header is the wrong field for a copy-paste command.** It's a
   single-line topbar element; multi-line content or code-fenced blocks
   render poorly. Operators wanted the command in the channel's
   description-style field where Mattermost actually renders markdown.
2. **The resumed command dumped the user in `~`, not the project.** The
   bridge already knows each session's cwd (purpose `cwd=…` token,
   `pending.cwd`, the SSE `projectPath`, VD's `get_session_meta`), so
   the natural form is `cd <cwd> && <cmd>`.
3. **Operators always run the bridge alongside `vibedeck --dangerously-skip-permissions`.**
   The previous default of `dangerous_permissions=False` meant the
   first thing every operator did was flip the env var. Flip the
   default instead.

## Goal

For every bridged channel, write a copy-pasteable code-fenced resume
block into the **Channel Purpose** below a stable separator, including
`cd <cwd> &&` and the matching dangerous-permission flag by default.

```
claude, opus, autorespond

---

Resume:
\`\`\`
cd /home/foo/project && claude --resume sess-abc --dangerously-skip-permissions
\`\`\`
```

The config tokens above the `---` separator continue to drive the
existing backend/model/autorespond/cwd parser. The block below is owned
by the bridge.

## High-Level Scope

### Channel Purpose layout

- Introduce a stable section separator (`---` on its own line, declared
  as `purpose.SECTION_SEPARATOR`).
- `purpose.parse()` only tokenises the section *above* the first
  standalone separator line, so adding/refreshing the resume block
  never mutates parsed config and never produces spurious warnings.
- Two new pure helpers (`purpose.split_config_section`,
  `purpose.join_sections`) own the layout. Both `_persist_purpose`
  (config writes) and `_update_resume_purpose` (resume writes) round-
  trip through these so writes from either direction preserve the
  other section.

### Resume command format

| Component       | Form                                              |
| --------------- | ------------------------------------------------- |
| cwd prefix      | `cd <shlex-quoted-cwd> && ` (omitted if cwd unset)|
| backend         | `claude --resume <sid>` / `codex resume <sid>`    |
| dangerous flag  | `--dangerously-skip-permissions` (claude)         |
|                 | `--yolo` (codex; hidden alias for `--dangerously-bypass-approvals-and-sandbox`, verified against codex-cli 0.128.0) |

Wrapped in `Resume:\n\`\`\`\n<cmd>\n\`\`\`` so Mattermost renders a
code-block with a copy button.

### Lifecycle write-points

1. **Invite claim** (`_claim_pending_invite`) — uses `pending.cwd` and
   `pending.backend`.
2. **CLI-originated channel** (`_create_channel_for_session`) — uses
   `data["projectPath"]` and `data["backend"]` from the SSE event.
3. **Startup reconcile** (`_reconcile_resume_purposes`) — reads
   `vd.get_session_meta(session_id)` to recover backend + cwd after a
   daemon restart. Falls back to MM-Purpose backend resolution if VD
   doesn't know the session (stale mapping). When the fallback fires
   we have no trustworthy cwd, so the cwd prefix is omitted but the
   resume command is still runnable.

### Dangerous-permissions default flip

`Config.dangerous_permissions` defaults to **True**. Set
`MM_BRIDGE_DANGEROUS_PERMISSIONS=0` (or TOML `dangerous_permissions = false`)
to opt out for constrained deployments.

## Non-goals

- No changes to thread-fork session handling — forks still don't get a
  Purpose-level resume block (a fork lives inside a thread but Purpose
  is channel-scoped).
- No retroactive cleanup of headers from the v1 feature; the bridge no
  longer writes Headers, but pre-existing `Resume: …` Header text
  remains until an operator clears it. Acceptable.
- No support for backends beyond claude/codex.
