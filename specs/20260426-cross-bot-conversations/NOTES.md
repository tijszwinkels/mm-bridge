# Cross-channel bot-to-bot conversations — exploratory notes

Date: 2026-04-26.

## Summary

Two Claude Code sessions, each bridged to a different Mattermost channel, can
already hold a real conversation across channels with no bridge code changes.
The mechanism is:

- Session A (in channel X) calls `mm-bridge post --channel Y "..."` from its
  Bash tool.
- Because the CLI runs in a separate process from the daemon, the daemon's
  per-process own-post-id deque (`MMClient._own_post_ids`) does not match,
  so the WS echo is not suppressed.
- The daemon's dispatcher forwards the post to session B (linked to channel Y)
  as a normal user turn.
- Session B replies via the daemon as usual; replies stay in channel Y. For
  A to see them, A polls with `mm-bridge read --channel Y`.

This was confirmed live: a multi-round philosophy exchange across two
sibling channels spawned via `mm-bridge spawn`, both posting as the same
`@claude` MM identity.

## agentcom — conversational protocol (LLM-level, no code change)

`agentcom` is the project label for the cross-channel agent-to-agent
conversation protocol. It exists to make the transcript readable for a
watching human and to give each participant a stable handle. The rules
live in `CLAUDE-include.md` (under the "agentcom — talking to another
bridge-backed agent in a different channel" bullet) so every bridge-backed
Claude session sees them.

1. On the first post into a foreign channel, the agent introduces itself with
   a self-chosen persona name and states its own channel id (looked up via
   `mm-bridge channel`).
2. Every subsequent post from that agent is prefixed `name: `.
3. The first post invites the other agent to do the same and to reply into the
   first agent's channel.
4. When an agent receives an introduction in its own channel, it adopts the
   same `name: ` convention for replies.
5. Don't poll for cross-channel replies via `mm-bridge read`; the bridge
   already delivers them as a normal user turn. Polling on top of that leads
   to double-handling.
6. End every cross-channel post with `[cslhi: N]` on its own line. See the
   "Loop control" section below for how `cslhi` is computed and what happens
   at the soft cap of 12.

Both agents post as the same MM bot user (`@claude`), so without persona
prefixes a human watching cannot distinguish speakers.

## Warts to fix

### 1. Completion ping fires when the triggerer is the bot itself

`mention_user_when_done` posts `@<triggerer>` after each session run. When the
triggerer is the bot account (because the post that triggered the run came
from `mm-bridge post` running under the bot identity), this produces a bare
`@claude` post after every turn. Harmless (own-post-id dedup prevents a
loop) but noisy.

Fix: when resolving the triggerer username, suppress the ping if it equals
`self.mm.bot_username`.

Source: `src/mm_bridge/bridge.py` (completion-mention path).

### 2. CLI-authored bridge artifacts re-deliver to the linked session

The same property that makes cross-channel posting work — CLI posts not
being in the daemon's own-post deque — also means that bridge-internal CLI
posts (spawn announcements, kickoff posts) get forwarded to the linked
session as if a user had said them. Observed: the `:thread: Spawned ... in
~slug~` announcement in the parent channel arrived in the parent Claude
session as a user turn several minutes after the spawn.

Possible fixes, in increasing order of complexity:

- **Marker prop.** CLI sets `props.from_bridge_cli=true` on the post. The
  daemon's dispatcher drops posts carrying that marker for forwarding. Cheap
  and explicit.
- **IPC hand-off.** CLI signals the daemon (unix socket / shared file /
  REST shim on the daemon) so the daemon records the post id in its own
  deque before the WS echo arrives. More moving parts.
- **Post via the daemon.** CLI delegates the post itself to the daemon over
  a local control channel; daemon authors the post and records its id.
  Largest change.

Recommendation: marker prop — same idea as MM's own integration markers,
no new transport.

**Update 2026-05-10:** the bare marker (`from_bridge_cli` only) was
extended to all `mm-bridge post` calls and broke cross-channel
agentcom: posts authored from session A and addressed to channel B
(via `--channel B`) were dropped by the dispatcher before reaching
B's session. The fix pairs the marker with `from_bridge_cli_channel`
— the channel id of the session that should NOT receive the post as
a user turn (typically the channel the post lands in). The dispatcher
drops only when the recorded channel matches the post's actual channel
(real own-channel echo); cross-channel posts carry the SENDER's channel
id, which differs from the destination, and pass through. From a
non-session shell (no echo concern) the marker is omitted entirely.
See `tests/test_bridge.py::ForwardingTests::test_posted_with_marker_and_*`.

### 3. Loop control — chosen approach: prompt-level `cslhi` (not bridge-side)

We considered a bridge-side per-channel turn budget but explicitly rejected
it. The decision is to keep the bots in charge of when to keep going and
when to stop, and to use the prompt convention as the only mechanism. The
bridge does no detection.

**Convention (lives in `CLAUDE-include.md`):**

- Every cross-channel post ends with `[cslhi: N]` on its own line.
- `cslhi` = "counter since last human intervention". A bot computes it by
  reading recent scrollback, finding the most recent post by any MM user
  other than `@claude`, counting the `@claude` posts since, and adding 1.
- A post by any non-`@claude` MM user resets `cslhi` to 0.
- **Soft cap at 12.** When the next post's `cslhi` would exceed 12, the
  sending bot does *not* post into the other agent's channel. It writes the
  content it was going to send and posts it into *its own* channel
  addressed to `@tijs` (or whoever the operator is). The bot then waits for
  a human reply.

Within the cap the bot uses its own judgement — keep going while still
adding signal, stop earlier if the conversation has reached a natural close.

**Why prompt-level, not bridge-level:**

- The bots are the only ones who know whether they're still being productive.
- A bridge-side turn budget would override that judgement uniformly.
- A bridge-side repetition / convergence detector would either be cheap and
  noisy (false-positive on wrap-up) or expensive (LLM-as-judge per N turns).
- If the prompt convention is ever insufficient in practice, we can layer a
  bridge-side detector later. We don't yet have evidence we need one.

**Risks accepted:**

- A bot may ignore the cslhi convention. Worst case: token waste until the
  human notices. No corruption, no destructive action.
- "Human" is identified by MM username inequality with the `@claude` bot.
  Other bots in the channel (e.g. a future Codex bot) would also count as
  "human" by this rule and reset the counter; that's the correct behaviour
  for now since they break the same-identity ping-pong loop.

Out of scope (no longer pursuing): bridge-side per-channel turn counter,
`Config.bot_turn_budget`, and dispatcher-level enforcement.

## Out of scope for this note

- Multi-bot identities (different MM users for Claude vs Codex). Today both
  sides use the same `@claude` identity, which works. Splitting identities
  would require Codex to have its own bot account and token, and a story for
  per-identity engagement gating.
- Cross-channel reply visibility: agent A still polls B's channel manually
  with `mm-bridge read`. No subscribe/notify path. Acceptable for now.
