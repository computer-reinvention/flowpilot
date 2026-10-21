"""Offline provider configuration checks against the installed adapter classes."""

import pytest

from backend.llm import DEFAULT_MODELS, KEYS, PREFIXES, get_llm


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for prefix in PREFIXES.values():
        for suffix in ("MODEL", "EXPLORATION_MODEL", "FAST_MODEL"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    for key in KEYS.values():
        monkeypatch.setenv(key, "test-not-a-real-key")


@pytest.mark.parametrize("provider", DEFAULT_MODELS)
@pytest.mark.parametrize("role", ["agent", "exploration", "fast"])
def test_provider_roles_construct_real_adapters(monkeypatch, provider, role):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    llm = get_llm(role)
    assert llm.model == DEFAULT_MODELS[provider][role]
    assert llm.provider == ("anthropic" if provider == "claude" else "google" if provider == "gemini" else provider)
    assert llm.temperature is None


@pytest.mark.parametrize("provider", DEFAULT_MODELS)
def test_explicit_role_overrides(monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    prefix = PREFIXES[provider]
    monkeypatch.setenv(f"{prefix}_MODEL", " custom-main ")
    assert get_llm().model == "custom-main"
    assert get_llm("exploration").model == "custom-main"
    monkeypatch.setenv(f"{prefix}_EXPLORATION_MODEL", "custom-explore")
    monkeypatch.setenv(f"{prefix}_FAST_MODEL", "custom-fast")
    assert get_llm("exploration").model == "custom-explore"
    assert get_llm("fast").model == "custom-fast"


@pytest.mark.parametrize("provider", DEFAULT_MODELS)
@pytest.mark.parametrize("role", ["agent", "exploration", "fast"])
def test_missing_key_is_clear_for_every_role(monkeypatch, provider, role):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv(KEYS[provider], " ")
    with pytest.raises(ValueError, match=KEYS[provider]):
        get_llm(role)


def test_provider_alias_and_invalid_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", " Anthropic ")
    assert get_llm().provider == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "typo")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_llm()


def test_reasoning_and_legacy_overrides(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert get_llm().reasoning_models == ["gpt-6.1-sol"]
    assert get_llm().frequency_penalty is None
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    assert get_llm().reasoning_models == []
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert get_llm()._requires_auto_tool_choice()
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    assert get_llm().thinking is None
