from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend import pi_llm
from backend.llm import get_llm


class Answer(BaseModel):
    result: str


def test_subscription_selects_pi_without_api_key(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    monkeypatch.setenv('AGENT_DRIVER', 'pi')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    assert isinstance(get_llm('fast'), pi_llm.PiSubscriptionLLM)


async def test_subscription_structured_output_validated(monkeypatch):
    async def run(task, system, tools, dispatch, role):
        assert role == 'fast'
        assert tools[0]['inputSchema']['required'] == ['result']
        await dispatch('submit_output', {'result': 'valid'})
    monkeypatch.setattr(pi_llm, 'run_pi', run)
    value = await pi_llm.PiSubscriptionLLM('fast').ainvoke([
        SimpleNamespace(role='system', content='Instructions'),
        SimpleNamespace(role='user', content='Observations'),
    ], Answer)
    assert value.completion.result == 'valid'


async def test_subscription_missing_output_rejected(monkeypatch):
    async def run(*args, **kwargs):
        return 'I am done'
    monkeypatch.setattr(pi_llm, 'run_pi', run)
    with pytest.raises(ValueError, match='without a valid'):
        await pi_llm.PiSubscriptionLLM('fast').ainvoke([], Answer)
