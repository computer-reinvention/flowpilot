"""Actual explore_flow callbacks through Pi dispatch, using local Chromium only."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend import explorer, observation, pi_browser, runner
from backend.observation import action_observation, create_observation
from backend.task_contracts import ErrorCode, TaskFault
from backend.task_settings import TaskSettings
from scripts.smoke_observation import synthetic_browser


CANARY = 'EXPLORER-secret-canary-7319'


@pytest_asyncio.fixture
async def fixture_browser():
    async with synthetic_browser() as value:
        yield value


def setup_explorer(monkeypatch, fixture_browser, *, inputs='[]', script='async()=>true'):
    browser, page, url = fixture_browser
    policy = create_observation(TaskSettings(readiness_window_seconds=0.05, readiness_timeout_seconds=0.5))
    flow = SimpleNamespace(id='fixture-flow', domain='127.0.0.1', task='Synthetic explorer',
                           entry_url=url, inputs=inputs)
    db = SimpleNamespace(update_flow=AsyncMock(), get_flow=AsyncMock(return_value=flow),
                         upsert_reflex=AsyncMock(), list_reflexes_for_domain=AsyncMock(return_value=[]),
                         get_reflex=AsyncMock(return_value={'script': script}))
    monkeypatch.setattr(explorer, '_db', db)
    monkeypatch.setattr(explorer, 'Browser', lambda **_: browser)
    monkeypatch.setattr(explorer, '_get_exploration_llm', lambda: None)
    monkeypatch.setattr(explorer, '_get_fast_llm', lambda: None)
    monkeypatch.setenv('AGENT_DRIVER', 'pi')
    # Only mock the CDP discovery HTTP request; the callbacks below use the actual
    # fixture Browser, Actor, CDP evaluation, policy and Pi dispatch implementation.
    response = SimpleNamespace(status=200, read=lambda: b'{"webSocketDebuggerUrl":"ws://localhost/devtools/browser/fixture"}')
    monkeypatch.setattr(explorer.http.client, 'HTTPConnection',
                        lambda *_a, **_k: SimpleNamespace(request=lambda *_a, **_k: None,
                                                         getresponse=lambda: response))
    events = []
    def trace(name, **fields):
        events.append([name, fields])
    for module in (explorer, runner, observation):
        monkeypatch.setattr(module, 'trace', trace)

    final_state = []
    async def capture(_):
        final_state.extend(json.loads(await page.evaluate('() => [window.right,window.wrong]')))
    async def run(agent):
        monkeypatch.setattr(pi_browser, 'run_pi', agent)
        await explorer.explore_flow(flow, 'lane007-fixture', 'http://127.0.0.1:1',
                                    observation_policy=policy, on_session_capture=capture)
    return SimpleNamespace(browser=browser, page=page, policy=policy, flow=flow, db=db,
                           events=events, final_state=final_state, run=run)


async def test_actual_generation_uses_exact_dispatch_snapshot_and_saves(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser)
    shown = None
    async def generate(llm, context):
        origin = action_observation.get()
        assert origin[0] is env.browser and origin[1] is env.policy
        assert origin[2].model_dump(mode='json') == shown
        assert context['elements'] == shown['elements']
        assert context['url'] == shown['url']
        assert 'fixture-secret' not in context['elements']
        return 'async()=>true'
    monkeypatch.setattr(explorer, 'generate_program', generate)
    async def agent(task, system, tools, dispatch, **kwargs):
        nonlocal shown
        shown = await dispatch('observe', {})
        # Deliberately stale stage metadata must not replace the snapshot URL.
        await dispatch('report_stage', dict(title='Synthetic', url='http://metadata.invalid/old'))
        result = await dispatch('generate_and_save_reflex', dict(name='stage', description='Compile this stage'))
        assert not result.get('error')
        await dispatch('finish_exploration', {})
    await env.run(agent)
    env.db.upsert_reflex.assert_awaited_once()
    assert env.db.upsert_reflex.await_args.kwargs['script'] == 'async()=>true'
    assert action_observation.get() is None


async def test_generation_latency_same_url_replacement_rejects_save_and_effect(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser)
    async def generate(*_):
        await env.page.evaluate('''() => { const replacement=action.cloneNode(true);
          replacement.onclick=()=>window.wrong++; action.replaceWith(replacement); }''')
        return 'async()=>{action.click();return true;}'
    monkeypatch.setattr(explorer, 'generate_program', generate)
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        with pytest.raises(TaskFault) as exc:
            await dispatch('generate_and_save_reflex', dict(name='stage', description='Compile'))
        assert exc.value.error.code == ErrorCode.STALE_GENERATION
        # Unknown/stale generation cannot be followed by a dependent test queue.
        with pytest.raises(RuntimeError, match='observe'):
            await dispatch('test_reflex', {'name': 'stage'})
        await dispatch('finish_exploration', {})
    await env.run(agent)
    env.db.upsert_reflex.assert_not_awaited()
    assert env.final_state == [0, 0]


async def test_generation_rejects_stale_snapshot_before_provider_or_second_dump(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser)
    generate = AsyncMock(return_value='async()=>true')
    monkeypatch.setattr(explorer, 'generate_program', generate)
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        await env.page.evaluate("() => {result.textContent='Changed $20';}")
        with pytest.raises(TaskFault):
            await dispatch('generate_and_save_reflex', dict(name='stage', description='Compile'))
        await dispatch('finish_exploration', {})
    await env.run(agent)
    generate.assert_not_awaited()
    env.db.upsert_reflex.assert_not_awaited()


@pytest.mark.parametrize('outcome', ['success', 'false', 'throw'])
async def test_actual_test_callback_redacts_canary_inputs_results_and_errors(monkeypatch, fixture_browser, outcome, caplog):
    script = {
        'success': 'async(inputs)=>{window.right++;return inputs.secret;}',
        'false': 'async(inputs)=>({success:false,secret:inputs.secret})',
        'throw': 'async(inputs)=>{throw new Error(inputs.secret);}',
    }[outcome]
    env = setup_explorer(monkeypatch, fixture_browser,
                         inputs=json.dumps([dict(name='secret', type='text', placeholder=CANARY)]), script=script)
    outputs = []
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        outputs.append(await dispatch('test_reflex', {'name': CANARY}))
        await dispatch('finish_exploration', {})
    await env.run(agent)
    recorded = json.dumps([env.events, outputs, env.db.update_flow.await_args_list], default=str) + caplog.text
    assert CANARY not in recorded
    assert script not in json.dumps(env.events)
    assert 'Total $5' not in json.dumps(env.events)
    assert bool(outputs[0].get('error')) == (outcome != 'success')
    assert env.final_state == ([1, 0] if outcome == 'success' else [0, 0])


async def test_actual_test_callback_keeps_dispatch_snapshot_across_db_await(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser, script='async()=>{action.click();return true;}')
    async def lookup(*_a, **_k):
        await env.page.evaluate('''() => { const replacement=action.cloneNode(true);
          replacement.onclick=()=>window.wrong++; action.replaceWith(replacement); }''')
        return {'script': 'async()=>{action.click();return true;}'}
    env.db.get_reflex.side_effect = lookup
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        with pytest.raises(TaskFault) as exc:
            await dispatch('test_reflex', {'name': 'stage'})
        assert exc.value.error.code == ErrorCode.STALE_GENERATION
        await dispatch('finish_exploration', {})
    await env.run(agent)
    assert env.final_state == [0, 0]


async def test_generation_exception_canaries_do_not_escape_callback_or_logs(monkeypatch, fixture_browser, caplog):
    env = setup_explorer(monkeypatch, fixture_browser)
    async def generate(*_):
        raise RuntimeError(CANARY)
    monkeypatch.setattr(explorer, 'generate_program', generate)
    outputs = []
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        outputs.append(await dispatch('generate_and_save_reflex', dict(name=CANARY, description=CANARY)))
        await dispatch('finish_exploration', {})
    await env.run(agent)
    assert outputs[0].get('error')
    assert CANARY not in json.dumps([env.events, outputs, env.db.update_flow.await_args_list], default=str) + caplog.text
    env.db.upsert_reflex.assert_not_awaited()


@pytest.mark.parametrize('failure', ['save', 'lookup', 'input_schema'])
async def test_entire_reflex_callback_redacts_database_and_schema_failures(monkeypatch, fixture_browser, failure, caplog):
    env = setup_explorer(monkeypatch, fixture_browser)
    monkeypatch.setattr(explorer, 'generate_program', AsyncMock(return_value='async()=>true'))
    if failure == 'save':
        env.db.upsert_reflex.side_effect = RuntimeError(CANARY)
    elif failure == 'lookup':
        env.db.get_reflex.side_effect = RuntimeError(CANARY)
    else:
        env.flow.inputs = json.dumps([{'name': CANARY, 'type': CANARY, 'placeholder': CANARY}, None])
    outputs = []
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        if failure == 'save':
            outputs.append(await dispatch('generate_and_save_reflex', dict(name=CANARY, description=CANARY)))
        else:
            outputs.append(await dispatch('test_reflex', {'name': CANARY}))
        await dispatch('finish_exploration', {})
    await env.run(agent)
    assert outputs[0].get('error')
    assert CANARY not in json.dumps([env.events, outputs, env.db.update_flow.await_args_list], default=str) + caplog.text
    assert env.final_state == [0, 0]


async def test_native_browser_use_registry_callbacks_retain_local_compatibility(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser, inputs=json.dumps([
        dict(name='secret', type='text', placeholder=CANARY)]), script='async(inputs)=>inputs.secret')
    monkeypatch.setenv('AGENT_DRIVER', 'browser-use')
    generated = []
    async def generate(llm, context):
        assert action_observation.get() is None
        assert 'Total $5' in context['elements'] and 'fixture-secret' not in context['elements']
        generated.append(context)
        return 'async()=>true'
    monkeypatch.setattr(explorer, 'generate_program', generate)
    outputs = []
    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs['tools']

        async def run(self, **_):
            # Actual browser-use normalized action wrappers/parameter models,
            # not the unwrapped Pi callback path.
            outputs.append(await self.tools.registry.execute_action(
                'generate_and_save_reflex', {'name': 'stage', 'description': CANARY}, browser_session=env.browser))
            outputs.append(await self.tools.registry.execute_action(
                'test_reflex', {'name': 'stage'}, browser_session=env.browser))
    monkeypatch.setattr(explorer, 'Agent', Agent)
    await env.run(AsyncMock())
    assert len(generated) == 1
    assert all(not result.error for result in outputs)
    assert CANARY not in json.dumps([env.events, [r.model_dump() for r in outputs]], default=str)
    env.db.upsert_reflex.assert_awaited_once()


async def test_outer_exploration_error_persistence_does_not_echo_canary(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser)
    async def agent(*_a, **_k):
        raise RuntimeError(CANARY)
    with pytest.raises(RuntimeError) as exc:
        await env.run(agent)
    assert CANARY not in str(exc.value)
    assert CANARY not in json.dumps(env.db.update_flow.await_args_list, default=str)
    assert any(call.args[1].get('exploration_error') == 'Exploration failed; details withheld'
               for call in env.db.update_flow.await_args_list)


async def test_successful_generation_trace_and_summary_omit_entire_prompt_and_script(monkeypatch, fixture_browser):
    env = setup_explorer(monkeypatch, fixture_browser,
                         inputs=json.dumps([dict(name='secret', type='text', placeholder=CANARY)]))
    script = 'async()=>"' + CANARY + '"'
    async def generate(llm, context):
        assert CANARY in json.dumps(context)
        return script
    monkeypatch.setattr(explorer, 'generate_program', generate)
    outputs = []
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        outputs.append(await dispatch('generate_and_save_reflex',
                                      dict(name=CANARY, description=CANARY, tags=[CANARY])))
        await dispatch('finish_exploration', {})
    await env.run(agent)
    assert not outputs[0].get('error')
    assert CANARY not in json.dumps([env.events, outputs])
    # Script memory is the explicitly requested output, unlike safe telemetry.
    assert env.db.upsert_reflex.await_args.kwargs['script'] == script
