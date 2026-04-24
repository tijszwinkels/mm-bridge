# mm-bridge — `channels` command

List Mattermost channels the bot can see, with substring filtering by title and client-side sort by recent activity. Primary purpose: discovery — so `mm-bridge post` / `read` targeting arbitrary channels becomes useful.

## Problem

Today there's no CLI way to discover a channel's `channel_id` from outside its own session. `mm-bridge channel` only prints the current session's id; everything else requires digging through the Mattermost web UI. With ~45 new channels per day in the dev team (91 after two days, on track for thousands by end of year), an agent that wants to post into a sibling or sister channel has no programmatic way to find it.

## Desired behaviour

One subcommand that lists bot-visible channels, filtered by substring and sorted by recency, with just enough output for a shell pipeline to pick a `channel_id`.

```
mm-bridge channels [--title <keyword>] [-n N] [--format text|json]
```

### Defaults

- **Scope:** channels the bot is a member of (the bot-scoped endpoint). Not the full public team directory — that's noise, and the bot usually only cares about channels it can post into.
- **Fetch:** single call to `GET /users/{bot_id}/teams/{team_id}/channels`. Confirmed against MM source (`server/channels/store/sqlstore/channel_store.go:1081`): `per_page` is silently ignored, the full list is returned in one response, ordered by `ch.DisplayName`. Client-side work is the only option.
- **Sort:** `last_post_at DESC`, tiebreak `create_at DESC`. New channels with no posts still surface via the tiebreak.
- **Display cap:** 20 rows by default (`-n N` to override, `-n 0` for unlimited).
- **`--title <kw>`:** case-insensitive substring match against both `display_name` and the URL-slug `name`. Applied before the display cap.
- **Format:** `text` (default) is tab-separated for easy `awk`/`cut` piping; `json` emits a projected list suitable for `jq`.

### Output — `text` format

Tab-separated: `<channel_id>\t<display_name>\t<badges>`. Timestamps omitted from the text output because they rarely help the caller decide; sort order alone conveys recency.

```
h5oep4r...wj7yq7f5ba6bzof4rty   mattermost-migration-impl    [session]
66un1ak...px3by8jo5ndai6rf5hh   mattermost-migration         [session] [purpose: claude, opus]
rwfcnx8...w5fb3mkcdkz8qmrhbdo   general
```

Badges:
- `[session]` if the channel is currently linked to a VibeDeck session (via `ChannelMapping.channel_to_session`).
- `[purpose: <trunc>]` if the channel `purpose` field is non-empty (truncated to ~40 chars).

### Output — `json` format

Array of objects projected from MM's channel records, one per matched channel:

```json
[
  {
    "id": "h5oep4r...",
    "name": "s-75f47f70c5e",
    "display_name": "mattermost-migration-impl",
    "last_post_at": 1776758032652,
    "create_at": 1776715023025,
    "purpose": "",
    "header": "Parent: ~mattermost-migration~",
    "session_id": "abc123..."
  }
]
```

`session_id` is `null` (or absent) for unlinked channels.

### Behaviour outside a MM session

Works identically — this command doesn't need the current session's sidecar. Only `MM_BOT_TOKEN` (and `MM_URL` / `MM_TEAM`) are required. Exit 1 with the usual "MM_BOT_TOKEN is required" error if missing.

### Edge cases

- **No matches:** exit 0, print nothing (text) or `[]` (json). Silent is fine for pipelines; the caller can detect empty output themselves.
- **Bot in zero channels:** same as no matches.
- **Purpose contains a tab or newline:** sanitised to a single space in the text output (the JSON format preserves the raw value).
- **display_name is empty:** fall back to `name` (the URL slug). MM guarantees `name` is non-empty.

## Scaling note

