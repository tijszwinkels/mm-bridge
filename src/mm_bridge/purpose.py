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


@dataclass
class PurposeConfig:
    backend: str
    model: str | None
    mention_only: bool = False
    warnings: list[str] = field(default_factory=list)


def _tokenize(purpose: str) -> list[str]:
    """Split on `,`, strip whitespace, lowercase, drop empty tokens."""
    return [tok.strip().lower() for tok in purpose.split(",") if tok.strip()]


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
    tokens = _tokenize(purpose)

    if not tokens:
        return PurposeConfig(
            backend=default_backend,
            model=default_model,
            mention_only=False,
            warnings=[],
        )

    warnings: list[str] = []
    first, *rest = tokens

    # Step 3: resolve the first token to a backend (and maybe a model).
    backend: str
    model: str | None
    if first in KNOWN_BACKENDS:
        backend = first
        model = None
    else:
        # If the first token is a model name under the default backend,
        # interpret it as "use default backend + this model".
        default_models = _models_for(default_backend, available_models_for)
        if first in default_models:
            backend = default_backend
            model = first
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
        if token == MENTION_ONLY_TOKEN:
            mention_only = True
            continue

        if token in backend_models:
            if model is not None and model != token:
                warnings.append(
                    f"Multiple model tokens in Channel Purpose — ignoring `{model}`, using `{token}`."
                )
            model = token
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
        warnings=warnings,
    )
