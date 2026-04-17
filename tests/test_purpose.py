"""Unit tests for mm_bridge.purpose.

Spec: specs/20260417-mattermost-bridge-v2/requirements.md §3
"""

from __future__ import annotations

import pytest

from mm_bridge.purpose import KNOWN_BACKENDS, PurposeConfig, parse


# A fixed model catalogue used by most tests — mirrors what VibeDeck would return.
_MODELS: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": ["gpt-5.4"],
    "pi": ["pi-v1"],
    "opencode": [],
}


def _models_for(backend: str) -> list[str]:
    return _MODELS[backend]


# ---------------------------------------------------------------------------
# Defaults / empty input
# ---------------------------------------------------------------------------


def test_empty_purpose_returns_defaults():
    cfg = parse("", "claude", "opus", _models_for)
    assert cfg == PurposeConfig(backend="claude", model="opus", mention_only=False, warnings=[])


def test_whitespace_only_purpose_returns_defaults():
    cfg = parse("   ,  ,\t, ", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.mention_only is False
    assert cfg.warnings == []


def test_none_default_model_stays_none_when_no_model_specified():
    cfg = parse("claude", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model is None
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Backend-only and backend+model
# ---------------------------------------------------------------------------


def test_backend_only_uses_default_model():
    cfg = parse("claude", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_backend_and_model():
    cfg = parse("claude, opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_case_insensitive():
    cfg = parse("Claude, Opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_codex_with_model():
    cfg = parse("codex, gpt-5.4", "claude", "opus", _models_for)
    assert cfg.backend == "codex"
    assert cfg.model == "gpt-5.4"
    assert cfg.warnings == []


def test_pi_backend_only_uses_default_model():
    cfg = parse("pi", "claude", "opus", _models_for)
    assert cfg.backend == "pi"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_all_known_backends_recognised():
    for backend in KNOWN_BACKENDS:
        cfg = parse(backend, "claude", None, _models_for)
        assert cfg.backend == backend
        assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Model-first (no backend token)
# ---------------------------------------------------------------------------


def test_model_only_uses_default_backend():
    cfg = parse("opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_model_only_case_insensitive():
    cfg = parse("OPUS", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Unknown tokens → warnings, fall back to defaults
# ---------------------------------------------------------------------------


def test_typo_first_token_warns_and_uses_defaults():
    cfg = parse("opusz", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert len(cfg.warnings) == 1
    assert "opusz" in cfg.warnings[0]


def test_unknown_first_token_falls_back_to_defaults():
    cfg = parse("nonsense, sonnet", "claude", "opus", _models_for)
    # First token doesn't resolve → default backend. Then "sonnet" *does* match
    # a claude model, so it should be set.
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert len(cfg.warnings) == 1
    assert "nonsense" in cfg.warnings[0]


def test_unknown_later_token_warns():
    cfg = parse("claude, bogus", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"  # default_model since nothing else set it
    assert len(cfg.warnings) == 1
    assert "bogus" in cfg.warnings[0]


# ---------------------------------------------------------------------------
# mention-only flag
# ---------------------------------------------------------------------------


def test_mention_only_flag():
    cfg = parse("claude, mention-only", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_backend_model_and_mention_only():
    cfg = parse("claude, sonnet, mention-only", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_mention_only_case_insensitive():
    cfg = parse("Claude, Sonnet, MENTION-ONLY", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.model == "sonnet"


# ---------------------------------------------------------------------------
# Whitespace tolerance
# ---------------------------------------------------------------------------


def test_extra_whitespace_tolerated():
    cfg = parse("  CLAUDE ,  OPUS  ", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Two model tokens — last wins, first warns
# ---------------------------------------------------------------------------


def test_two_model_tokens_last_wins_warn_on_first():
    cfg = parse("claude, opus, sonnet", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert len(cfg.warnings) == 1
    assert "opus" in cfg.warnings[0]


def test_three_model_tokens_warn_on_each_earlier():
    cfg = parse("claude, opus, sonnet, haiku", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "haiku"
    # Two warnings: opus was displaced by sonnet, sonnet was displaced by haiku.
    assert len(cfg.warnings) == 2


# ---------------------------------------------------------------------------
# Robustness — never raises
# ---------------------------------------------------------------------------


def test_never_raises_when_models_callable_fails():
    def broken(backend: str) -> list[str]:
        raise RuntimeError("vibedeck unreachable")

    cfg = parse("claude, opus", "claude", "sonnet", broken)
    # Can't verify opus since model lookup failed — it becomes an unknown token.
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"  # default_model fallback
    assert len(cfg.warnings) == 1
    assert "opus" in cfg.warnings[0]


def test_never_raises_on_garbage_input():
    # Weird punctuation shouldn't explode the parser.
    cfg = parse(",,,,", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
