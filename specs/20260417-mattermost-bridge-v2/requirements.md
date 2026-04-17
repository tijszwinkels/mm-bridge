# Mattermost ↔ VibeDeck Bridge v2 — Requirements

## 1. Invite-Driven Session Creation (MM → VibeDeck)

### US-1.1: Bot invite creates a new VibeDeck session
As a user, I can invite the `claude` bot to a Mattermost channel with `/invite @claude` and a VibeDeck session is created and bound to that channel.

**Acceptance Criteria:**
- WHEN the Mattermost WebSocket emits a `user_added` event for the bot's `user_id` in a channel that has no existing channel↔session mapping THEN the bridge creates a new VibeDeck session for that channel
- WHEN the session is created THEN the bridge links the channel_id to the returned session_id and persists the mapping
- WHEN the session creation starts THEN the bot posts a welcome message showing the resolved backend/model/cwd AND invites the user to provide context, for example:
  > *"Session started — backend: claude, model: opus, cwd: /home/claude.*
  > *Hi! Reply to catch me up, or just start asking. Use `@claude catch up 50` to include the last 50 messages."*
- The session itself starts with **clean context** (no channel history is auto-included) — catch-up is opt-in per §10
- WHEN the bot is added to a channel that *already* has a mapping (v1 leftover or reconnect) THEN no new session is created

### US-1.2: Bot does not auto-join other channels
As a user, I do not see the bot silently appear in channels I didn't invite it to.

**Acceptance Criteria:**
- WHEN the bridge starts THEN it does NOT reconcile/join public team channels
- WHEN a new channel is created in the team THEN the bridge does NOT auto-join it
- The only channels the bot enters are (a) channels it was explicitly invited to, and (b) channels the bridge itself created for a VibeDeck-originated session (see §2)

### US-1.3: First-message-creates-session path removed
As a maintainer, the "user posts in an unmapped channel → auto-create session" flow from v1 is removed so channels never accidentally spin up sessions.

**Acceptance Criteria:**
- WHEN a user posts in a channel the bot is a member of but which has no mapping THEN the bridge does NOT create a VibeDeck session for that post
- IF the post arrives after the bot was invited but before the mapping is persisted THEN the post is queued and forwarded to the new session when it's ready (same pending-queue behaviour v1 had for MM-originated channel creation)

---

## 2. VibeDeck-Originated Sessions → Mattermost Channel

