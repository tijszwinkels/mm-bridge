# mm-bridge — `read` command

Print recent posts from a Mattermost channel (or thread) in a human-readable form, with optional limit and since-filter. Primary callers: agents catching up on a channel, humans grepping scrollback from a terminal, and pipelines that feed channel history into an LLM for summarization.

## Problem

Channel history is accessible via the Mattermost web UI and raw API, but nothing in the shell prints it readably. Agents that want to reason over recent activity currently have to curl `/channels/{id}/posts`, cross-reference every `user_id` with a second API call, parse JSON, and format the output themselves. Every agent reinvents the same ~30 lines of glue.

This command is also the backbone of catch-up — pipe `mm-bridge read` into `claude -p` for a summary, no special "summarize" subcommand needed.

## Desired behaviour

```
mm-bridge read [--channel <id>] [--thread <root_post_id> | --no-thread]
               [-n N] [--since <ts|duration>] [--format text|json|jsonl]
               [--no-bot]
```

### Channel / thread resolution

Identical to `post` — see `specs/20260422-mm-bridge-post/OVERVIEW.md`:

- `--channel <id>` wins; otherwise read current session's sidecar; else exit 2.
- `--thread <root>` restricts to that thread; `--no-thread` overrides a thread-forked sidecar back to channel-level.
- If the session sidecar has a `root_id` and `--no-thread` is not given, the default reads that thread (a thread-forked session reading "its own history" should see the thread, not the whole channel).

### Limit and since

- **`-n N`:** fetch the last N posts (newest). Default 50. Cap 500 (matches `cfg.catch_up_max_n`). `N=0` means "no per-fetch cap" — apply `--since` only. `N` over the cap is silently clamped (no warning — this is a scripting tool, keep output clean).
- **`--since`:** ISO-8601 timestamp (e.g. `2026-04-22T10:00:00Z`) **or** a relative duration (`30m`, `2h`, `1d`, `7d`). Relative durations are parsed locally and converted to a ms-epoch; the API receives a concrete `since` parameter either way. Invalid format → exit 2 with the expected format shown.
- Both `-n` and `--since` may be combined: result is posts newer than `--since` AND within the last N of those. No flag → `-n 50` default applies.

### Channel-level read

Uses `GET /channels/{id}/posts` with `per_page=N` and optional `since`. Result is most-recent-first from the server; we reverse to **oldest-first** for display (easier to read a conversation linearly, top-to-bottom).

### Thread read

Uses `GET /posts/{root}/thread` (returns the root post + all replies). Applies `-n` and `--since` client-side against that list. Root post is always included if it's within the window; threads are usually small so this is cheap.

### Bot posts

