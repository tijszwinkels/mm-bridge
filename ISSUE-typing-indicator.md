# ISSUE: typing indicator inaccurate (stuck ON after the session goes idle)

Date: 2026-05-30
Branch: `fix/typing-idle-flip` (off `main`)

## Symptom

The Mattermost "user is typing…" indicator for a bridged session stays ON
after the agent has actually gone quiet. It is most visible for
external/observer sessions (CLI processes the operator started outside the
harness): the indicator can remain stuck indefinitely, never clearing.

## The cross-repo contract bug

This is a contract mismatch between two repos that share the harness SSE
stream:

- **agent-harness** (already on `main`) shipped a *freshness fix* that emits a
  `session.updated` SSE event whenever a session's status flips. Crucially it
  reuses the SAME event type for *both* directions:
  - `data.session.status == "running"` — the session just became busy.
  - `data.session.status == "idle"` — the session just **went quiet**. This
    idle-flip is the explicit "I'm done / silent now" signal.
  (See agent-harness `observer._maybe_publish_status_flip`, which builds the
  payload as `{"session": <full Session row incl. status>}`, and
  `models.py` `SessionStatus = Literal["idle", "running", "waiting_for_input",
  "archived"]`.)

- **mm-bridge** drove the typing indicator purely off the SSE **event type**
  and never inspected the status payload (`grep` confirmed zero `.status`
  reads in `bridge.py`). `session.updated` was a member of
  `HARNESS_ACTIVITY_EVENTS`, so *every* `session.updated` — including the
  idle-flip whose whole purpose is to say "went quiet" — was counted as
  activity.

The bridge therefore read the harness's "went quiet" signal as "still busy".

## Root cause (file:line, pre-fix)

`src/mm_bridge/bridge.py`:

- `HARNESS_ACTIVITY_EVENTS` (was ~line 36-44) **included** `"session.updated"`.
- `_on_harness_event` (~line 2218): for any `event_type in
  HARNESS_ACTIVITY_EVENTS` it did
  `self.last_activity_ts[session_id] = time.monotonic()` and
  `await self._start_typing_for_activity(session_id)` →
  `TypingIndicator.start` (`typing_indicator.py:22`), which publishes
  "user is typing" every `refresh_s` seconds.
- Typing is otherwise only stopped by `HARNESS_RUN_TERMINAL_EVENTS`
  (`_on_harness_run_lifecycle`, ~line 2845-2862) or by the silence watchdog
  `_run_typing_watchdog` (~line 641-657) after
  `typing_stop_after_silence_seconds` (15s) of no activity.

Live mechanism of the stuck indicator: for an external/observer session there
is **no run-terminal event**. The harness keeps emitting periodic
`session.updated` freshness ticks; each tick refreshed `last_activity_ts`, so
`now - last` never exceeded the 15s threshold and the silence watchdog never
fired. Net effect: typing stuck ON forever after the session was already idle.

## The fix and why reading status is the right layer

The bug is that the bridge inferred liveness from the event *type* when the
harness already encodes liveness explicitly in the event *payload*
(`session.status`). The fix moves the decision to that authoritative layer.

`src/mm_bridge/bridge.py`:

1. Removed `"session.updated"` from `HARNESS_ACTIVITY_EVENTS` — it is no longer
   unconditional activity.
2. Added `HARNESS_QUIET_SESSION_STATUSES = {"idle", "waiting_for_input",
   "archived"}` documenting which statuses mean "not producing output".
3. `_on_harness_event` now handles `session.updated` status-aware:
   - `_session_updated_is_activity(inner)` returns `True` only when
     `status == "running"` (read from `data.session.status`, falling back to
     `data.status`). A running-flip keeps the prior behavior: refresh
     `last_activity_ts` + (re)start typing.
   - Otherwise (quiet status, or missing/unknown status) it is NOT activity
     and calls the new `_stop_typing_for_idle(session_id)`, which pops
     `last_activity_ts` and stops the typing loop — clearing the indicator
     immediately instead of waiting on a watchdog the freshness ticks would
     keep resetting.

Why `status == "running"` (allow-list) rather than "anything not idle"
(deny-list): the SAFE default for an unknown/missing status is **non-activity**.
Genuine output always *also* emits `message` / `message.delta` / `tool.*`
events, and those remain in `HARNESS_ACTIVITY_EVENTS` and keep typing alive on
their own — so a bare freshness tick with no status should not. The
`run.started` / `message` / `tool.*` activity paths are untouched. (Confirmed
against agent-harness `observer.py` / `models.py`: real activity produces
message/tool events alongside status flips.)

## How verified (TDD)

New test class `TypingIndicatorActivityTests` in `tests/test_bridge.py`
(uses the in-process fakes: `FakeMattermostClient` records every
`publish_user_typing` into `.typing`; a real `TypingIndicator` is wired in the
`_BridgeTestCase` fixture with `refresh_s=0.01`):

- `test_session_updated_idle_does_not_start_typing` — RED on pre-fix code
  (idle-flip started typing); now no typing loop, empty `mm.typing`, no
  `last_activity_ts`.
- `test_idle_flip_stops_already_running_typing` — message starts typing, then
  an idle `session.updated` stops it (the missing external-session cleanup).
- `test_idle_status_at_top_level_data_is_honored` — defensive: status at
  `data.status`.
- `test_session_updated_unknown_status_is_not_activity` — SAFE default for a
  status-less freshness tick.
- `test_message_event_starts_typing` and
  `test_session_updated_running_starts_typing` — positive guards against
  over-correction (real activity / running-flip still start typing).

RED evidence (pre-fix): `4 failed, 2 passed` —
`AssertionError: 'ses_x' unexpectedly found in ['ses_x']` (typing was started
on an idle/unknown-status `session.updated`).

GREEN after fix: `6 passed`. Full suite: `611 passed, 1 skipped, 10 subtests
passed` (the 1 skip is pre-existing), no regressions.
