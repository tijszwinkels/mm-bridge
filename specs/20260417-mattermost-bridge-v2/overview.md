# Mattermost ↔ VibeDeck Bridge v2 — Overview

## Problem Statement

The current `mm-bridge` (shipped 2026-04-16, at `/home/claude/projects/mm-bridge/`) mirrors every VibeDeck session as a Mattermost channel and auto-joins/creates channels liberally. After one day of use the interaction pattern is good, but the automation is too aggressive:

- The `claude` bot reconciles team membership every 10s and auto-joins every public channel in the team (`_run_mm_channel_membership_reconciler`) — it ends up in channels where a human just wanted to chat with other humans.
- New MM channels automatically kick off a VibeDeck session on first message, even when the user didn't want Claude there.
- Backend/model choice for a session is a global env var (`VD_NEW_SESSION_BACKEND`, `VD_NEW_SESSION_MODEL_INDEX`), so you can't pick `codex + gpt-5.4` in one channel and `claude + sonnet` in another.
- Threads currently forward to the same VibeDeck session as the parent channel, so starting a thread pollutes the main conversation rather than branching off it.

## Goal

Make the bridge invite-driven, per-channel-configurable, and feel like a real chat participant rather than a firehose mirror:

1. **Invite-driven sessions.** The `claude` bot no longer auto-joins channels. A user invites it with `/invite @claude` (Mattermost slash command), and *that* is what triggers a new VibeDeck session bound to the channel.
2. **CLI-originated sessions still get a channel.** When VibeDeck detects a new session that didn't originate from Mattermost (e.g. started from CLI), the bridge creates an MM channel containing only the bot — humans can join manually if they want to watch.
3. **Per-channel backend/model config via Channel Purpose.** Mattermost's "Channel Purpose" field carries a comma-separated config (e.g. `claude, opus`; `codex, gpt-5.4`; `pi`). The bridge parses it and passes the result to VibeDeck when creating the session. A config file provides the default when Purpose is empty.
4. **Threads are conversation forks.** Starting a thread on a message creates a *new* VibeDeck session forked from the parent session (via VibeDeck's existing fork API). Replies in the thread go to the forked session, not the parent.
5. **`openFile` commands become MM attachments.** When VibeDeck's assistant output contains an `<openFile path="..." />` directive (which the VibeDeck web UI uses to open a preview pane), the bridge uploads that file to Mattermost and attaches it to the relayed post so it's viewable in the channel.
6. **Opt-in channel catch-up.** Clean-slate sessions by default. The user can reply to the bot to provide context, or use `@claude catch up N` to inject the last N channel messages.
7. **Claude can leave.** Assistant output `<leaveChannel reason="..." />` removes the bot from the channel and unlinks the session. Re-invite creates a fresh session.
8. **Feels like a real user.** Typing indicators while the session is running. Per-sender attribution (`username:` prefix) kicks in automatically when a second human joins the conversation. Bidirectional channel-name ↔ session-title sync.

## High-Level Scope

### Trigger model

- **MM → VD (new)**: bot added to a channel (member-added event with `user_id = bot`) → bridge reads Channel Purpose → creates VD session with parsed backend/model → links channel ↔ session.
- **VD → MM (unchanged in spirit)**: VibeDeck `session_added` SSE event → bridge creates an MM channel containing only the bot → links channel ↔ session. No human auto-invites, no auto-join of existing channels.
- **Remove**: the team-wide auto-join reconciler and the "first message in an unmapped channel creates a session" path.

### Channel Purpose config parser

- Comma-separated tokens, whitespace tolerated.
- First token is the backend name (`claude`, `codex`, `pi`, `opencode`).
- Subsequent tokens are model hints (e.g. `opus`, `sonnet`, `gpt-5.4`, `gpt-5.3-codex-spark`).
- Unknown tokens → bridge posts a warning message in the channel, falls back to defaults.
- Config file (e.g. `~/.config/mm-bridge/config.toml`) defines defaults (`default_backend = "claude"`, `default_model = "opus"`). An explicit Purpose token overrides the corresponding default.
- Changing Purpose after the channel already has a session does **not** retroactively mutate the session — the setting applies only to session creation. We'll surface this clearly (log + optional notice).

### Threads as forks

- New thread detected (MM post with `root_id` set, not in a thread the bridge is already tracking) → bridge calls VibeDeck's fork endpoint against the parent session → new session created for that thread → links thread root ↔ forked session.
- Subsequent replies in the thread route to the forked session.
- Assistant output from the forked session is posted back in the thread (as MM post with `root_id = thread root`).
- **Fork-point disclaimer.** VibeDeck / Claude / Codex / Pi all fork from the current *head* of the source session, not from an arbitrary message. When the bridge creates a thread-fork it posts a small notice as the first message in the thread — something like *"Forked from the parent session's current state — the full conversation up to this point is included in the fork context, not just up to the message you replied on."* — so the user isn't surprised.

### `openFile` → Mattermost attachment

- VibeDeck's assistant output can contain command blocks like `<openFile path="src/foo.py" line="42" />`, which the VibeDeck web UI intercepts to open a preview pane (`templates/static/js/commands.js`).
- The bridge scans outgoing assistant text for these directives, resolves `path` against the session's project path, uploads the file to Mattermost (`POST /api/v4/files`), and attaches the returned `file_id` to the post it creates for that message.
- Line number hints are preserved as a note in the post text (Mattermost has no native "open to line" concept).
- Missing or unreadable files → post goes through without the attachment plus a small warning line; we don't swallow the message.
- Size limits respected (Mattermost default 50 MB, configurable). Oversize files → skip + warn.

### Catch-up command

- New sessions start with clean context. The welcome message tells the user how to provide context: reply with their own summary, or run `@claude catch up N` (default 50, capped at 500) to inject the last N non-bot posts as a formatted context block.
- Catch-up is available at any point in a session, not just at start (a user can run it again after a tangent).

### Multi-participant channels

- All non-bot posts forward to VibeDeck by default. Bot's own posts are filtered by `user_id` (belt-and-suspenders; Mattermost normally doesn't deliver a user their own WS events).
- **Attribution kicks in on demand.** While only one human has posted in a session, messages forward as-is. As soon as a second human speaks, the bridge starts prefixing every forwarded message with `<username>: ` (Mattermost `username`, not display name) so Claude can tell contributors apart. This is tracked in-memory per session (and per thread-fork).
- `mention-only` Channel-Purpose token filters forwarding to posts containing `@claude` — useful for channels where humans chat freely and Claude is on standby.

