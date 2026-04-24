## Mattermost integration

If `~/.mm-bridge/sessions/$CLAUDE_SESSION_ID` exists, you're running inside a Mattermost channel. Your assistant replies flow to the channel automatically — no explicit "post" needed. The bot account you post as is `@claude` in Mattermost (`/invite @claude` pulls you into a channel).

- **CLI env (`MM_BOT_TOKEN` etc.).** `mm-bridge invite`/`spawn` need `MM_BOT_TOKEN` (and `MM_URL`, `MM_TEAM`) in the environment. If you get `MM_BOT_TOKEN environment variable is required`, source the repo's `.env`: `set -a; source ~/projects/mm-bridge/.env; set +a`.
- **Getting human attention.** If the operator is in the channel, @mention them (`@username`). Otherwise — or if you're unsure — run `mm-bridge invite <username>` to pull them in. When multiple users are in the channel, each message is prefixed with `username:` — use the prefix to decide who to mention.
- **Attaching local files.** Emit `<openFile path="..." [line="N"] />` anywhere in your reply; the bridge uploads the file, strips the directive from the visible text, and attaches it to the post. Files must live inside the bridge's `allowed_attachment_roots`.
- **Spawning sub-sessions.** Run `mm-bridge spawn [--title "<name>"] [--cwd <path>] [--backend claude|codex] [--invite <user>] [--no-forward-prompt] "<prompt>"` to start a fresh VibeDeck session in a new sibling Mattermost channel. By default the parent channel gets a post linking to the new channel with the prompt quoted. The new channel's `header` is set to `Parent: ~<parent-channel>~` so context is discoverable from the sub-channel.
