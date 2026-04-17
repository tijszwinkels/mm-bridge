"""Unit tests for mm_bridge.purpose.

Spec: specs/20260417-mattermost-bridge-v2/requirements.md §3
"""

from __future__ import annotations

import pytest

from mm_bridge.purpose import (
    KNOWN_BACKENDS,
    PurposeConfig,
    parse,
    to_purpose_string,
)


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
# cwd= token
# ---------------------------------------------------------------------------


def test_cwd_defaults_to_none():
    cfg = parse("claude, opus", "claude", "opus", _models_for)
    assert cfg.cwd is None


def test_cwd_absolute_path():
    cfg = parse("claude, cwd=/home/claude/projects/foo", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.cwd == "/home/claude/projects/foo"
    assert cfg.warnings == []


def test_cwd_preserves_case():
    cfg = parse("cwd=/Users/Tijs/Foo", "claude", "opus", _models_for)
    assert cfg.cwd == "/Users/Tijs/Foo"
    assert cfg.warnings == []


def test_cwd_with_backend_model_and_mention_only():
    cfg = parse(
        "claude, sonnet, cwd=/home/claude/projects/foo, mention-only",
        "claude", "opus", _models_for,
    )
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert cfg.mention_only is True
    assert cfg.cwd == "/home/claude/projects/foo"
    assert cfg.warnings == []


def test_cwd_relative_path_warns():
    cfg = parse("claude, cwd=./foo", "claude", "opus", _models_for)
    assert cfg.cwd is None
    assert len(cfg.warnings) == 1
    assert "cwd" in cfg.warnings[0].lower()
    assert "./foo" in cfg.warnings[0]


def test_cwd_empty_value_warns():
    cfg = parse("claude, cwd=", "claude", "opus", _models_for)
    assert cfg.cwd is None
    assert len(cfg.warnings) == 1
    assert "cwd" in cfg.warnings[0].lower()


def test_cwd_prefix_case_insensitive():
    cfg = parse("claude, CWD=/home/claude/foo", "claude", "opus", _models_for)
    assert cfg.cwd == "/home/claude/foo"


def test_cwd_surrounding_whitespace_tolerated():
    cfg = parse("claude, cwd = /home/claude/foo  ", "claude", "opus", _models_for)
    assert cfg.cwd == "/home/claude/foo"


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


# ---------------------------------------------------------------------------
# autorespond / noautorespond tokens + default_autorespond
# ---------------------------------------------------------------------------


def test_noautorespond_is_synonym_for_mention_only():
    cfg = parse("claude, noautorespond", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_autorespond_token_turns_mention_only_off():
    cfg = parse("claude, mention-only, autorespond", "claude", "opus", _models_for)
    # Last-write wins within Step 2a — autorespond clears the flag.
    assert cfg.mention_only is False


def test_default_autorespond_false_means_mention_only_by_default():
    cfg = parse("", "claude", "opus", _models_for, default_autorespond=False)
    assert cfg.mention_only is True


def test_default_autorespond_false_still_overridable_by_autorespond_token():
    cfg = parse(
        "claude, autorespond", "claude", "opus", _models_for,
        default_autorespond=False,
    )
    assert cfg.mention_only is False


def test_default_autorespond_true_defaults_to_responding():
    cfg = parse("", "claude", "opus", _models_for, default_autorespond=True)
    assert cfg.mention_only is False


def test_autoresponse_spelling_alias():
    """Users naturally type `autoresponse` / `noautoresponse` (with `e`)."""
    cfg = parse("claude, autoresponse", "claude", "opus", _models_for)
    assert cfg.mention_only is False
    assert cfg.warnings == []


def test_noautoresponse_spelling_alias():
    cfg = parse("claude, noautoresponse", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_autorespond_case_insensitive():
    cfg = parse("claude, AUTORESPOND", "claude", "opus", _models_for,
                default_autorespond=False)
    assert cfg.mention_only is False


# ---------------------------------------------------------------------------
# to_purpose_string — canonical serialization for persistence
# ---------------------------------------------------------------------------


def test_to_purpose_string_always_emits_flag():
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=False)
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, autorespond"
    )


def test_to_purpose_string_emits_mention_only_when_set():
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=True)
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, mention-only"
    )


def test_to_purpose_string_flag_independent_of_default():
    """Same config, different default_autorespond → same output."""
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=False)
    assert (
        to_purpose_string(cfg, default_autorespond=True)
        == to_purpose_string(cfg, default_autorespond=False)
    )


def test_to_purpose_string_with_cwd():
    cfg = PurposeConfig(
        backend="claude", model="opus", cwd="/home/claude/foo",
    )
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, autorespond, cwd=/home/claude/foo"
    )


def test_to_purpose_string_drops_model_when_none():
    cfg = PurposeConfig(backend="claude", model=None)
    assert to_purpose_string(cfg, default_autorespond=True) == "claude, autorespond"


def test_to_purpose_string_parseable_again():
    """Round-trip: serialize → parse → same effective config."""
    models = {"claude": ["opus", "haiku"], "codex": [], "pi": [], "opencode": []}
    orig = PurposeConfig(
        backend="claude", model="haiku", mention_only=True, cwd="/home/foo",
    )
    s = to_purpose_string(orig, default_autorespond=True)
    reparsed = parse(
        s, "claude", "opus", lambda b: models.get(b, []),
        default_autorespond=True,
    )
    assert reparsed.backend == orig.backend
    assert reparsed.model == orig.model
    assert reparsed.mention_only == orig.mention_only
    assert reparsed.cwd == orig.cwd
    assert reparsed.warnings == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
