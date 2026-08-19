"""What we ask the model to *do*, as opposed to which model answers.

One rule here has a hard failure mode and is therefore worth a test rather than a
comment: a DeepSeek V4-class model rejects a forced `tool_choice` while thinking is
on, so a structured call to one has to run with reasoning off — and that override
must outrank the configured effort, or turning effort up would turn every working
structured call on that model into a 400.
"""

import pytest

from core.config.langchain_config import (
    DEFAULT_REASONING_EFFORT, build_chat_model, reasoning_config_for,
    requested_reasoning_effort,
)


@pytest.fixture(autouse=True)
def _no_ambient_effort(monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("STRUCTURED_NO_REASONING_MODELS", raising=False)


def test_the_default_is_xhigh():
    """A code reviewer's reasoning tokens are the cheapest thing in the run."""
    assert DEFAULT_REASONING_EFFORT == "xhigh"
    assert requested_reasoning_effort() == "xhigh"
    assert reasoning_config_for("glm-5.3", structured=False) == {"effort": "xhigh"}


def test_the_whole_documented_ladder_is_accepted(monkeypatch):
    """OpenRouter lists seven levels; an earlier version of this collapsed the top
    two onto `high`, which silently discarded the settings above it."""
    for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        monkeypatch.setenv("LLM_REASONING_EFFORT", level)
        assert requested_reasoning_effort() == level, level


def test_only_spellings_are_aliased_never_levels(monkeypatch):
    for spelling in ("MAXIMUM", " full ", "highest"):
        monkeypatch.setenv("LLM_REASONING_EFFORT", spelling)
        assert requested_reasoning_effort() == "max", spelling


def test_empty_means_the_default_not_off(monkeypatch):
    """`.env.example` ships every setting blank, and four sibling flags read "" as
    *disabled*. Reading it as "send nothing" here would hand the provider's default
    to anyone who copied the template while the docs promised `high`."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    assert requested_reasoning_effort() == "xhigh"


def test_the_decision_can_be_handed_back_explicitly(monkeypatch):
    for word in ("provider", "default", "off"):
        monkeypatch.setenv("LLM_REASONING_EFFORT", word)
        assert requested_reasoning_effort() is None, word
        assert reasoning_config_for("glm-5.3", structured=False) is None


def test_a_nonsense_level_is_ignored_rather_than_sent(monkeypatch, capsys):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "ludicrous")
    assert requested_reasoning_effort() is None
    assert "ignoring" in capsys.readouterr().out


def test_a_forced_tool_choice_on_deepseek_outranks_the_effort(monkeypatch):
    """The ordering that keeps a 400 from being configurable."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")
    assert reasoning_config_for("deepseek/deepseek-v4-pro", structured=True) == {"effort": "none"}
    # ...but its agent-loop calls (tool_choice auto) still think hard.
    assert reasoning_config_for("deepseek/deepseek-v4-pro", structured=False) == {"effort": "xhigh"}


def test_the_effort_reaches_the_request_body(monkeypatch):
    """Asserting the wire, not the helper: this rides in `extra_body`, not a kwarg."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")
    model = build_chat_model(
        model="glm-5.3", api_key="test-key", base_url="https://example.invalid/v4",
        reasoning=reasoning_config_for("glm-5.3", structured=False),
    )
    assert model.extra_body["reasoning"] == {"effort": "xhigh"}
