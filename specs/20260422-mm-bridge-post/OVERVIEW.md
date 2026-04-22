# mm-bridge — `post` command

Post a message to a Mattermost channel from the shell, with optional thread anchor and file attachments. Primary caller: an agent that wants to broadcast into a known channel without needing to open a VibeDeck session there.

## Problem

The bridge relays assistant output from live sessions to their bound channels automatically, but there's no first-class way to post a one-off message from outside that flow — e.g. a script notifying a channel, an agent cross-posting into a sibling channel, or a cron job dropping a daily note. Direct curl works but requires every caller to re-do: source the bridge's env, build the JSON body, resolve the channel (if current), handle file upload multipart, and apply path-root guards. Small, repetitive, bug-prone.

## Desired behaviour

```
mm-bridge post [--channel <id>] [--thread <root_post_id> | --no-thread]
               [--file <path>]... [-] <message>
```

### Channel resolution

1. If `--channel <channel_id>` is given, use it verbatim.
2. Otherwise, read the current session's sidecar (`sidecar.read(cfg.sidecar_dir, $CLAUDE_SESSION_ID)`). The sidecar returns `(channel_id, root_id?)`; use `channel_id` and treat `root_id` as the default thread anchor (see Thread handling below).
3. If neither is available → exit 2 with `Error: not running inside a Mattermost channel and no --channel given.`

Channel IDs only, no name lookup. The companion `mm-bridge channels` is the discovery path; agents pipe its output into `--channel`.

### Thread handling

The bridge's sidecar already carries a two-line format for thread-forked sessions: line 1 = channel_id, line 2 = root_id (see `sidecar.py`). The three cases:

- **Session sidecar has no `root_id`** (channel-level session) and no `--thread` flag: post at channel level (no `root_id`).
- **Session sidecar has a `root_id`** (thread-forked session) and no `--thread` flag: post inside that thread by default. This matches the user's mental model — an agent in a thread-forked session that posts should stay inside the thread.
- **`--thread <root_post_id>`:** explicit override; post inside that thread regardless of sidecar.
- **`--no-thread`:** explicit override; post at channel level even when the sidecar's `root_id` is set. Useful when an agent in a thread-forked session needs to escape to the parent channel.

### Message source

- Positional `<message>`: the message body. Required unless `-` is given.
- `-` (single dash, in place of the positional): read the message body from stdin until EOF. Enables piping (`echo "text" | mm-bridge post -`).
- Whitespace-only messages exit 2 with `Error: message body is empty.` unless at least one `--file` is attached, in which case the message body may be empty (Mattermost allows attachment-only posts).

### File attachments

- `--file <path>` is repeatable; each path is uploaded once and added to the post's `file_ids`. Mattermost allows up to 10 file_ids per post; >10 exits 2 with a clear error before uploading anything.
- Paths must resolve (after symlink resolution and absolute normalization) inside one of `cfg.allowed_attachment_roots`. If `allowed_attachment_roots` is empty, paths are trusted (matches existing `<openFile />` behaviour — no regression).
- Missing files, unreadable files, files over `mm.get_max_file_size()` → exit 3 before posting. No partial-success ambiguity; either the whole post with all attachments succeeds or nothing is posted.
- Uploads use the existing `mm.upload_file(channel_id, path)`.

### Output on success

Prints the new `post_id` to stdout (single line, no prefix, no trailing newline beyond the one shell convention expects). Exit 0.

Scriptable:
```
POST_ID=$(mm-bridge post --channel "$CID" "hello")
mm-bridge post --channel "$CID" --thread "$POST_ID" "follow-up"
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Posted successfully. |
| 1 | Missing `MM_BOT_TOKEN` (standard across all subcommands). |
| 2 | User error (no channel resolved, empty body with no attachments, path outside allowed roots, >10 attachments). |
| 3 | MM API / upload failure (network, 5xx, login failure). |

## Design decisions

1. **No name-based `--channel` lookup.** Keeps this command tight. Discovery via `mm-bridge channels | grep ... | awk`.
2. **Default thread behaviour follows the sidecar.** An agent in a thread-forked session acting like "myself" should stay inside the thread unless it asks to escape.
3. **Attachments are pre-validated before posting.** An incomplete post is worse than no post — the caller can retry a clean failure.
4. **Path-root guard reused, not replaced.** Same mechanism as `<openFile />` so attachments posted via CLI follow the same safety rules as attachments posted by the bridge on behalf of a session.
5. **stdin support via bare `-`**, not `--stdin` or `<FILE`. Standard Unix idiom; matches `cat`, `kubectl apply -`, etc.

## Build plan

### Step 1 — CLI helper refactor

`_resolve_channel_from_session` in `cli.py` today returns only `channel_id` and uses a sidecar-path-based read instead of `sidecar.read`. Extend (or add alongside) an anchor-aware helper:

```py
def _resolve_anchor_from_session(
    sidecar_dir: Path | str, session_id: str,
) -> tuple[str, str | None]:
    """Return (channel_id, root_id) from the sidecar, or raise NotInMattermostChannel."""
