# mm-bridge — Quiet Tool-Use Notifications

Coalesce Claude Code tool-use notifications into one per-turn placeholder post and hard-delete it when the turn completes.

## Problem

Every tool invocation is currently posted as its own MM message (`_Using tool: Bash_`, `_Using tool: Read_`, …). On the dev `mattermost-preview` DB, 4898 of Claude's ~6800 posts (72%) are these one-liners. They're DB-cheap but noisy in channel scrollback and search.

## Desired behavior

Per VD session, at most one "tool-use" placeholder post exists at any moment.

- First tool call of a turn → create the post with a single line `_Using tool: Foo_`.
- Same tool again → edit the same post, bump a counter on the last line: `_Using tool: Foo (x2)_`, `(x3)`, …
- Different tool → edit the same post, append a **new line** for the new tool: `_Using tool: Foo (x3)_` / `_Using tool: Bar_`. A subsequent repeat of the new tool bumps the count on that last line.
- When Claude's real response lands (text block) → **drop the run state** (the placeholder post stays in the channel untouched), then post the response normally. The placeholder becomes a compact per-turn record of which tools were invoked.
- Tool errors (`tool_result` with `is_error=true`) → drop the run state and post the error as a normal message. Errors are signal.
- Interrupts / session stops (`session_status running=false`) → drop the run state as a safety net.
- **No deletes, ever.** A persistent tool-use summary avoids both the soft-delete tombstone in the MM UI and the privilege escalation needed for permanent-delete.

Example MM message transitions within one turn `[Bash, Bash, Read, Bash, Bash, <reply>]`:

```
post:  _Using tool: Bash_
edit:  _Using tool: Bash (x2)_
edit:  _Using tool: Bash (x2)_
       _Using tool: Read_
edit:  _Using tool: Bash (x2)_
       _Using tool: Read_
       _Using tool: Bash_
edit:  _Using tool: Bash (x2)_
       _Using tool: Read_
       _Using tool: Bash (x2)_
end-of-turn: drop state (post stays), then post real reply.
```

## Design decisions

1. **Per-session in-memory state on `Bridge`.** A `tool_use_posts: dict[session_id, ToolUseRun]` where `ToolUseRun` holds `post_id` and `lines: list[(tool_name, count)]`. In-memory is enough — a bridge restart mid-turn orphans one placeholder post; the cost of file I/O on every tool call outweighs that recovery value.
2. **Dispatch per block inside `_on_vd_message`.** Today `_extract_text_from_blocks` flattens blocks into one string. We switch to walking `msg["blocks"]` and handling each block type individually. A mixed-block event (`[text, tool_use]`) is handled naturally: the text path clears+posts, the trailing tool_use starts a fresh run.
3. **End-of-turn trigger = real text block.** Simpler and more reliable than gating on `session_status`. We still clear on `session_status running=false` as a safety net for interrupts and crashes.
4. **No delete at turn end.** The placeholder survives in the channel as a compact record of the tools used that turn. This sidesteps two problems: soft-delete leaves an ugly "(message deleted)" tombstone in the MM UI, and permanent-delete requires promoting the bot to `system_admin`. `mm_client.delete_post` still exposes a `permanent` kwarg (added during design) for future use by other callers; the bridge itself never calls it.
5. **No change to `tool_result` handling** beyond errors. Regular tool results aren't posted today; that stays.
6. **No change to attribution, directives, attachments.** The response-text path runs the existing code after the delete+clear.

## Server-side prerequisite

None. `ServiceSettings.EnableAPIPostDeletion` was flipped to `true` across the fleet (preview, plenny, tinkertank) earlier in the design exploration; it's unused by this feature and harmless left on.

### DB growth

Each turn leaves one current placeholder + N edit-history rows (one per tool switch) in `posts`. At ~5 edits/turn × 200 turns/week ≈ 1 200 rows/week ≈ 60 MB/year — same order as if we soft-deleted, but the posts are visible and useful in channel scrollback. Indexes keep live-post queries fast (`idx_posts_channel_id_delete_at_create_at`), so the cost is disk + backup size, not latency.

## Build plan

