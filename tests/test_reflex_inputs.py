"""Reflex argument binding: schema, validation, Pi dispatch and REPL callbacks (no browser)."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import pi_browser, runner, session as agent_session
from backend.reflex_inputs import (ReflexInputError, bind_inputs, catalog_entry, normalize_fields,
                                   referenced_inputs)

FIELDS = normalize_fields([
    {'name': 'origin', 'type': 'string'},
    {'name': 'destination', 'type': 'string', 'description': 'City'},
    {'name': 'date', 'type': 'string', 'description': 'YYYY-MM-DD'},
    {'name': 'passengers', 'type': 'integer', 'default': '1'},
    {'name': 'fare', 'type': 'string', 'enum': ['saver', 'flex'], 'required': False},
])


def test_normalize_declares_required_and_coerces_defaults():
    by_name = {f['name']: f for f in FIELDS}
    assert by_name['origin']['required'] is True
    assert by_name['passengers'] == {'name': 'passengers', 'type': 'integer', 'default': 1, 'required': False}
    assert by_name['fare']['required'] is False and by_name['fare']['enum'] == ['saver', 'flex']
    assert normalize_fields(None) is None
    assert normalize_fields({'q': {'type': 'string'}}) == [{'name': 'q', 'type': 'string', 'required': True}]
    for bad in ([{'name': '1x', 'type': 'string'}], [{'name': 'a', 'type': 'date'}],
                [{'name': 'a', 'type': 'string'}, {'name': 'a', 'type': 'string'}],
                [{'name': 'n', 'type': 'integer', 'default': 'two'}], [{'name': 'a', 'secret': 1}],
                [{'name': f'f{i}', 'type': 'string'} for i in range(40)]):
        with pytest.raises(ValueError):
            normalize_fields(bad)


def test_bind_precedence_types_and_least_privilege():
    bound = bind_inputs('search', FIELDS, {'origin': 'Berlin', 'destination': 'London', 'passengers': '2'},
                        {'date': '2026-11-02', 'otp': '123456'})
    # Call values, then task values, then defaults; undeclared task values are withheld.
    assert bound == {'origin': 'Berlin', 'destination': 'London', 'date': '2026-11-02', 'passengers': 2}
    assert bind_inputs('search', FIELDS, {'origin': 'A', 'destination': 'B', 'date': 'd'}, {})['passengers'] == 1


def test_bind_reports_missing_invalid_and_unknown_without_values():
    with pytest.raises(ReflexInputError) as exc:
        bind_inputs('search', FIELDS, {'origin': ' ', 'passengers': 'many', 'fare': 'first', 'routeid': 'x'},
                    {'date': '2026-11-02'})
    payload = exc.value.payload()
    assert payload['error_code'] == 'invalid_input'
    assert payload['script_ran'] is False and payload['repair_attempted'] is False
    assert payload['missing'] == ['origin', 'destination']
    assert {i['name'] for i in payload['invalid']} == {'passengers', 'fare'}
    assert payload['unknown'] == ['routeid']
    assert payload['parameters'] == FIELDS
    assert 'many' not in json.dumps(payload)  # rejected values are not echoed
    with pytest.raises(ReflexInputError):
        bind_inputs('x', FIELDS, ['not', 'an', 'object'], {})
    with pytest.raises(ReflexInputError):
        bind_inputs('x', FIELDS, 'not json', {})
    assert bind_inputs('x', None, '{"q": "a"}', {}) == {'q': 'a'}
    with pytest.raises(ReflexInputError):
        bind_inputs('x', None, {'big': 'v' * 40000}, {})


def test_legacy_undeclared_reflex_keeps_task_inputs():
    assert bind_inputs('old', None, None, {'product': 'milk'}) == {'product': 'milk'}
    assert bind_inputs('old', None, {'product': 'eggs', 'qty': 2}, {'product': 'milk'}) == {'product': 'eggs', 'qty': 2}
    script = "async (inputs) => { q(inputs.origin); q(inputs['dest']); q(inputs.origin) }"
    assert referenced_inputs(script) == ['origin', 'dest']
    assert catalog_entry(None, script) == {'inputs': None, 'referenced_inputs': ['origin', 'dest']}


async def test_run_reflex_and_save_reflex_schemas(monkeypatch):
    seen = {}

    async def agent(task, system, tools, dispatch, **kwargs):
        seen.update({t['name']: t for t in tools})
        await dispatch('complete', {'verify': 'async()=>true', 'summary': 'ok'})
    monkeypatch.setattr(runner, '_execute_reflex', AsyncMock(return_value=(True, 'true')))
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(None, 't', {}, '', SimpleNamespace(checkpoint=AsyncMock()), None, None, None)
    run = seen['run_reflex']['inputSchema']
    assert run['required'] == ['name'] and run['properties']['inputs']['type'] == 'object'
    assert 'invalid_input' in seen['run_reflex']['description']
    save = seen['save_reflex']['inputSchema']['properties']['inputs']
    assert save['type'] == 'array' and save['items']['required'] == ['name', 'type']


async def test_pi_input_error_returns_typed_result_without_running(monkeypatch):
    calls = []

    async def run_reflex(name, inputs=None):
        calls.append((name, inputs))
        from browser_use import ActionResult
        return ActionResult(extracted_content='ran')

    async def bind(name, call_inputs=None):
        return bind_inputs(name, FIELDS if name == 'search' else None, call_inputs, {})
    run_reflex.bind_inputs = bind
    results = []

    async def agent(task, system, tools, dispatch, **kwargs):
        # No observe first: a rejected binding never reaches the browser guards.
        results.append(await dispatch('run_reflex', {'name': 'search', 'inputs': {'origin': 'Berlin'}}))
        results.append(await dispatch('run_reflex', {'name': 'search', 'inputs': {
            'origin': 'Berlin', 'destination': 'London', 'date': '2026-11-02'}}))
        results.append(await dispatch('run_reflex', {'name': 'legacy'}))
        results.append(await dispatch('run_reflex', {'name': 'legacy', 'inputs': 'not json'}))
        await dispatch('complete', {'verify': 'async()=>true', 'summary': 'ok'})
    monkeypatch.setattr(runner, '_execute_reflex', AsyncMock(return_value=(True, 'true')))
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(_browser(), 't', {}, '', SimpleNamespace(checkpoint=AsyncMock()), None, run_reflex, None)
    assert results[0]['error_code'] == 'invalid_input' and results[0]['missing'] == ['destination', 'date']
    assert results[-1]['error_code'] == 'invalid_input'
    # Callbacks receive the raw call inputs as keywords; the callback rebinds them.
    assert calls == [('search', {'origin': 'Berlin', 'destination': 'London', 'date': '2026-11-02'}),
                     ('legacy', None)]


async def test_name_only_callbacks_remain_compatible(monkeypatch):
    from browser_use import ActionResult
    seen = []

    async def run_reflex(name):
        seen.append(name)
        return ActionResult(extracted_content='ok')

    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('run_reflex', {'name': 'old'})
        await dispatch('complete', {'verify': 'async()=>true', 'summary': 'ok'})
    monkeypatch.setattr(runner, '_execute_reflex', AsyncMock(return_value=(True, 'true')))
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(_browser(), 't', {}, '', SimpleNamespace(checkpoint=AsyncMock()), None, run_reflex, None)
    assert seen == ['old']


def _browser():
    return SimpleNamespace(get_current_page_url=AsyncMock(return_value='https://trains.example/'),
                           agent_focus_target_id='tab')


def _session(tmp_path, monkeypatch, url='https://trains.example/search', **kwargs):
    session = agent_session.BrowserSession('binder', tmp_path, verbose=False, **kwargs)
    page = SimpleNamespace(get_url=AsyncMock(return_value=url))
    session.browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    return session


async def test_session_binds_declared_inputs_and_never_repairs_input_errors(tmp_path, monkeypatch):
    from backend import llm
    session = _session(tmp_path, monkeypatch)
    execute = AsyncMock(return_value=(True, '{"success": true}'))
    monkeypatch.setattr(agent_session, '_execute_reflex', execute)
    repair = AsyncMock()
    monkeypatch.setattr(runner, '_repair_reflex', repair)
    monkeypatch.setattr(llm, 'get_llm', lambda role: role)
    declared = [{'name': 'origin', 'type': 'string'}, {'name': 'destination', 'type': 'string'},
                {'name': 'passengers', 'type': 'integer', 'default': '1'}]

    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        assert (await save('search', 'Search trains', 'async (inputs) => inputs', inputs=declared)).error is None
        assert 'Invalid declared inputs' in (await save('bad', 'b', 'async()=>1', inputs=[{'name': 'x', 'type': 'date'}])).error
        missing = await replay('search', {'origin': 'Berlin'})
        assert json.loads(missing.error)['missing'] == ['destination']
        with pytest.raises(ReflexInputError):
            await replay.bind_inputs('search', {'destination': 'London', 'passengers': 'x'})
        assert not (await replay('search', {'origin': 'Berlin', 'destination': 'London', 'passengers': 2})).error
        assert not (await replay('search', {'origin': 'Paris', 'destination': 'Amsterdam'})).error
        return 'done'
    monkeypatch.setattr(agent_session, 'drive_browser', drive)
    try:
        result = await session.ask('trains', {'token': 'secret-otp'})
        scripts = session.store.scripts()
        row = session.store.db.execute('SELECT successes,failures FROM reflexes').fetchone()
    finally:
        session.store.close()
    assert [c.args[1] for c in execute.await_args_list] == [
        {'origin': 'Berlin', 'destination': 'London', 'passengers': 2},
        {'origin': 'Paris', 'destination': 'Amsterdam', 'passengers': 1}]  # task secret withheld
    repair.assert_not_awaited()
    assert tuple(row) == (2, 0)
    assert result['metrics']['input_errors'] == 2 and result['metrics']['reused'] == 2
    assert scripts[0]['inputs'][0] == {'name': 'origin', 'type': 'string', 'required': True}


async def test_session_partial_effects_are_not_auto_repaired(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_session, '_execute_reflex', AsyncMock(return_value=(
        False, f'SCRIPT EXECUTION FAILED\nError: missing\nEFFECTS: {runner.PARTIAL_EFFECTS} - changed')))
    repair = AsyncMock()
    monkeypatch.setattr(runner, '_repair_reflex', repair)

    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        await save('add', 'Add fare', 'async()=>{}')
        detail = (await replay('add')).error
        assert 'Do not replay it automatically' in detail
        return 'inspected'
    monkeypatch.setattr(agent_session, 'drive_browser', drive)
    try:
        result = await session.ask('add fare')
    finally:
        session.store.close()
    repair.assert_not_awaited()
    assert result['metrics']['partial_effects'] == 1


def test_legacy_store_migrates_and_catalogs_referenced_inputs(tmp_path):
    path = tmp_path / 'legacy'
    path.mkdir()
    db = sqlite3.connect(path / 'memory.sqlite')
    db.executescript('''CREATE TABLE reflexes (domain TEXT, name TEXT, description TEXT, script TEXT,
        successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, PRIMARY KEY(domain,name));
        INSERT INTO reflexes(domain,name,description,script) VALUES('a.test','old','Old','async (inputs) => inputs.query');''')
    db.commit(); db.close()
    store = agent_session.UserStore(tmp_path, 'legacy')
    try:
        assert store.scripts() == [{'domain': 'a.test', 'name': 'old', 'description': 'Old', 'successes': 0,
                                    'failures': 0, 'status': 'active', 'inputs': None, 'referenced_inputs': ['query']}]
    finally:
        store.close()


async def test_hosted_save_checks_task_values_not_declared_schema(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, hosted=True, handoff_factory=object, on_question=AsyncMock(),
                       observation_policy=object())
    declared = [{'name': 'origin', 'type': 'string'}]

    async def drive(browser, task, inputs, entry, control, human, replay, save, **kwargs):
        # The `inputs` keyword declares a schema; it must not replace the task
        # values used to reject scripts embedding user-supplied data.
        assert (await save('search', 'Search', 'async (inputs) => inputs.origin', inputs=declared)).error is None
        leaked = await save('leak', 'Search', "async () => 'Reykjavik'", inputs=declared)
        assert leaked.error.startswith('Parameterize input values before saving scripts') and 'Reykjavik' in leaked.error
        return 'done'
    monkeypatch.setattr(agent_session, 'drive_browser', drive)
    try:
        await session.ask('trains', {'origin': 'Reykjavik'})
    finally:
        session.store.close()


async def test_repair_stops_when_repaired_attempt_partially_acted(monkeypatch):
    generated = AsyncMock(return_value='async()=>{}')
    monkeypatch.setattr(runner, 'generate_program', generated)
    monkeypatch.setattr(runner, '_execute_reflex', AsyncMock(return_value=(
        False, f'SCRIPT EXECUTION FAILED\nEFFECTS: {runner.PARTIAL_EFFECTS} - changed')))
    page = SimpleNamespace(get_url=AsyncMock(return_value='https://x.test'), evaluate=AsyncMock(return_value='els'))
    browser = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page),
                              get_browser_state_summary=AsyncMock(return_value=SimpleNamespace(dom_state=object())))
    ok, detail, fixed = await runner._repair_reflex('async()=>{}', 'missing', {}, '', browser, None, max_attempts=3)
    assert (ok, fixed) == (False, None) and runner.PARTIAL_EFFECTS in detail
    generated.assert_awaited_once()  # no second whole-stage attempt over partial effects