```

Use `sidecar.read(Path(sidecar_dir), session_id)` directly; raise `NotInMattermostChannel` on `None`. Keep `_resolve_channel_from_session` as a thin wrapper that calls the new helper and returns only the channel_id, so `invite`/`channel`/`spawn` are unchanged.

### Step 2 — Attachment path guard (reuse existing helper)

`bridge.py:178` already exposes `_resolve_attachment_path(raw_path, project_path, allowed_roots) -> Path | None`, used by the `<openFile/>` directive handler (`bridge.py:1741`) and by `_resolve_purpose_cwd` (`bridge.py:776`). It does symlink-following resolution (`Path.resolve(strict=False)`), expanduser on roots, and `relative_to`-based membership.

For the new CLI:

- **Reuse, don't reimplement.** Drop the leading underscore (`resolve_attachment_path`) so it's public, or move it to a small `src/mm_bridge/paths.py` module. A rename in place is the lighter touch — update both existing call sites.
- **Pass `project_path=None`** from the CLI: there's no session-bound project for an ad-hoc `mm-bridge post`. The helper's existing trust-the-caller fallback (when both `project_path` is None and `allowed_roots` is empty → returns the resolved path as-is) carries through unchanged. This matches the bridge's existing posture: if the operator hasn't configured `allowed_attachment_roots`, paths are trusted.
- **Treat `None` return as a user error** in `cmd_post`: print `Error: --file <path> is outside allowed_attachment_roots` and exit 2.

No new helper is needed — Step 2 collapses to a rename + a one-line CLI call.

### Step 3 — `cmd_post(args)`

1. `_require_bot_token(cfg)`.
2. Resolve channel + default root_id:
   - If `args.channel`: use it, default root_id = None.
   - Else: `_resolve_anchor_from_session(cfg.sidecar_dir, _current_session_id())`.
3. Resolve effective thread anchor:
   - `args.no_thread` → None.
   - Else `args.thread` if provided → that value.
   - Else the sidecar's root_id (which may be None).
4. Read message: `args.message` unless it's `-`, in which case `sys.stdin.read()`. Strip only trailing newline (keep intentional body whitespace).
5. Validate: if empty message + no `--file` → exit 2.
6. For each `--file` path: `_validate_attachment_path(Path(p), cfg.allowed_attachment_roots)`, stat it, check size against `mm.get_max_file_size()`. Fail fast before uploading.
7. `mm.login()`, then upload each validated file in order, collecting `file_ids`.
8. `post = mm.post(channel_id=..., message=..., file_ids=..., root_id=...)`.
9. `print(post["id"])`; return 0.

All exceptions in steps 7–8 → exit 3 with `Error: <reason>` on stderr. Steps 2–6 exit 2.

### Step 4 — argparse wiring

```py
p_post = sub.add_parser("post", help="Post a message to a Mattermost channel.")
p_post.add_argument("--channel", help="Channel id. Defaults to the current session's channel.")
thread_group = p_post.add_mutually_exclusive_group()
thread_group.add_argument("--thread", metavar="ROOT_POST_ID",
                          help="Post as a reply inside this thread.")
thread_group.add_argument("--no-thread", action="store_true",
                          help="Post at channel level even if the session is thread-forked.")
p_post.add_argument("--file", action="append", default=[], metavar="PATH",
                    help="Attachment path (repeatable, max 10).")
p_post.add_argument("message", help="Message body, or '-' to read from stdin.")
p_post.set_defaults(func=cmd_post)
```

### Step 5 — Tests

Unit (`tests/test_cli_post.py` or extend `test_bridge.py`):

- Channel resolution: `--channel` wins; fallback to sidecar; missing both → exit 2.
- Thread resolution: sidecar with root_id → post includes root_id; `--no-thread` → strips it; `--thread` overrides.
- Stdin: `message="-"` reads from stdin.
- Empty body + no file → exit 2. Empty body + one file → OK.
- Attachment count > 10 → exit 2 before any upload.
- Path outside `allowed_attachment_roots` → exit 2.
- Happy path: one text, one file — asserts `upload_file` called once with the resolved path, `post` called with the returned `file_ids` and the right `root_id`.

Mock `MattermostClient` at the module boundary as elsewhere.

## Non-goals

- Editing / updating existing posts.
- Deleting posts.
- Reactions or threads-related bulk ops beyond the `--thread` anchor.
- Channel name resolution (out of scope; use `mm-bridge channels`).
- Templating / markdown preprocessing.
- Typing indicators while posting (atomic — the command is too fast to warrant one).
