"""Actual browser-use Actor/CDP against synthetic loopback Chromium pages."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend import pi_browser, runner
from backend.observation import create_observation
from backend.task_contracts import ErrorCode, TaskFault
from backend.task_settings import TaskSettings
from scripts.smoke_observation import synthetic_browser


@pytest_asyncio.fixture
async def local_browser():
    async with synthetic_browser() as value:
        yield value


def policy(timeout=1.5):
    return create_observation(TaskSettings(readiness_window_seconds=0.08,
                                          readiness_timeout_seconds=timeout))


async def test_delayed_hydration_with_perpetual_traffic_and_clock(local_browser):
    browser, page, _ = local_browser
    await page.evaluate('''() => {
      document.body.setAttribute('aria-busy','true'); action.disabled=true;
      setInterval(()=>fetch('/noise').catch(()=>{}),30);
      setTimeout(()=>{ action.disabled=false; result.textContent='Ready $9';
        document.body.removeAttribute('aria-busy'); },220);
    }''')
    p = policy()
    start = time.monotonic()
    a = await p.observe(browser)
    assert a.readiness == 'ready'
    assert time.monotonic() - start >= 0.22
    assert 'disabled=False' in a.elements
    assert 'fixture-secret' not in a.model_dump_json()
    await asyncio.sleep(0.04)
    await p.require_fresh(browser, a)  # explicit timestamp churn is benign
    await page.evaluate("() => { result.textContent='Ready $99'; }")
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, a)
    b = await p.observe(browser)
    assert a.structural_fingerprint == b.structural_fingerprint
    assert a.progress_fingerprint != b.progress_fingerprint


@pytest.mark.parametrize('mutation', [
    "const el=action.cloneNode(true); el.onclick=()=>window.wrong++; action.replaceWith(el);",
    "const el=document.createElement('button'); el.textContent='Submit'; el.onclick=()=>window.wrong++; document.querySelector('main').prepend(el);",
    "action.disabled=true;",
    "field.value='changed';",
    "document.open(); document.write('<body><button onclick=\"window.wrong++\">Submit</button>'); document.close();",
])
async def test_same_url_change_cannot_click_wrong_target(local_browser, mutation):
    browser, page, _ = local_browser
    p = policy()
    a = await p.observe(browser)
    await page.evaluate('() => { ' + mutation + ' }')
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, a)
        await page.evaluate("() => document.querySelector('button').click()")
    assert await page.evaluate('() => window.wrong || 0') == '0'


async def test_navigation_same_url_reload_changes_document(local_browser):
    browser, page, url = local_browser
    p = policy()
    a = await p.observe(browser)
    await page.goto(url)
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, a)
    assert (await p.observe(browser)).document_id != a.document_id


async def test_duplicate_labels_frames_and_ambiguity(local_browser):
    browser, page, _ = local_browser
    await page.evaluate('''() => {
      const one=document.createElement('button'); one.textContent='Duplicate';
      const two=one.cloneNode(true); document.body.append(one,two);
      const frame=document.createElement('iframe');
      frame.srcdoc='<button id="inside">Frame control</button>'; document.body.append(frame);
    }''')
    p = policy()
    a = await p.observe(browser)
    assert a.readiness == 'ready' and 'Frame control' in a.elements
    duplicate_index = next(i for i, line in enumerate(a.elements.splitlines()) if 'Duplicate' in line)
    with pytest.raises(TaskFault):
        await p.resolve_reference(browser, a, f'r{duplicate_index}')
    await page.evaluate("() => document.querySelector('iframe').contentDocument.querySelector('button').disabled=true")
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, a)


async def test_unknown_cross_origin_and_never_ready_deadline(local_browser):
    browser, page, url = local_browser
    p = policy(timeout=0.4)
    await page.evaluate("() => document.body.setAttribute('aria-busy','true')")
    start = time.monotonic()
    a = await p.observe(browser)
    assert a.readiness == 'timeout'
    assert time.monotonic() - start < 1
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, a)
    await page.evaluate('''() => {
      document.body.removeAttribute('aria-busy'); const f=document.createElement('iframe');
      f.sandbox='allow-scripts'; f.srcdoc='<button>Opaque frame</button>'; document.body.append(f);
    }''')
    assert (await p.observe(browser)).readiness == 'unknown'


async def test_pi_code_mode_batch_rejects_stale_next_call_and_checks_completion(local_browser, monkeypatch):
    browser, page, url = local_browser
    p = policy()
    args = dict(script="async()=>{ action.click(); return 'submitted'; }",
                precondition="async()=>!action.disabled && result.textContent==='Total $5'",
                postcondition="async()=>result.textContent==='Receipt 1'")

    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        result = await dispatch('execute', args)
        assert result['success']
        await page.evaluate('''() => { const el=action.cloneNode(true);
          el.onclick=()=>window.wrong++; action.replaceWith(el); }''')
        with pytest.raises((RuntimeError, TaskFault)):
            await dispatch('execute', {**args, 'script': 'async()=>action.click()'})
        await dispatch('observe', {})
        with pytest.raises(TaskFault) as exc:
            await dispatch('execute', args)
        assert exc.value.error.code == ErrorCode.NO_PROGRESS
        await dispatch('complete', {'verify': 'async()=>window.right===1 && window.wrong===0', 'summary': 'Checked receipt'})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    answer = await pi_browser.drive_browser(browser, 'synthetic submit', {}, url,
                                           SimpleNamespace(checkpoint=AsyncMock()), None, None, None,
                                           observation_policy=p)
    assert answer == 'Checked receipt'
    assert await page.evaluate('() => [window.right,window.wrong]') == '[1, 0]'


async def test_pi_loop_recovery_after_noop_and_observations(local_browser, monkeypatch):
    browser, page, url = local_browser
    p = policy()
    no_op = dict(script='async()=>{window.noops++;return false;}',
                 precondition='async()=>true', postcondition='async()=>true')

    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        assert not (await dispatch('execute', no_op))['success']
        await dispatch('observe', {})
        with pytest.raises(TaskFault) as exc:
            await dispatch('execute', no_op)
        assert exc.value.error.code == ErrorCode.NO_PROGRESS
        # Recovery is a distinct checked action, not another form submission.
        await dispatch('execute', dict(script="async()=>{result.textContent='Recovered';return true;}",
                                      precondition='async()=>window.noops===1',
                                      postcondition="async()=>result.textContent==='Recovered'"))
        await dispatch('complete', dict(verify='async()=>window.noops===1 && window.right===0', summary='Recovered without submit'))
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(browser, 'recover', {}, url, SimpleNamespace(checkpoint=AsyncMock()),
                                   None, None, None, observation_policy=p)
    assert await page.evaluate('() => window.noops') == '1'


async def test_repair_rejects_state_changed_during_generation(local_browser, monkeypatch):
    browser, page, url = local_browser
    async def generate(*_):
        await page.evaluate("() => { action.replaceWith(action.cloneNode(true)); }")
        return 'async()=>{document.querySelector("button").click();return true;}'
    monkeypatch.setattr(runner, 'generate_program', generate)
    with pytest.raises(TaskFault):
        await runner._repair_reflex('async()=>false', 'synthetic failure', {}, url, browser, None,
                                    observation_policy=policy())
    assert await page.evaluate('() => window.right') == '0'


async def test_exploration_callback_preserves_origin_snapshot(local_browser, monkeypatch):
    from browser_use import ActionResult
    browser, page, url = local_browser
    async def test_reflex(name: str):
        await page.evaluate('() => { const el=action.cloneNode(true); el.onclick=()=>window.wrong++; action.replaceWith(el); }')
        # Historical callbacks do not accept a snapshot parameter. Their runner
        # still inherits this dispatch's observation through the scoped context.
        await runner._execute_reflex('async()=>action.click()', {}, url, browser)
        return ActionResult(extracted_content='unexpected')
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        with pytest.raises(TaskFault):
            await dispatch('test_reflex', {'name': 'replace'})
        await dispatch('finish_exploration', {})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(browser, 'explore', {}, url, SimpleNamespace(checkpoint=AsyncMock()),
                                   None, None, None, exploration_tools={'test_reflex': test_reflex},
                                   observation_policy=policy())
    assert await page.evaluate('() => window.wrong') == '0'


async def test_exploration_no_progress_budget_and_human_wait_do_not_reset_evidence(local_browser, monkeypatch):
    from browser_use import ActionResult
    browser, page, url = local_browser
    calls = 0
    async def test_noop(name: str):
        nonlocal calls
        calls += 1
        return ActionResult(extracted_content='No change')
    async def agent(task, system, tools, dispatch, **kwargs):
        for _ in range(3):
            await dispatch('observe', {})
            await dispatch('test_noop', {'name': 'noop'})
            await asyncio.sleep(0.01)  # deliberate waits/observation do not count
        await dispatch('observe', {})
        with pytest.raises(TaskFault) as exc:
            await dispatch('test_noop', {'name': 'noop'})
        assert exc.value.error.code == ErrorCode.NO_PROGRESS
        await dispatch('finish_exploration', {})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(browser, 'explore', {}, url, SimpleNamespace(checkpoint=AsyncMock()),
                                   None, None, None, exploration_tools={'test_noop': test_noop},
                                   observation_policy=policy())
    assert calls == 3


async def test_completion_requires_true_postcondition_and_ready_state(local_browser, monkeypatch):
    browser, page, url = local_browser
    async def agent(task, system, tools, dispatch, **kwargs):
        with pytest.raises(ValueError):
            await dispatch('complete', {'verify': 'async()=>false', 'summary': 'Must not finish'})
        await page.evaluate("() => document.body.setAttribute('aria-busy','true')")
        with pytest.raises(TaskFault):
            await dispatch('complete', {'verify': 'async()=>true', 'summary': 'Must not finish'})
        await page.evaluate("() => document.body.removeAttribute('aria-busy')")
        await dispatch('complete', {'verify': 'async()=>window.right===0', 'summary': 'Checked'})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    result = await pi_browser.drive_browser(browser, 'check', {}, url, SimpleNamespace(checkpoint=AsyncMock()),
                                           None, None, None, observation_policy=policy(timeout=0.4))
    assert result == 'Checked'


async def test_volatile_optout_does_not_hide_result_or_control_state(local_browser):
    browser, page, _ = local_browser
    await page.evaluate("() => { result.setAttribute('data-observation-volatile','timestamp'); action.setAttribute('data-observation-volatile','timestamp'); }")
    p = policy()
    before = await p.observe(browser)
    await page.evaluate("() => { result.textContent='Price $200'; }")
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, before)
    current = await p.observe(browser)
    await page.evaluate("() => { action.textContent='Changed action'; }")
    with pytest.raises(TaskFault):
        await p.require_fresh(browser, current)
