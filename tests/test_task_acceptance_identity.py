"""Consumer probes only; the integrated account authority checks remain blocked."""

import pytest

from tests.task_acceptance_support.client import Client
from tests.task_acceptance_support.peers import scripted_peer
from tests.task_acceptance_support.system import same_account_key_scope
from tests.task_acceptance_support.wire import PREFIX, no_store


@pytest.mark.parametrize('headers', [{}, {'Cache-Control': 'private'}, {'Cache-Control': 'no-cache'},
                                     {'Cache-Control': 'max-age=60'}])
def test_credential_no_store_oracle_rejects_cacheable_responses(headers):
    with pytest.raises(AssertionError):
        no_store(headers)


@pytest.mark.parametrize('value', ['no-store', 'private, no-store', 'NO-STORE, max-age=0'])
def test_credential_no_store_oracle_accepts_case_insensitive_directive(value):
    no_store({'cAcHe-CoNtRoL': value})


def test_same_account_key_scope_consumer_uses_shared_profiles_and_account_receipts():
    profiles = [{'profile_id': 'profile_a'}, {'profile_id': 'profile_b'}]
    shared = {'profile_id': 'profile_shared', 'label': 'scope-run:shared-authority', 'state': 'available',
              'created_at': '2030-01-01T00:00:00Z'}
    second_token = 'fp_live_synthetic_scope_only_000000001'
    headers = {'Cache-Control': 'no-store'}
    cases = [
        {'method': 'POST', 'path': '/api/v1/api-keys', 'status': 200,
         'response': {'id': 'key_second', 'key': second_token}, 'headers': headers},
        {'method': 'GET', 'path': PREFIX + '/profiles', 'status': 200,
         'expected_headers': {'Authorization': 'Bearer ' + second_token}, 'response': {'items': profiles}},
        *[{'method': 'POST', 'path': PREFIX + '/profiles', 'status': 201, 'response': shared,
           'request': {'label': 'scope-run:shared-authority', 'idempotency_key': 'scope-run:shared-profile'},
           'expected_headers': {'Authorization': 'Bearer ' + token}}
          for token in ['owner-token', second_token]],
        {'method': 'DELETE', 'path': PREFIX + '/profiles/profile_shared', 'status': 404,
         'expected_headers': {'Authorization': 'Bearer foreign-token'},
         'response': {'error': {'code': 'not_found', 'message': 'Not found', 'retryable': False}}},
        {'method': 'DELETE', 'path': PREFIX + '/profiles/profile_shared', 'status': 204,
         'expected_headers': {'Authorization': 'Bearer ' + second_token}, 'response': None},
        {'method': 'GET', 'path': PREFIX + '/profiles', 'status': 200,
         'expected_headers': {'Authorization': 'Bearer owner-token'}, 'response': {'items': profiles}},
        {'method': 'DELETE', 'path': '/api/v1/api-keys/key_second', 'status': 200,
         'response': {'status': 'deleted'}, 'headers': headers},
        {'method': 'GET', 'path': PREFIX + '/profiles', 'status': 401,
         'response': {'error': {'code': 'unauthenticated', 'message': 'Rejected', 'retryable': False}}},
    ]
    with scripted_peer(cases) as base:
        result = same_account_key_scope(Client(base, 'owner-token'), Client(base, 'dashboard-token'),
                                        Client(base, 'foreign-token'), ['profile_a', 'profile_b'], 'scope-run')
        assert result['same_account_keys'] == 2 and result['distinct_preserved_profiles'] == 2
