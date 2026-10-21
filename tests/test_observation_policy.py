import asyncio
import copy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.observation import ProgressGuard, create_observation
from backend.task_contracts import ErrorCode, TaskFault
from backend.task_settings import TaskSettings


def policy(**kwargs):
    return create_observation(TaskSettings(readiness_window_seconds=0.01,
                                          readiness_timeout_seconds=0.15, **kwargs))


def raw():
    return dict(url='http://localhost/', docs=[['top', 'document-a']],
                controls=[dict(identity=['top', 'BUTTON', 'save'], status=[False, '', '', '', False, ''], label='Save')],
                content=[['top', 'Total $5']], nodes=['node-a'], busy=False, unknown=False)


def browser(value):
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=lambda _: copy.deepcopy(value)))
    return SimpleNamespace(agent_focus_target_id='tab-a', must_get_current_page=AsyncMock(return_value=page))


async def test_immutable_coherent_snapshot_and_secret_free_dump():
    value = raw()
    value['controls'][0]['status'][-1] = 'password-never-persist'
    p = policy()
    b = browser(value)
    a = await p.observe(b)
    assert a.readiness == 'ready'
    assert a.references[0].snapshot_id == a.snapshot_id
    assert 'password-never-persist' not in a.model_dump_json()
    with pytest.raises(ValueError):
        a.readiness = 'timeout'
    with pytest.raises(FrozenInstanceError):
        p.settings = TaskSettings()
    await p.require_fresh(b, a)
    assert (await p.observe(b)).snapshot_id != a.snapshot_id


@pytest.mark.parametrize('change', ['url', 'document', 'node', 'order', 'value', 'disabled', 'text', 'target'])
async def test_freshness_is_not_structural_identity(change):
    value = raw()
    p, b = policy(), browser(value)
    before = await p.observe(b)
    if change == 'url':
        value['url'] += 'next'
    elif change == 'document':
        value['docs'][0][-1] = 'document-b'
    elif change == 'node':
        value['nodes'][0] = 'replacement'
    elif change == 'order':
        value['nodes'].append('extra')
    elif change == 'value':
        value['controls'][0]['status'][-1] = 'new value'
    elif change == 'disabled':
        value['controls'][0]['status'][0] = True
    elif change == 'text':
        value['content'] = [['top', 'Total $99']]
    else:
        b.agent_focus_target_id = 'tab-b'
    with pytest.raises(TaskFault) as exc:
        await p.require_fresh(b, before)
    assert exc.value.error.code == ErrorCode.STALE_GENERATION
    after = await p.observe(b)
    assert after.structural_fingerprint == before.structural_fingerprint
    if change in ('value', 'disabled', 'text'):
        assert after.progress_fingerprint != before.progress_fingerprint


async def test_ambiguity_rejected_not_first_match():
    value = raw()
    value['controls'] *= 2
    value['nodes'].append('node-b')
    p, b = policy(), browser(value)
    observation = await p.observe(b)
    with pytest.raises(TaskFault, match='stale_generation'):
        await p.resolve_reference(b, observation, 'r0')


async def test_timeout_and_unknown_are_not_ready():
    p, value = policy(), raw()
    value['busy'] = True
    b = browser(value)
    observation = await p.observe(b)
    assert observation.readiness == 'timeout'
    with pytest.raises(TaskFault):
        await p.require_fresh(b, observation)
    value['unknown'] = True
    assert (await p.observe(b)).readiness == 'unknown'


async def test_deadline_and_cancellation_bound_hung_capture():
    b = browser(raw())
    async def hung():
        await asyncio.sleep(10)
    b.must_get_current_page.side_effect = hung
    with pytest.raises(TaskFault) as exc:
        await policy().observe(b)
    assert exc.value.error.code == ErrorCode.READINESS_TIMEOUT
    task = asyncio.create_task(policy().observe(b))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_progress_is_task_local_and_observation_does_not_reset_budget():
    p, b = policy(), browser(raw())
    guard = ProgressGuard(2)
    for _ in range(2):
        guard.before('noop', await p.observe(b))
    with pytest.raises(TaskFault) as exc:
        guard.before('noop', await p.observe(b))
    assert exc.value.error.code == ErrorCode.NO_PROGRESS
    guard.before('repair', await p.observe(b))
    ProgressGuard(2).before('noop', await p.observe(b))
