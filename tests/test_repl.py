import pytest

from backend import repl


@pytest.mark.asyncio
async def test_stdin_answers_fields_and_confirmations(monkeypatch):
    answers = iter(['maybe', 'yes', 'Oslo'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert await repl.ask_stdin({'kind': 'confirmation', 'message': 'Book?', 'fields': []}) == {'confirmed': True}
    question = {'kind': 'inputs', 'message': 'Need city', 'fields': [{'name': 'city', 'label': 'City'}]}
    assert await repl.ask_stdin(question) == {'values': {'city': 'Oslo'}}


@pytest.mark.asyncio
async def test_stdin_browser_command_hands_over_the_browser(monkeypatch):
    monkeypatch.setattr('builtins.input', lambda prompt: '/browser')
    question = {'kind': 'inputs', 'message': 'Need code', 'fields': [{'name': 'otp'}]}
    assert await repl.ask_stdin(question) == {'use_browser': True}


def test_repl_is_a_client_of_the_session_core():
    from backend import session
    assert repl.BrowserSession is session.BrowserSession
    assert not hasattr(repl, 'UserStore')