At current growth (~45 channels/day), the full-fetch approach works for the foreseeable future — 1k channels is ~500 KB on the wire and sub-second to deserialize and sort. If this becomes painful we'll move to a daemon-maintained index that consumes the WS events (`channel_created`, `channel_updated`, `posted`, `user_added`, `user_removed`) and persists a local file the CLI reads. That refactor is explicitly deferred; the CLI's contract doesn't change when it happens.

## Design decisions

1. **Bot-scoped, not team-scoped, by default.** The team-wide list is mostly irrelevant (channels the bot can't post to). No `--all` flag in v1; easy to add later.
2. **No server-side search fallback.** `/channels/search` exists but is team-scoped and matches `display_name` loosely (testing showed it returning unrelated channels for a precise term). Client-side filtering on a single-fetch list is more predictable.
3. **Sort tiebreak on `create_at`, not `update_at`.** `update_at` changes on metadata edits (rename, purpose change) which we don't want to boost a dead channel above an active one.
4. **No pagination, no TTL cache, no daemon index for v1.** Keep it simple; the "fetch all" cost is acceptable at current scale. Flagged as follow-up work.

## Build plan

### Step 1 — MM client helper

Add to `mm_client.py`:

```py
def list_bot_channels(self) -> list[dict]:
    """Return the full list of channels the bot is a member of in its team."""
    return self._driver.channels.get_channels_for_team_for_user(
        self._bot_user_id, self._team_id,
    )
```

Existing `get_bot_channel_ids` returns only IDs; keep both. `list_bot_channels` returns full channel records (id, name, display_name, last_post_at, create_at, purpose, header, type).

### Step 2 — CLI handler

New `cmd_channels(args)` in `cli.py`:

1. `_require_bot_token(cfg)`, `_make_mm_client(cfg)`, `mm.login()`.
2. `channels = mm.list_bot_channels()`.
3. Filter: drop `type == "D"` (DMs) by default — they're noise for post/read use cases. (Consider a `--include-dms` flag later; not in v1.)
4. If `args.title`: filter where `kw.lower() in (display_name + " " + name).lower()`.
5. Sort: `sorted(key=lambda c: (c["last_post_at"] or 0, c["create_at"] or 0), reverse=True)`.
6. Enrich with session_id via `ChannelMapping.load(cfg.state_file)` — one read, no writes. Sets the `[session]` badge and the JSON `session_id` field.
7. Truncate to `args.n` (default 20; 0 means unlimited).
8. Emit text or json.

### Step 3 — argparse wiring

Add subparser in `_build_parser`:

```py
p_channels = sub.add_parser(
    "channels", help="List Mattermost channels the bot can see.",
)
p_channels.add_argument("--title", help="Substring filter on display_name/name.")
p_channels.add_argument("-n", type=int, default=20,
                         help="Max rows to display (0 = unlimited). Default 20.")
p_channels.add_argument("--format", choices=["text", "json"], default="text")
p_channels.set_defaults(func=cmd_channels)
```

### Step 4 — Tests

Unit tests in `tests/test_cli.py` (or a new `tests/test_cli_channels.py`):

- Filter by `--title` matches both `display_name` and `name`, case-insensitive.
- Sort: channels with `last_post_at=0` fall back to `create_at` order.
- DM channels (`type="D"`) excluded.
- `[session]` badge appears only when `ChannelMapping` has an entry.
- `--format json` emits the documented projected shape (no raw MM fields beyond what's listed).
- `-n 0` disables the cap.
- Missing `MM_BOT_TOKEN` exits 1 with the standard error message.

Integration-ish: mock `MattermostClient` at the module boundary so tests don't hit a real server (matching the style in `tests/test_bridge.py`).

## Non-goals

- Team-wide channel discovery (all public channels, not just bot's).
- Name-based `--channel` resolution in `post`/`read` (covered in those specs — they accept IDs; callers pipe from `channels`).
- Archived/deleted channels.
- Per-channel unread counts or member listings.
- Cached or daemon-maintained index (deferred; will be its own spec when needed).
- Interactive picker / fuzzy match (pipe to `fzf` externally if desired).
