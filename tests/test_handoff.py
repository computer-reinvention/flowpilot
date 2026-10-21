import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.handoff import HandoffServer


def test_application_webhook_retains_original_handoff_contract(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, 'PROFILES_DIR', tmp_path / 'profiles')
    from backend.main import _build_webhook_payload, BASE_URL
    event = _build_webhook_payload('run.paused', 'run-1', 'paused_for_hitl',
                                   '[auth] Login', 'sandbox-1', 'alice', 'auth')
    assert event['event'] == 'run.paused'
    assert event['run_id'] == 'run-1'
    assert event['profile_id'] == 'alice'
    assert event['category'] == 'auth'
    assert event['browser_cdp_url'] == f'{BASE_URL}/ws/browser/sandbox-1'
    assert event['resume_url'] == f'{BASE_URL}/api/v1/runs/run-1/resume'


@pytest.mark.asyncio
async def test_link_resolution_expiry_and_confirmation():
    delivered = asyncio.Queue()
    server = HandoffServer(SimpleNamespace(pages=[]), 'ws://browser', 'alice', delivered.put)
    server.base = 'http://test'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=server.base) as client:
        task = asyncio.create_task(server.request('Buy?', kind='confirmation'))
        event = await delivered.get()
        assert event['event'] == 'run.paused'
        assert event['browser_cdp_url'] == 'ws://browser'
        assert not task.done()
        assert (await client.get('/help/other-user')).status_code == 410
        assert (await client.get(event['assistance_url'])).status_code == 200
        assert (await client.post(event['resume_url'], json={})).status_code == 422
        assert not task.done()
        assert (await client.post(event['resume_url'], json={'confirmed': False})).status_code == 200
        assert await task == {'confirmed': False}
        assert (await client.post(event['resume_url'], json={'confirmed': True})).status_code == 410


@pytest.mark.asyncio
async def test_input_resolution_and_cancellation():
    delivered = asyncio.Queue()
    server = HandoffServer(SimpleNamespace(pages=[]), 'ws://browser', 'alice', delivered.put)
    server.base = 'http://test'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=server.base) as client:
        task = asyncio.create_task(server.request('Address?', kind='inputs', fields=[{'name':'address'}]))
        event = await delivered.get()
        assert (await client.post(event['resume_url'], json={'values': {}})).status_code == 422
        await client.post(event['resume_url'], json={'values': {'address': 'Test'}})
        assert (await task)['values']['address'] == 'Test'
        task = asyncio.create_task(server.request('Login'))
        event = await delivered.get()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await client.get(event['assistance_url'])).status_code == 410
        assert server.pending is None


@pytest.mark.asyncio
async def test_browser_actions_only_during_pause():
    delivered = asyncio.Queue()
    page = SimpleNamespace(url='https://fixture.test', screenshot=AsyncMock(return_value=b'png'),
                           keyboard=SimpleNamespace(insert_text=AsyncMock()))
    server = HandoffServer(SimpleNamespace(pages=[page]), 'ws://browser', 'alice', delivered.put)
    server.base = 'http://test'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=server.base) as client:
        task = asyncio.create_task(server.request('Login'))
        event = await delivered.get()
        url = event['assistance_url']
        assert (await client.get(url+'/screen')).content == b'png'
        assert (await client.post(url+'/action', json={'kind':'type','text':'123456'})).status_code == 200
        page.keyboard.insert_text.assert_awaited_once_with('123456')
        await client.post(url+'/cancel')
        with pytest.raises(RuntimeError, match='cancelled'):
            await task
        assert (await client.post(url+'/action', json={'kind':'type','text':'bad'})).status_code == 410