### Step 1 — MM client: `delete_post`

Add `delete_post(post_id: str, *, permanent: bool = False) -> None` to `mm_client.py`. The driver's `delete_post` doesn't take a `permanent` param, so for the `permanent=True` branch we use the generic `client.delete("/api/v4/posts/{id}", params={"permanent": "true"})`. The bridge only calls with the default today; the kwarg is there for operators who later promote the bot.

### Step 2 — Per-session tool-use run state

On `Bridge`, add:

```py
@dataclass
class ToolUseRun:
    post_id: str
    lines: list[tuple[str, int]]  # (tool_name, count)

self.tool_use_posts: dict[str, ToolUseRun] = {}
```

Helpers (module-level or Bridge methods):

- `_format_run(run) -> str`: renders lines to the MM message body.
- `_bump_or_append(run, tool)`: mutates run's lines in place.
- `_clear_run(session_id)`: hard-deletes the post (if any) and pops state. Idempotent. Safe from any thread/handler.

### Step 3 — Rework `_on_vd_message` block dispatch

Replace the flatten-then-post flow with per-block handling:

| Block type | Action |
|---|---|
| `tool_use` | Upsert tool-use run; create or edit the placeholder post. |
| `tool_result` (`is_error=true`) | `_end_tool_use_run`; post error as normal message. |
| `tool_result` (non-error) | Ignore (existing behavior). |
| `text` (non-empty) | `_end_tool_use_run`; run the existing directives / attachments / truncate / post path. |
| `text` (blank/whitespace) | Skip (existing behavior). |

Preserve the existing `<leaveChannel/>`, `<openFile/>`, truncation, and root_id behavior for the text path.

### Step 4 — Safety-net cleanup hooks

Call `_clear_run(session_id)` from:

- `_on_vd_session_status(running=false)` — catches interrupts/crashes without a final text reply.
- `_on_mm_user_removed` (bot removed), `_leave_channel`, `_run_stop_command`, `_restart_session_with_config` — anywhere we `posters.forget(session_id)` today.

### Step 5 — Tests

Unit (`tests/test_bridge.py`):

- `Bash, Bash, Read, Bash, Bash` → one create + 4 edits; final body has 3 lines (`Bash (x2)`, `Read`, `Bash (x2)`).
- Trailing text block → run state dropped, placeholder left untouched, reply posted.
- Tool error mid-run → run state dropped, placeholder left untouched, error posted.
- `session_status running=false` → run state dropped.
- Next turn creates a fresh placeholder with a new post_id (not an edit of the old one).
- Mixed-block event `[text, tool_use]` → text posted, then new run starts with one line.
- **`mm.deletes` must stay empty across all scenarios** — the placeholder is never deleted.
- Two concurrent sessions remain isolated (per-session state).

### Step 6 — Docs

Update `specs/20260417-mattermost-bridge-v2/design.md` (if it covers the assistant-message path) and mention the MM prereq in the bridge README.

## Risks / edge cases

- **Empty run state after delete failure.** If `permanent=true` succeeds but `update_post` has already been rate-limited, we could end up with a zombie placeholder. Mitigation: delete by `post_id` is idempotent; on the next turn we just start a new placeholder.
- **Ordering.** VD SSE events are ordered per session. Different sessions are isolated by the dict keying. No cross-session interaction.
- **Thread forks.** Placeholder lives on whichever anchor (channel or thread) the session is mapped to, same as today's tool-use posts. No special handling.
- **Very long runs.** Lines grow unbounded (one per tool switch). In practice turns are short, but we could cap at e.g. 20 lines with an ellipsis if this ever becomes a problem. Out of scope now.

## Configuration

`show_tool_use` (bool, default `true`) — when `false`, the bridge silently drops every `tool_use` block and never posts a placeholder. Channels see only real assistant replies and tool errors. Settable via TOML (`show_tool_use = false`) or env var (`MM_SHOW_TOOL_USE=false`). Global; not per-channel (yet).

## Non-goals

- Persisting run state across bridge restarts.
- Changing the non-error `tool_result` behavior.
- Caching typing-indicator state to signal "working" differently.
- Batching across turns.
