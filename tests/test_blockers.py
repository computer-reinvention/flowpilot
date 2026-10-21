from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import blockers


@pytest.mark.parametrize('answer,expected', [
    ({'choice': 'auth', 'confidence': .95}, 'auth'),
    ({'choice': 'captcha', 'confidence': .6}, None),
    ({'choice': 'none', 'confidence': 1}, None),
    ({'choice': 'execute_shell', 'confidence': 1}, None),
    ({'choice': 'auth', 'confidence': float('nan')}, None),
    ({'choice': 'auth', 'confidence': 2}, None),
])
async def test_classifier_validates_confidence_and_choice(monkeypatch, answer, expected):
    monkeypatch.setenv('JEV_ENABLED', 'true')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'fake-key')
    response = MagicMock()
    response.json.return_value = {'answers': {'blocker': answer}}
    client = AsyncMock()
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(blockers.httpx, 'AsyncClient', lambda **kw: context)
    assert await blockers.classify_blocker('login required') == expected
    assert client.post.await_args.kwargs['json']['questions']['blocker']['type'] == 'choice'


async def test_disabled_classifier_makes_no_network_call(monkeypatch):
    monkeypatch.setenv('JEV_ENABLED', 'false')
    client = MagicMock()
    monkeypatch.setattr(blockers.httpx, 'AsyncClient', client)
    assert await blockers.classify_blocker('auth') is None
    client.assert_not_called()


async def test_service_failure_falls_back(monkeypatch):
    monkeypatch.setenv('JEV_ENABLED', 'true')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'fake-key')
    context = AsyncMock()
    context.__aenter__.side_effect = blockers.httpx.ConnectError('offline')
    monkeypatch.setattr(blockers.httpx, 'AsyncClient', lambda **kw: context)
    assert await blockers.classify_blocker('auth') is None
