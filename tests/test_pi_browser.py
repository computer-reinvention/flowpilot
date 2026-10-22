from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import pi_browser, runner


async def test_stable_observation_and_navigation_guard(monkeypatch):
    page = SimpleNamespace(get_url=AsyncMock(return_value='https://example.com'), evaluate=AsyncMock(return_value='form'))
    browser = SimpleNamespace(
        get_current_page_url=page.get_url, agent_focus_target_id='tab-a',
        must_get_current_page=AsyncMock(return_value=page),
        get_browser_state_summary=AsyncMock(return_value=SimpleNamespace(dom_state=object())))
    execute = AsyncMock()
    async def act(*args):
        browser.agent_focus_target_id = 'popup'
        return True, 'submitted'
    execute.side_effect = act
    monkeypatch.setattr(runner, '_execute_reflex', execute)
    async def agent(task, system, tools, dispatch, **kwargs):
        await dispatch('observe', {})
        browser.get_browser_state_summary.assert_awaited_once_with(include_screenshot=False, cached=False)
        await dispatch('execute', {'script': 'async()=>{}'})
        with pytest.raises(RuntimeError, match='Page changed'):
            await dispatch('execute', {'script': 'async()=>{}'})
        assert execute.await_count == 1
        await dispatch('observe', {})
        await dispatch('execute', {'script': 'async()=>{}'})
        await dispatch('complete', {'verify': 'async()=>true', 'summary': 'done'})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(browser, 'task', {}, '', SimpleNamespace(checkpoint=AsyncMock()), None, None, None)


async def test_pi_cannot_report_success_without_completion_check(monkeypatch):
    monkeypatch.setattr(pi_browser, "run_pi", AsyncMock(return_value="Done!"))
    with pytest.raises(RuntimeError, match="without verified completion"):
        await pi_browser.drive_browser(None, "task", {}, "", SimpleNamespace(checkpoint=AsyncMock()),
                                       None, None, None)


async def test_pi_completion_checks_browser_and_returns_summary(monkeypatch):
    execute = AsyncMock(return_value=(True, "true"))
    monkeypatch.setattr(runner, "_execute_reflex", execute)
    async def agent(task, system, tools, dispatch, **kwargs):
        assert any(t["name"] == "human_help" and t["human"] for t in tools)
        await dispatch("complete", {"verify": "async () => !!document.querySelector('#receipt')", "summary": "Receipt visible"})
        with pytest.raises(ValueError, match="already completed"):
            await dispatch("execute", {"script": "async () => {}"})
    monkeypatch.setattr(pi_browser, "run_pi", agent)
    result = await pi_browser.drive_browser(None, "task", {}, "", SimpleNamespace(checkpoint=AsyncMock()),
                                            None, None, None)
    assert result == "Receipt visible"
    assert "#receipt" in execute.await_args.args[0]


async def test_exploration_tools_have_typed_schemas_and_dispatch(monkeypatch):
    called = []
    async def report_stage(title: str, url: str, note: str | None = None):
        called.append(title)
        return SimpleNamespace(model_dump=lambda: {"saved": True})
    async def agent(task, system, tools, dispatch, **kwargs):
        assert kwargs["role"] == "exploration"
        definition = next(t for t in tools if t["name"] == "report_stage")
        assert definition["inputSchema"]["required"] == ["title", "url"]
        await dispatch("report_stage", {"title": "Form", "url": "https://example.test"})
        await dispatch("finish_exploration", {})
    monkeypatch.setattr(pi_browser, "run_pi", agent)
    result = await pi_browser.drive_browser(None, "task", {}, "", SimpleNamespace(checkpoint=AsyncMock()),
                                            None, None, None, exploration_tools={"report_stage": report_stage})
    assert called == ["Form"] and result == "Exploration finished"


async def test_pi_preserves_handoff_and_reflex_metadata(monkeypatch):
    from browser_use import ActionResult
    human = AsyncMock()
    save = AsyncMock(return_value=ActionResult(extracted_content='saved'))
    confirmation = AsyncMock(return_value=ActionResult(extracted_content='User declined'))
    monkeypatch.setattr(runner, '_execute_reflex', AsyncMock(return_value=(True, 'true')))
    async def agent(task, system, tools, dispatch, **kwargs):
        definitions = {t['name']: t for t in tools}
        assert definitions['request_confirmation']['human']
        assert definitions['request_input_values']['human']
        assert 'pause_after_auth' in definitions['save_reflex']['inputSchema']['properties']
        await dispatch('human_help', {'reason':'OTP', 'category':'auth'})
        await dispatch('save_reflex', {'name':'login','description':'Login','script':'async()=>{}',
                                      'tags':['auth'],'pause_after_auth':True})
        result = await dispatch('request_confirmation', {'summary':'Order?'})
        assert 'declined' in result['extracted_content']
        await dispatch('complete', {'verify':'async()=>true','summary':'Stopped'})
    monkeypatch.setattr(pi_browser, 'run_pi', agent)
    await pi_browser.drive_browser(None, 'task', {}, '', SimpleNamespace(checkpoint=AsyncMock()), human,
                                   None, save, interaction_tools={'request_confirmation':confirmation})
    human.assert_awaited_once_with('OTP', 'auth')
    assert save.await_args.kwargs['pause_after_auth'] is True


def test_progress_summary_reveals_only_action_and_host():
    from backend.pi_browser import progress_summary
    assert progress_summary('navigate', {'url': 'https://m.uber.com/go?pickup=Secret+Street&otp=123456'}) == 'Opening m.uber.com'
    assert progress_summary('execute', {'script': 'async()=>"4111 1111"'}) == 'Working on the page'
    assert progress_summary('request_input_values', {'fields_json': '[]'}) is None
