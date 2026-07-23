"""Formatters for agent-harness watchdog notices posted to the channel.

agent-harness v0.1.1 (idle-watchdog spec, 2026-07-23) added three run
watchdog events on top of the existing idle-kill. Each carries the
*configured* thresholds in its payload, so these pure formatters derive the
operator-facing wording from the payload instead of hardcoding minute
counts. The bridge posts the result into the run's channel/thread.

Event → formatter:
- ``run.idle_warning``         → :func:`idle_warning_notice`   (kill deferred)
- ``run.max_runtime_warning``  → :func:`max_runtime_warning_notice`
- ``run.exceeded_max_runtime`` → :func:`exceeded_max_runtime_notice` (cap kill)
- ``run.timed_out_idle``       → :func:`idle_timeout_notice`   (idle kill)
"""
from __future__ import annotations

# Harness policy defaults (idle-watchdog spec §Layer 3/5). Used only as a
# fallback when a payload field is missing or unusable so a notice is never
# blank or nonsensical; the real values normally come from the event.
DEFAULT_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RUN_SECONDS = 3600
DEFAULT_MAX_RUN_WARNING_LEAD_SECONDS = 600


def _coerce_seconds(value: object) -> int | None:
    """Best-effort positive int seconds from a payload field.

    Returns ``None`` for anything unusable (missing, non-numeric, ≤ 0) so the
    caller can fall back to a default. ``bool`` is rejected explicitly — it is
    an ``int`` subclass but never a valid duration.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds > 0 else None
    return None


def humanize_seconds(seconds: int) -> str:
    """Human-friendly duration for channel prose.

    Whole hours read as ``"1 hour"`` / ``"2 hours"``; everything else rounds
    to the nearest minute (``"10 min"``). Sub-minute values floor to
    ``"1 min"`` so a notice never claims "0 min".
    """
    minutes = max(1, round(seconds / 60))
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


def idle_warning_notice(data: dict) -> str:
    """``run.idle_warning`` — the idle threshold was crossed but the process
    tree is still burning CPU, so the harness DEFERRED the kill. Reassure the
    operator that a long-running command is expected and work continues.
    """
    idle = _coerce_seconds(data.get("idle_seconds")) or DEFAULT_IDLE_TIMEOUT_SECONDS
    return (
        "⏳ Still working — a long-running command is keeping the session busy. "
        f"No new output for ~{humanize_seconds(idle)}, but it's still using CPU, "
        "so I'll keep going. `.stop` to end it now."
    )


def max_runtime_warning_notice(data: dict) -> str:
    """``run.max_runtime_warning`` — the hard runtime cap is ``seconds_until_kill``
    away. Warn so the operator can wrap up or plan a follow-up run.
    """
    cap = _coerce_seconds(data.get("max_run_seconds")) or DEFAULT_MAX_RUN_SECONDS
    lead = (
        _coerce_seconds(data.get("seconds_until_kill"))
        or DEFAULT_MAX_RUN_WARNING_LEAD_SECONDS
    )
    elapsed = max(cap - lead, 0)
    return (
        f"⏳ This run has been going ~{humanize_seconds(elapsed)} and will be "
        f"stopped at the {humanize_seconds(cap)} runtime limit. `.stop` to end "
        "it now, or send a follow-up message afterwards to continue in a new run."
    )


def exceeded_max_runtime_notice(data: dict) -> str:
    """``run.exceeded_max_runtime`` — the run hit the hard runtime cap and was
    stopped regardless of activity. The session itself is unharmed.
    """
    cap = _coerce_seconds(data.get("max_run_seconds")) or DEFAULT_MAX_RUN_SECONDS
    return (
        f"⚠️ _Run stopped at the {humanize_seconds(cap)} runtime limit. "
        "The session is fine — send a new message to continue._"
    )


def idle_timeout_notice(data: dict) -> str:
    """``run.timed_out_idle`` — the idle watchdog force-stopped a genuinely
    silent run. Replaces the stale "30 minutes" copy: the threshold now comes
    from the payload (``idle_seconds`` carries the *configured* timeout).
    """
    idle = _coerce_seconds(data.get("idle_seconds")) or DEFAULT_IDLE_TIMEOUT_SECONDS
    return (
        f"⚠️ _Session stopped after ~{humanize_seconds(idle)} of inactivity. "
        "The harness force-stopped it; the previous reply may be incomplete. "
        "Send a new message to resume._"
    )
