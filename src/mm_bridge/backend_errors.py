"""Shared template + helpers for surfacing backend-invocation failures.

When a backend/harness interaction fails (harness unreachable, session create
fails, the CLI errors on boot, a run fails), the bridge logs the full error at
ERROR level AND posts a concise, human-facing message into the channel/thread
the user is looking at. These pure helpers shape that message so every failure
path reads the same: lead with *what was attempted*, name the backend, then the
trimmed error. The raw error/traceback stays in the log; the channel sees only
the meaningful line(s).
"""
from __future__ import annotations

_ERROR_DETAIL_MAX_LEN = 500


def condense_error_detail(raw: str, *, max_len: int = _ERROR_DETAIL_MAX_LEN) -> str:
    """Reduce a raw error string to the human-relevant line(s).

    A multi-line blob (e.g. a traceback that leaked through) collapses to its
    final, non-blank line — for exceptions that's the actual cause. Over-long
    detail is truncated with an ellipsis. Empty input yields a placeholder so
    the fenced block is never blank. The full error is preserved in the log by
    the caller; this only shapes what the channel sees.
    """
    text = (raw or "").strip()
    if not text:
        return "(no detail reported)"
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        text = lines[-1].strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def exception_detail(exc: BaseException) -> str:
    """Human-relevant detail for a caught exception.

    ``str(exc)`` is preferred (for httpx status errors it's already a clean,
    single line like ``agent-harness POST /v1/sessions -> 500: …``); a
    message-less exception falls back to its type name so the block is never
    empty.
    """
    msg = str(exc).strip()
    return msg or type(exc).__name__


def run_failure_detail(data: dict) -> str:
    """Human-relevant detail from a ``run.failed`` SSE payload.

    The harness emits one of two shapes (agent-harness ``orchestrator``):
    ``{error, error_type}`` when the run process couldn't start or crashed
    mid-run (e.g. the CLI binary wasn't found), or ``{returncode}`` when the
    CLI exited non-zero on its own. Falls back to a generic note if neither is
    present.
    """
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        etype = data.get("error_type")
        return f"{etype}: {error.strip()}" if etype else error.strip()
    returncode = data.get("returncode")
    if returncode is not None:
        return f"CLI exited with a non-zero status ({returncode})."
    return "The run failed before producing a reply."


def format_backend_error(action: str, backend: str | None, detail: str) -> str:
    """Build the channel-facing backend-error message.

    ``action`` is the attempted operation phrased as an infinitive
    ("start a session", "run your message", "fork this thread"). ``backend``
    is named when known. ``detail`` is passed through :func:`condense_error_detail`.
    """
    backend_phrase = f"the `{backend}` backend" if backend else "the backend"
    detail = condense_error_detail(detail)
    return (
        f":warning: I tried to {action} with {backend_phrase} and got this error:\n"
        f"```\n{detail}\n```\n"
        "The full error is in the bridge log. "
        "`mm-bridge doctor` on the host can diagnose config/connectivity issues."
    )