### Leaving a channel

- Assistant directive `<leaveChannel reason="..." />` (parsed the same way as `openFile`): strip the directive, post any remaining text as a farewell, call Mattermost's `remove_channel_member` for the bot, and drop the channel↔session mapping. The VibeDeck session itself is not terminated.
- Manual kick (user removes the bot via MM UI) is treated the same way — cleans up the mapping.
- Re-invite to the same channel starts a fresh session.

### Typing indicator

- VibeDeck already broadcasts `session_status` SSE events with `running: bool`. The bridge subscribes and calls Mattermost's `publish_user_typing(bot_user_id, channel_id, parent_id=thread_root or None)` every 3 s while running, stops on `running: false` or after 10 s of silence.
- Failures are logged at DEBUG (too noisy otherwise) and never take down message forwarding.

### Channel name ↔ session title sync

- **VD → MM**: existing behaviour kept — `session_summary_updated` SSE → `PUT channel display_name` (truncated to 64 chars).
- **MM → VD**: new — `channel_updated` WS event with a changed `display_name` → `POST /api/session-titles/set` (truncated to 200 chars, VibeDeck's limit).
- A short in-memory debounce set prevents ping-pong between the two directions.

### Migration from v1

- Existing `state.json` channel↔session mappings are preserved.
- Startup no longer auto-joins channels; the bot may already be in channels from v1 — we leave it there but don't start new sessions for them.
- The auto-join reconciler and "first-message-creates-session" code paths are removed (not feature-flagged — the user wants the new behavior).

## Resolved Design Decisions

- **Fork precision** — accepted: fork from head. VibeDeck / Claude Code / Codex / Pi all fork from the current conversation head; none support fork-at-message. The bridge posts a disclaimer as the first message in the thread so the user knows the fork includes the full parent history, not just up to the message they replied on.
- **Config format** — TOML. `~/.config/mm-bridge/config.toml`, parsed with stdlib `tomllib`.
- **`xhigh` / thinking levels** — dropped from v2 scope.

## Out of Scope

- Typed message payloads / Mattermost plugin rendering (that's the separate "Mattermost widget architecture" spec).
- General file/image relay between MM ↔ VibeDeck (only the assistant-side `openFile` → MM attachment flow is in scope; user-uploaded files in MM going back to VibeDeck are out).
- Session archival or channel deletion when a session ends.
- Slash commands other than Mattermost's built-in `/invite`. (Bot-control happens via assistant directives like `<leaveChannel />` and user commands like `@claude catch up N`, not custom slash commands.)
- Changing VibeDeck itself (fork-at-message, thinking-level API, etc.).
- Multi-user channels beyond attribution + mention-only: no per-user access control, no per-user rate limiting, no DM handling.
