import copy

import pytest

from tests.task_acceptance_support.client import Client, local_url
from tests.task_acceptance_support.peers import scripted_peer
from tests.task_acceptance_support.wire import admission, error, fixture, ordinary_safe, view


CASES = fixture("exchanges.json")["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_independent_frozen_wire_consumer(case):
    with scripted_peer([case]) as url:
        reply = Client(url).request(case["method"], case["path"],
                                    case.get("request") if case["method"] != "GET" else None)
        assert reply.status == case["status"] and reply.body == case["response"]
        if reply.status >= 400:
            error(reply.status, reply.body, reply.headers)
        elif case.get("response_model") == "TaskAdmission":
            admission(reply.body)
        elif case.get("response_model") == "TaskView":
            view(reply.body)
            ordinary_safe(reply.body)


def test_retry_fixture_is_same_receipt_and_decline_is_explicit():
    cases = {case["name"]: case for case in CASES}
    assert cases["admit"]["request"] == cases["duplicate_admit"]["request"]
    assert cases["admit"]["response"] == cases["duplicate_admit"]["response"]
    assert cases["decline"]["request"]["confirmed"] is False
    policy = fixture("policy.json")
    assert policy["authentication"]["identity_headers_trusted"] is False
    assert policy["restart"]["automatic_website_replay"] is False
    assert policy["capacity"]["paused_browser_counts"] is True
    assert policy["assistance"]["ordinary_status_contains_link"] is False


@pytest.mark.parametrize("mutation", ["terminal", "revision", "interaction", "error", "result", "owner"])
def test_oracle_rejects_broken_state(mutation):
    original = next(case["response"] for case in CASES if case["name"] == "completed")
    broken = copy.deepcopy(original)
    if mutation == "terminal":
        broken["state"] = "running"
        broken.pop("result")
        broken["revision"] += 1
    elif mutation == "revision":
        broken["revision"] = 0
    elif mutation == "interaction":
        broken["interaction"] = {"kind": "browser"}
    elif mutation == "error":
        broken["error"] = {"code": "provider_failure"}
    elif mutation == "result":
        broken.pop("result")
    else:
        broken["profile_id"] = "foreign"
    with pytest.raises(AssertionError):
        view(broken, original)


@pytest.mark.parametrize("field", ["assistance_url", "cdp_url", "password", "otp", "values", "api_key"])
def test_oracle_rejects_secret_fields(field):
    with pytest.raises(AssertionError):
        ordinary_safe({"nested": [{field: "synthetic"}]})


@pytest.mark.parametrize("url", ["http://example.com", "http://169.254.169.254", "http://127.0.0.1@evil.test", "file:///etc/passwd", "http://localhost/?token=x"])
def test_client_rejects_nonlocal_targets(url):
    with pytest.raises((AssertionError, ValueError)):
        local_url(url)
