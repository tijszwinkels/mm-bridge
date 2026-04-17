"""Channel Purpose config parser.

Parses a Mattermost channel's "Purpose" text into a backend/model/flags config.
Never raises — unknown tokens become warnings the bridge surfaces as a channel
message.

Spec: specs/20260417-mattermost-bridge-v2/requirements.md §3
      specs/20260417-mattermost-bridge-v2/design.md §2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

KNOWN_BACKENDS: frozenset[str] = frozenset({"claude", "codex", "pi", "opencode"})

MENTION_ONLY_TOKEN = "mention-only"
CWD_PREFIX = "cwd="


@dataclass
class PurposeConfig:
    backend: str
    model: str | None
    mention_only: bool = False
    cwd: str | None = None
    warnings: list[str] = field(default_factory=list)


def _tokenize(purpose: str) -> list[str]:
    """Split on `,`, strip whitespace, drop empty tokens.

    Case is preserved — callers lowercase at comparison time. We can't
    lowercase eagerly because `cwd=` values carry case-sensitive paths.
    """
    return [tok.strip() for tok in purpose.split(",") if tok.strip()]


def _parse_cwd_token(token: str) -> tuple[str | None, str | None]:
    """If `token` is a `cwd=…` assignment, return (value_or_None, warning_or_None).

    Returns (None, None) when the token isn't a cwd assignment at all.
    Returns (path, None) when a valid absolute path is supplied.
    Returns (None, warning) when the assignment is malformed.

    Tolerates whitespace around the `=` (`cwd = /path`). The `cwd` key is
    case-insensitive but the path value preserves case.
    """
    lhs, sep, raw_value = token.partition("=")
    if not sep or lhs.strip().lower() != "cwd":
        return None, None
    value = raw_value.strip()
    if not value:
        return None, "Could not parse Channel Purpose `cwd=` token — value is empty."
    if not value.startswith("/"):
        return None, (
            f"Channel Purpose `cwd=` value `{value}` must be an absolute path."
        )
    return value, None


def _models_for(
    backend: str, available_models_for: Callable[[str], list[str]]
) -> list[str]:
    """Fetch models for a backend, tolerating callable failures.

    We treat a failing/raising models callable as "no models known" so that
    parsing never raises — the caller can still use defaults.
    """
    try:
        models = available_models_for(backend) or []
    except Exception:
        return []
    return [m.lower() for m in models]


def parse(
    purpose: str,
    default_backend: str,
    default_model: str | None,
    available_models_for: Callable[[str], list[str]],
) -> PurposeConfig:
    """Parse a channel purpose string into a PurposeConfig.

    Arguments:
        purpose: raw Purpose field from Mattermost.
        default_backend: backend to use when purpose is empty or unparseable.
        default_model: model to use when only the backend is specified.
        available_models_for: callable returning the list of model names for
            a given backend. Called at most once per backend during parsing.

    Never raises. Unknown tokens are collected into PurposeConfig.warnings.
    """
    raw_tokens = _tokenize(purpose)

    if not raw_tokens:
        return PurposeConfig(
            backend=default_backend,
            model=default_model,
            mention_only=False,
            cwd=None,
            warnings=[],
        )

    warnings: list[str] = []

    # Step 2a: extract cwd= assignments up-front. Paths are case-sensitive so
    # we keep the raw tokens (not lowercased) until this point.
    cwd: str | None = None
    remaining: list[str] = []
    for tok in raw_tokens:
        value, warn = _parse_cwd_token(tok)
        if warn:
            warnings.append(warn)
            continue
        if value is not None:
            if cwd is not None and cwd != value:
                warnings.append(
                    f"Multiple `cwd=` tokens in Channel Purpose — ignoring `{cwd}`, using `{value}`."
                )
            cwd = value
            continue
        remaining.append(tok)

    if not remaining:
        return PurposeConfig(
            backend=default_backend,
            model=default_model,
            mention_only=False,
            cwd=cwd,
            warnings=warnings,
        )

    first, *rest = remaining
    first_lc = first.lower()

    # Step 3: resolve the first token to a backend (and maybe a model).
    backend: str
    model: str | None
    if first_lc in KNOWN_BACKENDS:
        backend = first_lc
        model = None
    else:
        # If the first token is a model name under the default backend,
        # interpret it as "use default backend + this model".
        default_models = _models_for(default_backend, available_models_for)
        if first_lc in default_models:
            backend = default_backend
            model = first_lc
        else:
            warnings.append(
                f"Could not parse Channel Purpose token `{first}` — using defaults."
            )
            backend = default_backend
            model = None

    # Cache the model list for the *chosen* backend (may differ from default).
    backend_models = _models_for(backend, available_models_for)

    mention_only = False

    # Step 4: walk remaining tokens.
    for token in rest:
        token_lc = token.lower()
        if token_lc == MENTION_ONLY_TOKEN:
            mention_only = True
            continue

        if token_lc in backend_models:
            if model is not None and model != token_lc:
                warnings.append(
                    f"Multiple model tokens in Channel Purpose — ignoring `{model}`, using `{token_lc}`."
                )
            model = token_lc
            continue

        warnings.append(
            f"Could not parse Channel Purpose token `{token}`."
        )

    # Fill in default model if nothing else resolved one.
    # (The test suite treats `default_model` as a backend-agnostic fallback;
    # the bridge layer is responsible for turning names into backend-specific
    # indices at session-create time.)
    if model is None:
        model = default_model

    return PurposeConfig(
        backend=backend,
        model=model,
        mention_only=mention_only,
        cwd=cwd,
        warnings=warnings,
    )
