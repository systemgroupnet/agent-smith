"""The OPENROUTER_* settings were renamed; the old names must keep working."""

import pytest

from core.config import langchain_config as lc


@pytest.fixture(autouse=True)
def _fresh_warnings():
    lc._warned_renames.clear()


@pytest.mark.parametrize("new,old", sorted(lc.RENAMED_ENV.items()))
def test_the_old_name_still_works_and_warns_once(monkeypatch, capsys, new, old):
    monkeypatch.delenv(new, raising=False)
    monkeypatch.setenv(old, "legacy-value")
    assert lc.renamed_env(new) == "legacy-value"
    assert lc.renamed_env(new) == "legacy-value"
    out = capsys.readouterr().out
    assert out.count(f"{old} is deprecated") == 1
    assert new in out


@pytest.mark.parametrize("new,old", sorted(lc.RENAMED_ENV.items()))
def test_the_new_name_wins_when_both_are_set(monkeypatch, capsys, new, old):
    monkeypatch.setenv(new, "new-value")
    monkeypatch.setenv(old, "old-value")
    assert lc.renamed_env(new) == "new-value"
    assert "deprecated" not in capsys.readouterr().out


def test_an_empty_new_name_falls_through_to_the_old_one(monkeypatch):
    """.env.example ships every setting blank; a blank new name must not hide a set old one."""
    monkeypatch.setenv("DEFAULT_LLM_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "old-key")
    assert lc.primary_api_key() == "old-key"


def test_primary_settings_read_the_new_names(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_API_KEY", "k")
    monkeypatch.setenv("DEFAULT_LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "12000")
    assert lc.primary_api_key() == "k"
    assert lc.primary_base_url() == "https://llm.example.com/v1"
    assert lc.llm_timeout_ms() == 12000


def test_timeout_default_and_garbage(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_MS", raising=False)
    assert lc.llm_timeout_ms() == 300000
    monkeypatch.setenv("LLM_TIMEOUT_MS", "5min")
    assert lc.llm_timeout_ms() == 300000


def test_a_request_timeout_uses_the_renamed_setting(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TIMEOUT_MS", "9000")
    model = lc.build_chat_model(model="x/y", api_key="k", base_url="https://llm.example.com/v1")
    assert model.request_timeout == 9.0