### US-2.1: CLI-started sessions get an MM channel
As a user, when I start a VibeDeck session from the CLI (or any path that isn't Mattermost), a channel is created in Mattermost so I can optionally watch or interact with it.

**Acceptance Criteria:**
- WHEN the VibeDeck SSE emits a `session_added` event for a session that has no existing mapping AND no pending MM channel waiting to claim it THEN the bridge creates a new MM channel
- WHEN the channel is created THEN the bot is the only member (no auto-invite of any human)
- WHEN VibeDeck later emits `session_summary_updated` THEN the channel's display name is updated to the summary title (existing v1 behaviour kept)
- The channel is **public** by default so users can discover and join it manually (private/DM variant is out of scope for v2)

### US-2.2: MM-originated sessions don't duplicate channels
As a user, when I invited the bot (§1.1), the VibeDeck `session_added` SSE event that results from that invite should NOT create a second channel.

**Acceptance Criteria:**
- WHEN the bridge creates a VibeDeck session from an MM invite THEN it remembers the pending session (cwd + initial-message prefix + backend) for up to 30 s
- WHEN a VibeDeck `session_added` event arrives matching a pending MM channel THEN the bridge claims it (links existing channel ↔ new session) instead of creating a new channel (same claim logic as v1's `_claim_pending_mm_channel_for_session`, adapted for the no-initial-message case below)
- IF no initial message was sent (see §4.2) THEN matching falls back to cwd + backend only

---

## 3. Channel Purpose Config Parsing

### US-3.1: Parse backend and model from Channel Purpose
As a user, I can write a comma-separated config in the channel's Purpose field to control which backend/model the session uses.

**Acceptance Criteria:**
- WHEN the bridge creates a session for an invited channel THEN it reads the channel's `purpose` field via `GET /api/v4/channels/{id}`
- WHEN the Purpose is non-empty THEN tokens are split on `,`, trimmed of whitespace, lowercased
- WHEN the first token matches a known backend name (`claude`, `codex`, `pi`, `opencode`) THEN that backend is used
- WHEN a later token matches a model name returned by VibeDeck's `GET /backends/{name}/models` THEN that model's index is used as `model_index`
- WHEN Purpose is empty THEN defaults from the config file are used
- Matching is case-insensitive and tolerant of extra spaces (`Claude,Opus` == `claude, opus`)

### US-3.2: Partial overrides
As a user, I can specify *just* a backend (no model) or *just* a model (using the default backend).

**Acceptance Criteria:**
- WHEN Purpose contains only a backend token (e.g. `pi`) THEN the default model for that backend is used (or VibeDeck's default if none configured)
- WHEN Purpose contains the default backend's name plus a model (e.g. `claude, sonnet`) THEN the model overrides the default but backend stays
- WHEN Purpose starts with a token that is neither a known backend nor a known model THEN the bridge posts a warning in the channel and falls back to defaults

### US-3.3: Unknown tokens warn, don't fail
As a user, if I typo a model name the session still starts and I get a clear warning.

**Acceptance Criteria:**
- WHEN a token can't be resolved to a backend or model THEN the bridge posts a channel message like *":warning: Could not parse Channel Purpose token `opusz` — using defaults (claude, opus)."*
- WHEN the warning is posted THEN the session creation proceeds with the resolved-or-default values; it does NOT abort

### US-3.4: Purpose changes after session creation
As a user, I understand that changing the Purpose after the session has started does not retroactively swap backends.

**Acceptance Criteria:**
- WHEN the Purpose is edited after a session exists in the channel THEN the bridge does NOT create a new session
- WHEN this happens (detected via `channel_updated` WebSocket event) THEN the bridge posts a one-time notice: *"Channel Purpose changed — this takes effect only for new sessions. Start a new channel (or thread) to use these settings."*

---

## 4. Config File (TOML)

### US-4.1: Default backend and model in config file
As an operator, I can configure defaults in `~/.config/mm-bridge/config.toml` without setting env vars.

**Acceptance Criteria:**
- The config file is loaded at startup from `$XDG_CONFIG_HOME/mm-bridge/config.toml` (falling back to `~/.config/mm-bridge/config.toml`)
- Parsed with stdlib `tomllib`
- Supported keys (all optional):
  ```toml
  default_backend = "claude"
  default_model = "opus"
  default_cwd = "/home/claude"
  state_file = "~/.config/mm-bridge/state.json"
  [mattermost]
  url = "localhost"
  port = 8065
  scheme = "http"
  team = "workspace"
  [vibedeck]
  url = "http://localhost:8765"
  ```
- WHEN an env var is also set THEN env takes precedence (matches v1 behaviour; lets operators override in shell)
- WHEN the config file does not exist THEN the bridge starts with built-in defaults and logs an info message
- WHEN the config file has invalid TOML THEN the bridge logs the error and exits non-zero (fail fast — don't silently start with wrong config)
- `MM_BOT_TOKEN` remains env-only (secret)

### US-4.2: Initial message for new sessions
As a user, when the bot is invited to a channel with an empty Purpose, the session starts without an initial prompt; I can then type my first message and it forwards normally.

**Acceptance Criteria:**
- WHEN a session is created from an MM invite THEN no synthetic "hello" message is sent to VibeDeck
- NOTE: this differs from v1, which required an initial message because sessions were created on first post. The new trigger is "bot invited", not "first message"
- IF a VibeDeck backend requires a non-empty message to start (check per-backend) THEN the bridge sends a minimal placeholder (e.g. `Hello`) and logs it; this gets called out in the design section

---

## 5. Threads as Conversation Forks

### US-5.1: New thread creates a forked session
As a user, when I start a thread on a message in a channel, a *new* VibeDeck session is forked from the parent session and replies in the thread go there.

**Acceptance Criteria:**
- WHEN a Mattermost post arrives with `root_id` set AND the bridge has no mapping for that `root_id` yet THEN the bridge calls `POST /sessions/{parent_session_id}/fork` with the post's text as the initial message (parent_session_id comes from the channel mapping)
- WHEN the fork returns a new session_id THEN the bridge persists a thread-fork mapping: `(channel_id, root_id) → forked_session_id`
- WHEN subsequent replies arrive in the same thread THEN they route to the forked session, not the parent
- WHEN the forked session emits assistant messages via SSE THEN they're posted back in Mattermost with `root_id` set to the thread root (so they render inside the thread)

### US-5.2: Fork disclaimer
As a user, I see a short notice when the fork is created so I know the fork includes the full history of the parent session, not just up to my reply point.

**Acceptance Criteria:**
- WHEN a thread-fork session is created THEN the bridge posts a disclaimer message in the thread (with `root_id` = thread root) containing text approximately: *":information_source: Forked conversation. The full history of the parent session up to its current state is included — not only up to the message you replied on."*
- The disclaimer is posted BEFORE any assistant output from the fork

### US-5.3: Fork fails gracefully
As a user, if forking fails (backend doesn't support it — e.g. opencode — or VibeDeck's fork is disabled) I still get useful feedback.

**Acceptance Criteria:**
- WHEN the fork endpoint returns `403` (fork disabled) OR `501` (backend doesn't support it) THEN the bridge posts an error in the thread: *":warning: Couldn't fork this conversation — [reason]. Reply in the main channel instead."*
- WHEN this happens THEN subsequent thread replies are NOT forwarded (thread is "dead" for that session)
- WHEN the fork endpoint fails with a network error THEN it's retried once; on second failure the same warning is posted

### US-5.4: Thread mapping persistence
As a maintainer, thread-fork mappings survive bridge restarts.

**Acceptance Criteria:**
- The state file schema is extended with a `thread_mapping` key: `{ "<channel_id>:<root_id>": "<session_id>", ... }`
- Reload at startup restores the mapping
- Format is backwards compatible: v1 state files load without the thread_mapping key and get an empty dict

---

## 6. `openFile` → Mattermost Attachment

### US-6.1: openFile directive attaches the file
As a user, when Claude outputs something like `<openFile path="src/foo.py" />` in a channel, I see `foo.py` attached to the post in Mattermost.

**Acceptance Criteria:**
- WHEN the bridge receives a VibeDeck `message` event with `role=assistant` AND the extracted text contains one or more `<openFile ... />` directives THEN each directive is parsed out
- The parser tolerates variations: `<openFile path="..." />`, `<openFile path="..." line="42" />`, `<openFile path="..." follow="true" />` (regex matches `/<openFile\s+([^>]*)\/>/gi` to mirror VibeDeck's JS)
- WHEN a `path` attribute is present THEN the path is resolved against the session's `projectPath` (from VibeDeck's session metadata) if relative, otherwise used as absolute
- WHEN the resolved file exists and is readable and under the size limit THEN the bridge uploads it to Mattermost (`POST /api/v4/files` with the channel_id) and includes the returned `file_id` in the post's `file_ids` array
- WHEN multiple `<openFile />` directives appear in one message THEN each resolves to its own attachment on the same post (Mattermost supports up to 10 file_ids per post)
- WHEN a `line` attribute is present THEN the post text gets a short suffix like *" (jump to line 42)"* — MM has no real "open to line" feature, it's just a hint

### US-6.2: Directives are stripped from the post body
As a user, I don't see raw `<openFile ... />` XML in my channel.

**Acceptance Criteria:**
- WHEN directives are extracted THEN they are removed from the text body before it's posted to Mattermost
- WHEN the entire body is *only* directives (no other text) THEN the post still gets created as long as at least one attachment succeeded (just empty text + files)

### US-6.3: File errors don't swallow messages
As a user, if a file referenced by an openFile is missing or too big, I still get the assistant's text message.

**Acceptance Criteria:**
- WHEN a path can't be resolved (file doesn't exist, outside project dir, permission denied) THEN the post is created without that attachment, and a warning line is appended: *"\n_Could not attach `path/to/file.py`: file not found._"*
- WHEN a file exceeds Mattermost's `MaxFileSize` (default 50 MB, read from `GET /api/v4/config/client?format=old`) THEN the attachment is skipped with a similar warning
- WHEN the upload fails mid-flight (network, 5xx) THEN the bridge retries once, then logs + warns

### US-6.4: Path traversal protection
As a maintainer, I don't want `<openFile path="/etc/passwd" />` to exfiltrate random files via a Mattermost channel.

**Acceptance Criteria:**
- WHEN a resolved path is outside the session's `projectPath` AND the path is absolute AND it's not under a configured `allowed_roots` list THEN the attachment is skipped with a warning (the text of the message still posts)
- The config file supports `allowed_attachment_roots = ["~", "/tmp"]` to relax this when appropriate; default is `["<project_path>"]`
- Symlinks are resolved before checking (no escape via symlink)

---

## 7. Migration from v1

### US-7.1: State file preserved
As an operator, upgrading from v1 to v2 doesn't lose channel↔session mappings.

**Acceptance Criteria:**
- WHEN the bridge starts with a v1 `state.json` file THEN existing `channel_to_session` entries are loaded unchanged
- WHEN the v1 file lacks `thread_mapping` THEN that field is initialized as empty and saved back on next write

### US-7.2: V1 behaviours removed
As a maintainer, v1-specific behaviours that conflict with v2 are deleted, not feature-flagged.

**Acceptance Criteria:**
- The `_run_mm_channel_membership_reconciler` / `_reconcile_mm_channel_membership_once` / `join_all_team_channels` code is removed
- The `_on_mm_channel_created` auto-join handler is removed
- The "first message in unmapped channel creates session" flow (`_handle_new_mm_channel_message` as it exists in v1) is replaced with the invite-driven flow; the pending-queue sub-machinery is adapted, not deleted
- `MM_SYNC_EXISTING` env var is removed (no startup backfill in v2)

### US-7.3: Existing channels keep working
As a user, channels I already had working with v1 still relay messages after upgrading.

**Acceptance Criteria:**
- WHEN a channel has a v1 mapping THEN messages in that channel route to the mapped session as before
- WHEN the bot was already a member of other public channels from v1's auto-join THEN it stays (the bridge doesn't actively leave channels)
- NEW behaviour (Purpose parsing, threads-as-forks, openFile attachments) applies to all channels — mapped or not — going forward

---

## 8. Typing Indicator While Claude Works

### US-8.1: Show typing indicator when session is running
As a user, I can see "claude is typing…" in the channel while VibeDeck is actively processing my message, so I know it hasn't hung.

**Acceptance Criteria:**
- WHEN VibeDeck's SSE emits `session_status` for a mapped session with `running: true` THEN the bridge starts publishing typing indicators to the session's MM channel
- WHEN VibeDeck's SSE emits `session_status` with `running: false` OR `session_status` stops for that session for >10 s THEN the bridge stops publishing
- Typing indicators are refreshed every 3 s while active (Mattermost expires them after ~5 s)
- Publishing uses `users.publish_user_typing(bot_user_id, channel_id)` via the existing `mattermostautodriver` client

### US-8.2: Typing indicators work inside threads
As a user, when I'm replying inside a thread (which is its own forked session), the typing indicator appears in the thread, not the main channel.

**Acceptance Criteria:**
- WHEN the running session is bound to a thread (via the thread-fork mapping from §5) THEN the bridge passes `parent_id = <thread_root_id>` to `publish_user_typing` so MM scopes the indicator to that thread
- WHEN a thread's parent channel also has its own session running in parallel THEN both indicators can be active simultaneously (independent refresh tasks)

### US-8.3: Resilience
As a maintainer, typing-indicator failures never take down message forwarding.

**Acceptance Criteria:**
- WHEN `publish_user_typing` raises (network blip, auth error) THEN the exception is logged at DEBUG (not WARN — too noisy) and the refresh loop continues on the next tick
- WHEN the bridge shuts down THEN all typing refresh tasks are cancelled cleanly

---

## 9. Observability

### US-9.1: Structured logging
As a maintainer, I can diagnose issues from the bridge's logs.

**Acceptance Criteria:**
- All major events logged at INFO: bot invite → session create, session_added claim, thread fork, openFile upload, Purpose parse result
- Failures logged at WARNING with enough context (channel_id, session_id, path) to diagnose
- Unexpected exceptions logged with full traceback (`logger.exception`)
- Sensitive fields (tokens) never logged

### US-9.2: Health check
As an operator, I can tell if the bridge is up and connected.

**Acceptance Criteria:**
- The bridge logs a single INFO line at startup confirming both connections: *"Connected — Mattermost (team=workspace, bot=claude) + VibeDeck (http://localhost:8765)"*
- WHEN either connection drops THEN a WARNING is logged on disconnect and on reconnect (v1 already does this for SSE; keep it)

---

## 10. Catch-up Command

### US-10.1: `@claude catch up N` injects last N channel messages
As a user, I can bring Claude up to speed on prior channel discussion with a single command.

**Acceptance Criteria:**
- WHEN a user posts `@claude catch up N` (N optional, integer, 1–500) in a channel mapped to a session THEN the bridge fetches the last N non-bot channel posts via `GET /api/v4/channels/{id}/posts?per_page=N` (ordered oldest → newest, excluding the catch-up command itself)
- WHEN `N` is omitted THEN default is 50
- WHEN `N > 500` THEN the bridge clamps to 500 and posts a note explaining the limit
- WHEN the messages are fetched THEN they are formatted as a single block:
  ```
  [Catch-up context — last N messages from this channel, oldest first]
  <username>: <message>
  <username>: <message>
  ...
  [End of catch-up]
  
  <user's original request following catch up, if any>
  ```
- WHEN that block is sent to the session THEN it's the *next* message VibeDeck receives (queued if another is in flight)
- WHEN the catch-up is sent THEN the bridge posts a confirmation reply: *":arrows_counterclockwise: Sent the last N messages as context."*

### US-10.2: Catch-up works at any time
As a user, I can run catch-up more than once in a session (e.g. mid-conversation if the thread drifted).

**Acceptance Criteria:**
- Catch-up is a regular command detected by prefix match on any message — not tied to session-start
- No deduplication — if the user runs it twice, Claude gets the context twice (user's choice)

### US-10.3: Catch-up excludes bot posts
As a user, catch-up context reflects human conversation, not Claude's prior replies.

**Acceptance Criteria:**
- WHEN formatting catch-up context THEN posts where `user_id == bot_user_id` are excluded
- System messages (joins/leaves, topic changes) are excluded
- Only text content is included — attachments and embeds are omitted from the catch-up block (out of scope for v2)

---

## 11. Multi-Participant Channels

### US-11.1: Prefix forwarded posts with `username:` only when multiple humans have spoken
As a user, in a 1-on-1 chat with Claude my messages forward as-is, but as soon as a second human speaks in the channel, every forwarded post gets a `username:` prefix so Claude can tell contributors apart.

**Acceptance Criteria:**
- The bridge maintains an in-memory `set[user_id]` of non-bot users who have posted in each mapped session, populated from forwarded posts
- WHEN a non-bot user posts AND the session's set has ≤1 distinct human THEN the post is forwarded as-is (no prefix)
- WHEN a non-bot user posts AND the session's set has ≥2 distinct humans THEN the post is forwarded with the format `<username>: <message>` (using Mattermost's `username` field, not `nickname` or computed display name)
- WHEN the threshold flips from 1 → 2 humans THEN the very first "multi-human" post includes the prefix (the earlier single-user posts are not retroactively re-sent; Claude gets the context from that point on)
- The posters set is NOT persisted across bridge restarts; on restart, behaviour resets to "single user" until a second human posts again (acceptable)
- For thread-fork sessions (§5) the posters set is tracked independently per forked session

### US-11.2: Bot's own posts never loop back
As a maintainer, Claude's output posted into Mattermost is never re-forwarded to VibeDeck as a user message.

**Acceptance Criteria:**
- WHEN the MM WebSocket emits a `posted` event with `post.user_id == bot_user_id` THEN it is ignored
- This check is belt-and-suspenders: Mattermost normally doesn't notify the bot of its own posts via WS, but v1 relies on that implicit behaviour and it shouldn't

### US-11.3: Mention-only mode
As a user, I can configure a channel where humans chat freely and Claude only responds when explicitly pinged.

**Acceptance Criteria:**
- WHEN the Channel Purpose includes the token `mention-only` THEN only posts that contain `@<bot_username>` (or `@claude`) are forwarded to VibeDeck
- WHEN a post is filtered out due to `mention-only` THEN it's not forwarded — no bot post, no warning
- The `@claude` mention itself is stripped from the forwarded text (so Claude doesn't see a stray @-handle)
- `mention-only` is orthogonal to backend/model tokens: `claude, sonnet, mention-only` is valid

---

## 12. Claude Can Leave the Channel

### US-12.1: `<leaveChannel />` directive
As a user, Claude can remove itself from a channel when the work is done, without needing me to kick it.

**Acceptance Criteria:**
- WHEN an assistant message contains `<leaveChannel />` (optionally with a `reason="..."` attribute, parsed like `openFile`) THEN the bridge:
  1. Strips the directive from the text and posts whatever's left as a final message (so Claude can say goodbye)
  2. Calls Mattermost's `channels.remove_channel_member(channel_id, bot_user_id)` to remove the bot
  3. Deletes the channel↔session mapping from the state file
- WHEN the `reason` attribute is present AND the assistant text is empty THEN the bridge posts `"_Leaving: <reason>_"` before leaving
- WHEN the leave-API call fails THEN the bridge logs a WARNING and posts *":warning: Failed to leave the channel."* so the user can manually kick

### US-12.2: Re-invite starts a fresh session
As a user, if I invite Claude back to a channel it previously left, it starts a new session, not the old one.

**Acceptance Criteria:**
- WHEN `<leaveChannel />` removes a mapping AND the bot is later re-invited to the same channel THEN §1.1 flow runs normally, creating a brand-new session
- The old session (on VibeDeck's side) is not terminated or modified — it's just unlinked

### US-12.3: User kick is equivalent to leave
As a maintainer, if a user kicks the bot out manually, the bridge cleans up its mapping.

**Acceptance Criteria:**
- WHEN the MM WebSocket emits `user_removed` for the bot in a mapped channel THEN the bridge deletes the channel↔session mapping
- The bridge does NOT terminate the VibeDeck session (same rationale as US-12.2)

### US-12.4: `@claude leave` command
As a user, I can type `@claude leave` to reset my context by making Claude leave — then re-invite later for a fresh session. This doesn't rely on Claude recognizing the intent in natural language.

**Acceptance Criteria:**
- WHEN a user posts a message matching `^@claude leave\b.*$` (case-insensitive, optional reason text after `leave`) in a channel mapped to a session THEN the bridge:
  1. Posts a short farewell (*"Leaving — invite me back any time for a fresh session."*, or *"Leaving: <reason>"* if a reason was given after `leave`)
  2. Calls `channels.remove_channel_member` to remove the bot
  3. Deletes the channel↔session mapping and clears attribution state for that session
- WHEN the command is issued inside a thread (post has `root_id`) THEN only the thread-fork mapping (§5) is removed; the bot stays in the channel and the parent session's mapping is untouched
- WHEN the command is issued in an unmapped channel THEN it's a no-op (nothing to leave from)
- The `@claude leave` post is NOT forwarded to VibeDeck — it short-circuits before the normal forward path, same as `@claude catch up` in §10
- Re-invite behaviour is the same as §12.2 — a brand-new session with fresh context

---

## 13. Channel Name ↔ Session Title Sync

### US-13.1: VibeDeck title → MM channel display name
As a user, when VibeDeck generates or updates a session title (auto-summary or manual via `/api/session-titles/set`), the MM channel's display name follows.

**Acceptance Criteria:**
- WHEN the SSE emits `session_summary_updated` for a mapped session THEN the bridge updates the MM channel's `display_name` to the new title (truncated to 64 chars — MM limit) via `PUT /api/v4/channels/{id}` (this is the existing v1 behaviour; keep it)
- WHEN the session's custom title is set via `/api/session-titles/set` and VibeDeck broadcasts an event for that THEN same behaviour. (If VibeDeck doesn't currently broadcast custom-title changes, the design can either poll or add an SSE event — design decision, not requirement)

### US-13.2: MM channel display name → VibeDeck session title
As a user, when I rename a channel in Mattermost, the VibeDeck session takes the new name.

**Acceptance Criteria:**
- WHEN the MM WebSocket emits `channel_updated` for a mapped channel AND the `display_name` changed since last seen THEN the bridge calls `POST /api/session-titles/set` with the channel's new display_name (trimmed, max 200 chars per VibeDeck's limit)
- WHEN the sync succeeds THEN no extra post is created (silent — the rename is its own feedback)
- WHEN the sync fails THEN the bridge posts a WARNING-level log, but no channel message (too chatty)

### US-13.3: Loop prevention
As a maintainer, the two sync directions don't ping-pong.

**Acceptance Criteria:**
- WHEN the bridge renames an MM channel because of VibeDeck title change THEN it marks that channel_id + new title in a small in-memory "recently synced" set for 10 s, so the resulting `channel_updated` WS event is ignored
- WHEN the bridge sets a VibeDeck custom title because of an MM rename THEN it marks that session_id + title similarly, so the resulting `session_summary_updated` SSE event (if VibeDeck re-broadcasts) is ignored
- The debounce is in-memory only; a bridge restart reissues sync once if names drifted during downtime (acceptable)
