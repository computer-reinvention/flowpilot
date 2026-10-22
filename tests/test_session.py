import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import repl


async def test_repl_repairs_and_persists_in_original_scope(tmp_path, monkeypatch):
    from backend import runner, llm
    session = repl.BrowserSession('repair-user', tmp_path, verbose=False)
    page = SimpleNamespace(get_url=AsyncMock(return_value='https://example.com'))
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    monkeypatch.setattr(repl, '_execute_reflex', AsyncMock(return_value=(False, 'stale selector')))
    async def repair(**kwargs):
        assert kwargs['inputs'] == {'date': '2026-10-21'}
        page.get_url.return_value = 'https://other.test/results'
        return True, 'repaired result', 'async()=>({success:true})'
    repair_mock = AsyncMock(side_effect=repair)
    monkeypatch.setattr(runner, '_repair_reflex', repair_mock)
    monkeypatch.setattr(llm, 'get_llm', lambda role: role)
    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        await save('search', 'search', 'async()=>{}', tags=['search'])
        assert not (await replay('search')).error
        return 'done'
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        result = await session.ask('search', {'date': '2026-10-21'})
        row = session.store.db.execute('SELECT * FROM reflexes').fetchone()
        assert row['domain'] == 'example.com'
        assert row['script'] == 'async()=>({success:true})'
        assert json.loads(row['tags']) == ['search']
        assert row['successes'] == 1 and row['failures'] == 1
        assert result['metrics']['repaired'] == 1
        repair_mock.assert_awaited_once()
    finally:
        session.store.close()


@pytest.mark.parametrize('failure', ['NEEDS_AUTH: login', 'NEEDS_HUMAN: captcha'])
async def test_repl_human_boundary_bypasses_repair(tmp_path, monkeypatch, failure):
    from backend import runner
    session = repl.BrowserSession('human-user', tmp_path, verbose=False, human_help=AsyncMock())
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=SimpleNamespace(
        get_url=AsyncMock(return_value='https://example.com'))))
    monkeypatch.setattr(repl, '_execute_reflex', AsyncMock(return_value=(False, failure)))
    repair = AsyncMock()
    monkeypatch.setattr(runner, '_repair_reflex', repair)
    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        await save('login', 'login', 'async()=>{}')
        await replay('login')
        return 'assisted'
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        await session.ask('login')
        repair.assert_not_awaited()
        session.human_callback.assert_awaited_once()
    finally:
        session.store.close()


def test_user_memory_isolation_and_lock(tmp_path):
    alice = repl.UserStore(tmp_path, 'alice')
    bob = repl.UserStore(tmp_path, 'bob')
    try:
        alice.db.execute("INSERT INTO reflexes(domain,name,description,script) VALUES('example.com','search','Search','async () => {}')")
        alice.db.commit()
        assert len(alice.scripts()) == 1
        assert bob.scripts() == []
        with pytest.raises(ValueError, match='already has'):
            repl.UserStore(tmp_path, 'alice')
        with pytest.raises(ValueError, match='User must'):
            repl.UserStore(tmp_path, '../alice')
    finally:
        alice.close(); bob.close()
    reopened = repl.UserStore(tmp_path, 'alice')
    try:
        assert reopened.scripts()[0]['name'] == 'search'
    finally:
        reopened.close()


async def test_replay_scoped_to_user_and_site_with_metrics(tmp_path, monkeypatch):
    session = repl.BrowserSession('alice', tmp_path, verbose=False)
    page = SimpleNamespace(get_url=AsyncMock(return_value='https://example.com/cart'))
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    execute = AsyncMock(return_value=(True, 'done'))
    monkeypatch.setattr(repl, '_execute_reflex', execute)
    async def drive(browser, task, inputs, entry, control, human, replay, save, on_event, interaction_tools):
        on_event({'type':'metric', 'name':'model_turn'})
        await save('add', 'Add product', 'async () => {}')
        assert not (await replay('add')).error
        page.get_url.return_value = 'https://other.test/cart'
        assert (await replay('add')).error
        return 'Cart ready'
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        result = await session.ask('Add milk', {'product':'milk'})
        assert result['metrics']['reused'] == 1
        assert result['metrics']['saved'] == 1
        assert result['metrics']['model_turns'] == 1
        execute.assert_awaited_once()
        assert json.loads(session.store.history()[0]['metrics'])['reused'] == 1
    finally:
        session.store.close()


