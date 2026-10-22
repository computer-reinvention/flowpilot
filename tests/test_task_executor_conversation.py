import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import session as agent_session
from backend.task_contracts import Principal
from backend.task_settings import TaskSettings
from backend.tasks.memory import ProfileStore
from tests.fixtures.browser_tasks.doubles import bindings_double, channel_double


@pytest.mark.asyncio
async def test_hosted_private_question_never_uses_stdin_or_persists_response(tmp_path, monkeypatch):
    def no_stdin(*args):
        pytest.fail('Hosted execution read stdin')
    monkeypatch.setattr('builtins.input', no_stdin)
    settings = TaskSettings(state_dir=tmp_path)
    store = ProfileStore(settings, Principal(account_id='alice'), 'p')
    session = agent_session.BrowserSession(store.path.name, store=store, hosted=True, verbose=False,
                                  handoff_factory=lambda *args: channel_double(),
                                  on_question=AsyncMock(return_value={'values': {'otp': '246810'}}),
                                  observation_policy=bindings_double().observation)
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=SimpleNamespace(
        get_url=AsyncMock(return_value='https://example.test/form'))))
    async def inference(*args, **kwargs):
        result = await kwargs['interaction_tools']['request_input_values']('[{"name":"otp","label":"Code"}]')
        assert json.loads(result.extracted_content)['otp'] == '246810'
        save = args[7]
        # A step that reads the code as a placeholder is reusable and saved; one with
        # the code written in is rejected.
        assert not (await save('login', 'Enter the code', 'async (inputs) => inputs.otp', inputs=[{'name': 'otp', 'type': 'string'}])).error
        assert (await save('login_literal', 'Enter the code', "async () => '246810'")).error
        # Even transformed model responses are not persisted after private input.
        return 'Code characters: 2 4 6 8 1 0'
    monkeypatch.setattr(agent_session, 'drive_browser', inference)
    inputs = {}
    try:
        result = await session.ask('Sign in', inputs)
        assert result['answer'].startswith('Code characters')
        assert inputs == {'otp': '246810'}
        history = store.history()
        assert '246810' not in str(history)
        assert '2 4 6 8 1 0' not in str(history)
        assert [s['name'] for s in store.scripts()] == ['login'] and '246810' not in str(store.scripts())
    finally:
        store.close()


def test_hosted_session_requires_ports_without_opening_browser(tmp_path):
    with pytest.raises(ValueError, match='require interaction and observation'):
        agent_session.BrowserSession('alice', tmp_path, hosted=True)


@pytest.mark.asyncio
async def test_missing_completion_never_becomes_success(tmp_path, monkeypatch):
    # Exercise actual Pi completion semantics rather than a fake success answer.
    from backend.pi_browser import drive_browser
    from backend.replay import RunControl
    monkeypatch.setattr('backend.pi_browser.run_pi', AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match='without verified completion'):
        await drive_browser(object(), 'Synthetic task', {}, '', RunControl([False]),
                            AsyncMock(), AsyncMock(), AsyncMock(),
                            observation_policy=bindings_double().observation)


@pytest.mark.asyncio
async def test_hosted_failed_replay_does_not_automatically_retry_uncertain_effects(tmp_path, monkeypatch):
    store = ProfileStore(TaskSettings(state_dir=tmp_path), Principal(account_id='alice'), 'p')
    session = agent_session.BrowserSession(store.path.name, store=store, hosted=True, verbose=False,
                                  handoff_factory=lambda *args: channel_double(), on_question=AsyncMock(),
                                  observation_policy=bindings_double().observation)
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=SimpleNamespace(
        get_url=AsyncMock(return_value='https://example.test/cart'))))
    store.db.execute('INSERT INTO reflexes(domain,name,script) VALUES(?,?,?)',
                     ('example.test', 'add', 'async () => { /* synthetic mutation */ }'))
    store.db.commit()
    execute = AsyncMock(return_value=(False, 'Script mutated then failed'))
    repair = AsyncMock()
    monkeypatch.setattr(agent_session, '_execute_reflex', execute)
    monkeypatch.setattr('backend.runner._repair_reflex', repair)
    async def inference(*args, **kwargs):
        result = await args[6]('add')
        assert 'Effects may be partial' in result.error
        return 'Inspected partial effects; no retry authorized'
    monkeypatch.setattr(agent_session, 'drive_browser', inference)
    try:
        await session.ask('Synthetic mutation')
        execute.assert_awaited_once()
        repair.assert_not_awaited()
    finally:
        store.close()


def test_source_urls_are_cited_valid_and_deduplicated():
    from backend.tasks.runtime import source_urls
    answer = ('See https://en.wikipedia.org/wiki/Tiger_(film). Also (https://example.com/a), '
              'https://example.com/a again, https://en.wikipedia.org/wiki/[redacted] and http://bad url.')
    assert source_urls(answer) == ['https://en.wikipedia.org/wiki/Tiger_(film)', 'https://example.com/a', 'http://bad']