Included by default. `--no-bot` strips them (useful when piping into a summary prompt so Claude doesn't see its own prior replies as "user messages").

### Output — `text` format (default)

Per post, one block; posts separated by a blank line:

```
[2026-04-22 14:32] alice
Got the bridge running locally; want to test the channels command next.

[2026-04-22 14:35] claude
Starting with a small test channel. Want me to spawn a sub-session?
📎 notes.md (12 KB)
```

- Timestamp format: `[YYYY-MM-DD HH:MM]` in local time. One-line header, body underneath.
- Username resolved from `user_id` via `mm.get_user(uid)`, cached per-invocation. Unknown users (deactivated, deleted) render as `user:<short_id>`.
- Attachments listed as `📎 <filename> (<size>)` lines appended to the body. No download; the CLI is read-only.
- System posts (`post.type != ""`: joins/leaves, channel header changes, etc.) are skipped — they're noise in catch-up flows. Override with an `--include-system` flag if the need comes up; not in v1.
- Posts with empty body (attachment-only) still render their header and attachment lines.

### Output — `json` format

Projected object, single array, oldest-first:

```json
[
  {
    "id": "abc123",
    "create_at": 1776781000000,
    "user_id": "mhob8f9...",
    "username": "claude",
    "is_bot": true,
    "root_id": "",
    "message": "Starting with a small test channel.",
    "files": [
      {"id": "f1", "name": "notes.md", "size": 12345, "mime_type": "text/markdown"}
    ]
  }
]
```

`--format jsonl` emits one JSON object per line (newline-delimited), same shape. Useful for stream processing.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Read successfully (even if zero posts in range). |
| 1 | Missing `MM_BOT_TOKEN`. |
| 2 | User error (no channel, bad `--since` format, etc.). |
| 3 | MM API failure. |

## Design decisions

1. **Oldest-first display order.** Conversation reads top-to-bottom; reversing is trivial (`tac`) if needed for newest-first.
2. **Local time for display, epoch in JSON.** Text is for humans; JSON is for scripts.
3. **Username cache is per-invocation only.** Simple dict; no persistence. Channels usually have ≤20 unique posters in a 50-post window.
4. **Skip system posts by default.** They dilute signal in catch-up prompts.
5. **Attachments listed, not downloaded.** This is a `read` command, not a sync tool. Keep the surface small.
6. **Thread read uses `get_post_thread`, not channel posts + filter.** The server already returns the thread as a unit; let it.
7. **`--since` accepts durations.** `--since 1h` is what agents want 90% of the time; requiring an ISO timestamp would be cumbersome.

## Build plan

### Step 1 — MM client helpers

`get_posts` already exists. Add two:

```py
def get_posts_since(self, channel_id: str, since_ms: int, per_page: int = 200) -> list[dict]:
    """Posts created after since_ms. Oldest first."""

def get_thread_posts(self, root_id: str) -> list[dict]:
    """All posts in a thread (root + replies), oldest first."""
```

Both wrap `posts.get_posts_for_channel` / `posts.get_post_thread` and return a flattened, ordered list. Pagination: `get_posts_since` fetches until the response is below `per_page` or a hard safety cap (say 2000) is hit.

### Step 2 — Duration parsing

Small utility in a new `_parse_since` helper (place in `cli.py` — it's CLI-specific):

```py
_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")

def _parse_since(raw: str, now_ms: int) -> int:
    """Return a ms-epoch for --since. Raises ValueError on bad input."""
```

- Digits-only or ISO-8601 → parse as absolute.
- `<int><m|h|d>` → subtract from `now_ms`.
- Anything else → raise `ValueError` with a message listing accepted formats.

### Step 3 — Username cache

Per-invocation `_UserCache` (also module-level in `cli.py`):

```py
class _UserCache:
    def __init__(self, mm): ...
    def username(self, user_id: str) -> str:
        # returns "user:<short>" on lookup failure (bot user gets 'claude' via login)
```

The bridge's `MattermostClient` already exposes `get_user`. Cache on the `_UserCache` instance; don't touch the driver's internals.

### Step 4 — `cmd_read(args)`

1. Resolve channel + thread anchor (same code path as `post` — share the helper from step 1 of the post spec).
2. Parse `--since` (if given) into `since_ms`.
3. Decide which API to call:
   - Thread anchor set → `mm.get_thread_posts(root_id)`, then filter by `since_ms` and the last `n` (after sort).
   - Else if `since_ms` → `mm.get_posts_since(channel_id, since_ms)`, then take last `n`.
   - Else → `mm.get_posts(channel_id, n)`.
4. Drop system posts (`type != ""`).
5. If `args.no_bot`: drop `post.user_id == mm.bot_user_id`.
6. Sort oldest-first by `create_at`.
7. Render per `--format`.

### Step 5 — argparse wiring

```py
p_read = sub.add_parser("read", help="Print recent posts from a channel.")
p_read.add_argument("--channel", help="Channel id. Defaults to current session's channel.")
thread_group = p_read.add_mutually_exclusive_group()
thread_group.add_argument("--thread", metavar="ROOT_POST_ID",
                          help="Read only posts inside this thread.")
thread_group.add_argument("--no-thread", action="store_true",
                          help="Read channel-level even if the session is thread-forked.")
p_read.add_argument("-n", type=int, default=50,
                    help="Max posts (0 = unlimited within --since). Default 50, cap 500.")
p_read.add_argument("--since", help="ISO-8601 timestamp or duration like '1h', '2d'.")
p_read.add_argument("--format", choices=["text", "json", "jsonl"], default="text")
p_read.add_argument("--no-bot", action="store_true", help="Exclude bot posts.")
p_read.set_defaults(func=cmd_read)
```

### Step 6 — Tests

Unit (`tests/test_cli_read.py`):

- `--since 1h` parses to `now - 3600_000`.
- `--since 2026-04-22T10:00:00Z` parses to the right ms-epoch.
- Bad `--since` → exit 2.
- Thread-forked sidecar → `get_thread_posts` called, not `get_posts`.
- `--no-thread` with thread sidecar → `get_posts` called.
- System posts filtered from output.
- `--no-bot` filters bot posts.
- Text format: correct timestamp, username resolution, attachment lines.
- `--format json` / `jsonl` shape.
- `-n 0` with `--since` returns all posts since window, no cap.
- `-n 5000` silently clamped to 500.

Mock `MattermostClient` as elsewhere.

## Non-goals

- Following (live tail / `tail -f` style). Out of scope — `mm-bridge serve` is the streaming entrypoint, not this.
- Downloading attachments (the text output hints, JSON gives file metadata; `curl` does the rest).
- Full-text search in posts (`mm-bridge search <term>` could come later).
- Highlighting the current user's mentions or reactions.
- Reading DMs (same filter as `channels` — DMs are a distinct UX, deferred).
- Summarization — that's `mm-bridge read ... | claude -p <prompt>`; see the recipe in `CLAUDE-include.md`.