async def test_failure_recorded_without_success_claim(tmp_path, monkeypatch):
    session = repl.BrowserSession('alice', tmp_path, verbose=False)
    session.browser = object()
    monkeypatch.setattr(repl, 'drive_browser', AsyncMock(side_effect=RuntimeError('failed stage')))
    try:
        with pytest.raises(RuntimeError, match='failed stage'):
            await session.ask('Do the task')
        assert session.store.history()[0]['result'] == 'RuntimeError: failed stage'
    finally:
        session.store.close()


async def test_original_reflex_pause_and_input_confirmation_primitives(tmp_path, monkeypatch):
    session = repl.BrowserSession('alice', tmp_path, verbose=False)
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=SimpleNamespace(
        get_url=AsyncMock(return_value='https://example.com'))))
    session.handoff = SimpleNamespace(request=AsyncMock(return_value={}))
    session.on_question = AsyncMock(side_effect=[{'values': {'city': 'Oslo'}}, {'confirmed': False}])
    execute = AsyncMock(return_value=(True, 'done'))
    monkeypatch.setattr(repl, '_execute_reflex', execute)
    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        await save('auth', 'Login', 'async () => {}', tags=['login'], pause_after_auth=True)
        await replay('auth')
        tools = kwargs['interaction_tools']
        result = await tools['request_input_values']('[{"name":"city","label":"City"}]')
        assert json.loads(result.extracted_content) == {'city': 'Oslo'}
        assert inputs['city'] == 'Oslo'
        assert 'Do NOT proceed' in (await tools['request_confirmation']('Order?')).extracted_content
        return 'User declined'
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        await session.ask('Do something')
        execute.assert_awaited_once()
        assert session.handoff.request.await_count == 1
        assert session.on_question.await_count == 2
        row = session.store.db.execute('SELECT pause_after,tags FROM reflexes').fetchone()
        assert row['pause_after'] == 'auth' and json.loads(row['tags']) == ['login']
    finally:
        session.store.close()


async def test_otp_uses_chat_and_is_not_saved_in_inputs(tmp_path, monkeypatch):
    session = repl.BrowserSession('alice', tmp_path, verbose=False)
    session.browser = object()
    session.handoff = SimpleNamespace(request=AsyncMock())
    monkeypatch.setattr('builtins.input', lambda prompt: '246810')
    async def drive(*args, **kwargs):
        result = await kwargs['interaction_tools']['request_input_values']('[{"name":"otp","label":"SMS code"}]')
        assert json.loads(result.extracted_content)['otp'] == '246810'
        return 'Verified'
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        await session.ask('Sign in')
        session.handoff.request.assert_not_awaited()
        assert '246810' not in session.store.history()[0]['inputs']
    finally:
        session.store.close()


async def test_human_cancel_stops_owner_not_just_tool(tmp_path, monkeypatch):
    from backend.handoff import HandoffCancelled
    session = repl.BrowserSession('alice', tmp_path, verbose=False)
    session.browser = object()
    session.handoff = SimpleNamespace(request=AsyncMock(side_effect=HandoffCancelled('cancel')))
    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        async def dispatch():
            try:
                await human('Login')
            except RuntimeError:
                pass  # Pi's dispatcher normally sends tool errors back to the model.
        await asyncio.create_task(dispatch())
        await asyncio.sleep(1)
        pytest.fail('Cancelled run continued')
    monkeypatch.setattr(repl, 'drive_browser', drive)
    try:
        with pytest.raises(RuntimeError, match='Human cancelled'):
            await session.ask('Login')
        assert not session._asking
        assert 'cancelled' in session.store.history()[0]['result']
    finally:
        session.store.close()
