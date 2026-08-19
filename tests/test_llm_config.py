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


def test_the_default_is_the_ceiling():
    """A code reviewer's reasoning tokens are the cheapest thing in the run."""
    assert DEFAULT_REASONING_EFFORT == "high"
    assert requested_reasoning_effort() == "high"
    assert reasoning_config_for("glm-5.2", structured=False) == {"effort": "high"}


def test_max_is_accepted_and_mapped(monkeypatch):
    """There is no `max` on OpenRouter's scale; sending one verbatim would be invalid."""
    for spelling in ("max", "MAXIMUM", " full "):
        monkeypatch.setenv("LLM_REASONING_EFFORT", spelling)
        assert requested_reasoning_effort() == "high", spelling


def test_empty_means_the_default_not_off(monkeypatch):
    """`.env.example` ships every setting blank, and four sibling flags read "" as
    *disabled*. Reading it as "send nothing" here would hand the provider's default
    to anyone who copied the template while the docs promised `high`."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    assert requested_reasoning_effort() == "high"


def test_the_decision_can_be_handed_back_explicitly(monkeypatch):
    for word in ("provider", "default", "off"):
        monkeypatch.setenv("LLM_REASONING_EFFORT", word)
        assert requested_reasoning_effort() is None, word
        assert reasoning_config_for("glm-5.2", structured=False) is None


def test_a_nonsense_level_is_ignored_rather_than_sent(monkeypatch, capsys):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "ludicrous")
    assert requested_reasoning_effort() is None
    assert "ignoring" in capsys.readouterr().out


def test_a_forced_tool_choice_on_deepseek_outranks_the_effort(monkeypatch):
    """The ordering that keeps a 400 from being configurable."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    assert reasoning_config_for("deepseek/deepseek-v4-pro", structured=True) == {"effort": "none"}
    # ...but its agent-loop calls (tool_choice auto) still think hard.
    assert reasoning_config_for("deepseek/deepseek-v4-pro", structured=False) == {"effort": "high"}


def test_the_effort_reaches_the_request_body(monkeypatch):
    """Asserting the wire, not the helper: this rides in `extra_body`, not a kwarg."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    model = build_chat_model(
        model="glm-5.2", api_key="test-key", base_url="https://example.invalid/v4",
        reasoning=reasoning_config_for("glm-5.2", structured=False),
    )
    assert model.extra_body["reasoning"] == {"effort": "high"}
