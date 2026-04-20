## Mattermost integration

If `~/.mm-bridge/sessions/$CLAUDE_SESSION_ID` exists, you're running inside a Mattermost channel. Your assistant replies flow to the channel automatically — no explicit "post" needed.

- **Getting human attention.** If a user is already talking to you, @mention them (`@username`) in your reply. If you're alone in the channel and need input, run `mm-bridge invite <username>` (default: `tijs`). When multiple users are in the channel, each user's message is prefixed with `username:` — use the prefix to decide who to mention.
- **Attaching local files.** Emit `<openFile path="..." [line="N"] />` anywhere in your reply; the bridge uploads the file, strips the directive from the visible text, and attaches it to the post. Files must live inside the bridge's `allowed_attachment_roots`.
- **Spawning sub-sessions.** Run `mm-bridge spawn [--title "<name>"] [--cwd <path>] [--backend claude|codex] [--invite <user>] [--no-forward-prompt] "<prompt>"` to start a fresh VibeDeck session in a new sibling Mattermost channel. By default the parent channel gets a post linking to the new channel with the prompt quoted. The new channel's `header` is set to `Parent: ~<parent-channel>~` so context is discoverable from the sub-channel.
