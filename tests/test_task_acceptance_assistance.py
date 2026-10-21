"""Amendment consumers against strict HTTP peers, not production-peer passes."""

import copy

import pytest

from tests.task_acceptance_support.assistance import expired, metadata, mint
from tests.task_acceptance_support.client import Client
from tests.task_acceptance_support.peers import scripted_peer
from tests.task_acceptance_support.wire import assistance_state, fixture, ordinary_safe


def test_public_mint_state_consumer_accepts_pause_generation_nine_without_lease_field():
    cases = {case['name']: copy.deepcopy(case) for case in fixture('exchanges.json')['cases']}
    interaction = cases['browser_pause']['response']['interaction']
    interaction['generation'] = 9
    mint_case = cases['mint_assistance']
    mint_case['request']['generation'] = 9
    mint_case['response']['generation'] = 9
    state = copy.deepcopy(fixture('viewer_commands.json')['state'])
    state['interaction'] = interaction
    state_case = {'method': 'GET', 'path': mint_case['response']['assistance_url'] + '/state',
                  'status': 200, 'response': state,
                  'expected_headers': {'Authorization': None},
                  'headers': fixture('policy.json')['assistance']['headers']}
    with scripted_peer([mint_case, cases['browser_pause'], state_case]) as base:
        owner = Client(base, 'synthetic-account-token')
        grant = mint(owner, cases['admit']['response'], interaction)
        assert metadata(owner, grant, interaction, state['url']) == {'viewport_width': 1280, 'viewport_height': 720}
    assert 'lease_generation' not in state and 'execution_generation' not in state


@pytest.mark.parametrize('field', ['lease_generation', 'execution_generation', 'lease', 'principal'])
def test_execution_identity_is_not_public_metadata_or_task_status(field):
    state = copy.deepcopy(fixture('viewer_commands.json')['state'])
    state[field] = 1
    with pytest.raises(AssertionError):
        assistance_state(state, state['interaction'])
    with pytest.raises(AssertionError):
        ordinary_safe({'task_id': 'task_demo', field: 1})


@pytest.mark.parametrize('mutation', ['generation', 'url', 'width', 'height'])
def test_metadata_oracle_rejects_wrong_pause_or_invalid_current_viewport(mutation):
    state = copy.deepcopy(fixture('viewer_commands.json')['state'])
    expected = copy.deepcopy(state['interaction'])
    if mutation == 'generation':
        state['interaction']['generation'] += 1
    elif mutation == 'url':
        state['url'] = 'not-a-url'
    elif mutation == 'width':
        state['viewport_width'] = 0
    else:
        state['viewport_height'] = 16385
    with pytest.raises(AssertionError):
        assistance_state(state, expected)


def test_expired_capability_consumer_requires_state_and_late_input_failures():
    expired_case = next(case for case in fixture('exchanges.json')['cases'] if case['name'] == 'assistance_expired')
    grant = {'assistance_url': expired_case['path']}
    state = {**expired_case, 'path': grant['assistance_url'] + '/state'}
    late_input = {**expired_case, 'method': 'POST', 'path': grant['assistance_url'] + '/input',
                  'request': {'kind': 'release'}}
    with scripted_peer([state, late_input]) as base:
        expired(Client(base, 'account-token-must-not-be-forwarded'), grant)
