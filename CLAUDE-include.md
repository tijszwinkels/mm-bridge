## Mattermost integration

If `~/.mm-bridge/sessions/$CLAUDE_SESSION_ID` exists, you're running inside a Mattermost channel. Your assistant replies flow to the channel automatically — no explicit "post" needed. The bot account you post as is `@claude` in Mattermost (`/invite @claude` pulls you into a channel).

- **CLI env (`MM_BOT_TOKEN` etc.).** Every `mm-bridge` subcommand that hits the MM API (`invite`, `spawn`, `channels`, `post`, `read`) needs `MM_BOT_TOKEN` (and `MM_URL`, `MM_TEAM`) in the environment. If you get `Error: MM_BOT_TOKEN environment variable is required`, source the repo's `.env`: `set -a; source ~/projects/mm-bridge/.env; set +a`.
- **Getting human attention.** If the operator is in the channel, @mention them (`@username`). Otherwise — or if you're unsure — run `mm-bridge invite <username>` to pull them in. When multiple users are in the channel, each message is prefixed with `username:` — use the prefix to decide who to mention.
- **Attaching local files.** Emit `<openFile path="..." [line="N"] />` anywhere in your reply; the bridge uploads the file, strips the directive from the visible text, and attaches it to the post. Files must live inside the bridge's `allowed_attachment_roots`.
- **Spawning sub-sessions.** Run `mm-bridge spawn [--title "<name>"] [--cwd <path>] [--backend claude|codex] [--invite <user>] [--no-forward-prompt] "<prompt>"` to start a fresh VibeDeck session in a new sibling Mattermost channel. By default the parent channel gets a post linking to the new channel with the prompt quoted. The new channel's `header` is set to `Parent: ~<parent-channel>~` so context is discoverable from the sub-channel.
- **Discovering channels.** `mm-bridge channels [--title <kw>] [-n N]` lists channels the bot can see, sorted by recent activity. Filter by title substring; pipe the first column (channel_id) into `post` / `read`. `mm-bridge channel` (singular) prints the current session's channel_id for scripts that want to target "myself".
- **Reading scrollback.** `mm-bridge read [--channel <id>] [-n N] [--since 1h|2d|ISO]` prints recent posts from a channel (or the current session's channel if `--channel` is omitted). `--thread <root_post_id>` restricts to a thread; an agent running inside a thread-forked session reads its own thread by default.
- **Posting ad-hoc.** `mm-bridge post [--channel <id>] [--thread <root>] [--file <path>]... "<message>"` sends a one-off message. Omit `--channel` to post into the current session's channel. Use `-` in place of the message to read the body from stdin.
- **Summarizing a channel.** There's no dedicated subcommand; pipe `read` into an ephemeral Claude run. For example:

  ```sh
  mm-bridge read --channel <id> --since 1d --no-bot --format text | \
    claude -p --model haiku --no-session-persistence \
      "Summarize this Mattermost channel transcript in 3-5 bullets, focusing on decisions, questions, and action items."
  ```

  Codex equivalent:

  ```sh
  mm-bridge read --channel <id> --since 1d --no-bot --format text | \
    codex exec --model gpt-5.4-mini --ephemeral --sandbox read-only \
      "Summarize this Mattermost channel transcript in 3-5 bullets, focusing on decisions, questions, and action items."
  ```

  `claude -p` (alias for `--print`) runs a one-shot, non-interactive completion. `--model haiku` keeps summaries cheap. `--no-session-persistence` skips writing the conversation to `~/.claude/projects/.../<id>.jsonl` — only valid with `-p`. For Codex, `exec` runs non-interactively; when stdin is piped and a prompt is provided, the transcript is appended as a `<stdin>` block, `--ephemeral` avoids persisting session files, and `--model gpt-5.4-mini` keeps the run lightweight. `--no-bot` on the `read` side strips Claude's own prior replies so it doesn't re-read itself as context. Tune the prompt per use case.
