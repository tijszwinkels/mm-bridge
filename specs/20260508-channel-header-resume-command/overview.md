# Channel Header Shows Harness Resume Command — Overview

## Problem Statement

A Mattermost channel bound to a VibeDeck session is the easiest place for a
user to *find* a session — but the user has no obvious way to *resume* that
session locally in their own terminal. They have to know that the channel
maps to a session, dig the `session_id` out of the sidecar at
`~/.mm-bridge/sessions/<id>`, and remember the right CLI invocation
(`claude --resume <id>`, `codex resume <id>`, …).

That last step is friction. The session id is the only piece of information
the bridge already owns at session-claim time, and the CLI form is fixed
per backend. A copy-pasteable command in the channel topbar costs us
nothing and removes a recurring papercut.

## Goal

Whenever the bridge binds a VibeDeck session to a Mattermost channel,
write a copy-pasteable resume command into that channel's **Header**
(topbar text). When the bridge daemon is configured to pass through
dangerous-permissions to backends, include the matching flag in the
command so a resumed session gets the same permission level.

The Channel **Purpose** field is reserved for backend/model config and
must not be touched.

## High-Level Scope

### Resume-command formatter (per backend)

| Backend | Base command                  | With dangerous-permissions                                           |
| ------- | ----------------------------- | -------------------------------------------------------------------- |
| claude  | `claude --resume <id>`        | `claude --resume <id> --dangerously-skip-permissions`                |
| codex   | `codex resume <id>`           | `codex resume <id> --dangerously-bypass-approvals-and-sandbox`       |
| other   | (skip — no resume line emitted) |                                                                    |

The implementor MUST verify the exact CLI form for `codex resume` against
the codex CLI before shipping (`codex --help` / `codex resume --help`).
If `codex resume` doesn't exist or differs, use the actual form and update
this table.

### Where the command lives

Channel **Header**, on its own line, prefixed with a stable marker
(e.g. `Resume:`) so existing/future header content (`Parent: ~ch~`,
operator notes) can coexist. When a channel already has header content,
append/replace only the `Resume:` line — never clobber other lines.

### When the header is written

1. **Session claim** — both `_claim_pending_invite` and
   `_claim_pending_fork` in `bridge.py`. This is the moment the session
   first becomes addressable, so it's the natural write-point.
2. **Bridge startup reconcile** — for every channel ↔ session in the
   persisted `ChannelMapping`, set the header if it's missing or stale.
3. **Rebind** — covered by (1): a channel that later gets a new
   `session_id` runs through claim again and overwrites the line.

### Dangerous-permissions plumbing

The bridge does not currently know whether VibeDeck was launched with
`--dangerously-skip-permissions`. Two viable options; pick whichever is
simpler given the current code:

- **Mirror config**: a new bridge config key `dangerous_permissions`
  (TOML) / `MM_BRIDGE_DANGEROUS_PERMISSIONS` (env), default `false`.
  The operator sets it to match VD. Single source of truth lives in operator
  config — no API contract.
- **Query VD**: if VibeDeck exposes its `_skip_permissions` state via
  any HTTP endpoint or SSE event, query/subscribe and use that.

Implementor MUST check VD's API surface (`vd_client.py`,
VibeDeck's `server.py` routes) and prefer the API path *only if* a
public endpoint already exists. Otherwise add the config key. Don't add
a new VD endpoint as part of this work.

## Non-goals

- No retroactive cleanup of headers on channels whose session has ended
  (left-over headers don't break anything).
- No support for backends without a documented resume command.
- No changes to Channel Purpose or its parser.
