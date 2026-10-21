"""Black-box local-service probes. No implementation imports or in-process clients."""

import json
import secrets
from concurrent.futures import ThreadPoolExecutor

from .client import Client
from .wire import PREFIX, admission, ordinary_safe, view


def onboard(base, label):
    password = secrets.token_urlsafe(24)
    email = f"{label}@acceptance.invalid"
    anonymous = Client(base)
    signup = anonymous.request("POST", "/api/auth/signup", {
        "email": email, "password": password, "name": label})
    assert signup.status == 200 and signup.body["token"]
    login = anonymous.request("POST", "/api/auth/login", {"email": email, "password": password})
    assert login.status == 200 and login.body["token"]
    dashboard = Client(base, login.body["token"])
    created = dashboard.request("POST", "/api/v1/api-keys", {"name": label})
    assert created.status in {200, 201} and created.body["key"].startswith("fp_live_")
    listed = dashboard.request("GET", "/api/v1/api-keys")
    assert listed.status == 200
    assert created.body["key"] not in json.dumps(listed.body)
    assert all("key" not in item and "key_hash" not in item for item in listed.body)
    return Client(base, created.body["key"]), dashboard, created.body["id"]


def profiles(client, label):
    result = []
    for suffix in ("a", "b"):
        payload = {"label": label + suffix, "idempotency_key": label + suffix}
        reply = client.request("POST", f"{PREFIX}/profiles", payload)
        assert reply.status == 201
        result.append(reply.body["profile_id"])
    assert result[0] != result[1]
    return result


def ownership(base, owner, foreign, profile_ids):
    for token in ("", "fp_live_invalid_synthetic_only", "eyJhbGciOiJub25lIn0.eyJ1c2VyX2lkIjoiZm9yZ2VkIn0."):
        Client(base, token).expect_error("GET", f"{PREFIX}/profiles", None, "unauthenticated",
                                        {"X-User-Id": "forged", "X-Account-Id": "forged"})
    foreign_list = foreign.request("GET", f"{PREFIX}/profiles")
    assert foreign_list.status == 200
    assert not set(profile_ids).intersection(item["profile_id"] for item in foreign_list.body["items"])
    for profile_id in profile_ids:
        foreign.expect_error("DELETE", f"{PREFIX}/profiles/{profile_id}", None, "not_found")
        foreign.expect_error("POST", PREFIX, {"task": "No effects", "profile_id": profile_id,
                             "idempotency_key": "foreign-" + profile_id}, "not_found")
    return {"foreign_profiles_checked": len(profile_ids)}


def retry_followup(base, owner, foreign, profile_id, site, run_id):
    task = {"task": f"Open {site.url}/ . Click Complete prerequisite once, then Add synthetic item once. Stop. Do not submit or sign in.",
            "profile_id": profile_id, "idempotency_key": run_id + ":admit"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: owner.start(task), range(4)))
    assert all(r == receipts[0] for r in receipts)
    receipt = receipts[0]
    admission(receipt)
    changed = {**task, "task": "Different behavior; must conflict"}
    owner.expect_error("POST", PREFIX, changed, "conflict")
    path = receipt["status_url"]
    foreign.expect_error("GET", path, None, "not_found")
    foreign.expect_error("POST", path + "/cancel", None, "not_found")
    # New transport instance, not caller process continuity.
    fresh = Client(base, owner.token)
    settled = fresh.wait(receipt["task_id"], timeout=30)
    assert settled["state"] == "completed"
    assert site.counters() == {"prerequisite": 1, "cart": 1}
    view(fresh.get(receipt["task_id"]), settled)
    follow = fresh.start({"task": "On the existing synthetic page, add one more item. Do not repeat the prerequisite or submit.",
                          "session_id": receipt["session_id"], "idempotency_key": run_id + ":follow"})
    assert follow["session_id"] == receipt["session_id"] and follow["profile_id"] == profile_id
    assert fresh.wait(follow["task_id"], timeout=30)["state"] == "completed"
    assert site.counters() == {"prerequisite": 1, "cart": 2}
    ordinary_safe(fresh.get(follow["task_id"]))
    return {"admission_attempts": 4, "distinct_tasks": 1, "effect_counters": site.counters()}


def interaction_boundaries(owner, foreign, profile_id, run_id, site):
    """Deterministic local inference must honor natural interaction requests.

    Failure is real evidence, not permission to fabricate a waiting status. These
    checks cover HTTP boundaries; secret at-rest and executor stop proof stay open.
    """
    receipt = owner.start({"task": "Ask me for a synthetic OTP using a secret input field before doing anything else. After I answer, finish without echoing the value or any website effects.",
                           "profile_id": profile_id, "idempotency_key": run_id + ":question"})
    waiting = owner.wait(receipt["task_id"], {"waiting_for_input"}, timeout=15)
    interaction = waiting["interaction"]
    assert any(field["kind"] == "secret" for field in interaction["fields"])
    path = receipt["status_url"] + "/interactions/" + interaction["interaction_id"] + "/respond"
    values = {field["name"]: "synthetic-canary-" + secrets.token_hex(16) for field in interaction["fields"]}
    response = {"generation": interaction["generation"], "kind": "input", "values": values,
                "idempotency_key": run_id + ":answer"}
    foreign.expect_error("POST", path, response, "not_found")
    owner.expect_error("POST", path, {**response, "generation": interaction["generation"] + 1}, "stale_generation")
    accepted = owner.request("POST", path, response)
    assert accepted.status == 202
    duplicate = owner.request("POST", path, response)
    assert duplicate.status == 202
    owner.expect_error("POST", path, {**response, "values": {key: value + "changed" for key, value in values.items()}}, "conflict")
    finished = owner.wait(receipt["task_id"])
    assert finished["state"] == "completed"
    assert not any(secret in json.dumps(finished) for secret in values.values())

    decision = owner.start({"task": f"Open {site.url}/ . Ask for explicit confirmation before clicking Confirm synthetic submission. If declined, finish without submitting.",
                            "profile_id": profile_id, "idempotency_key": run_id + ":confirmation"})
    waiting = owner.wait(decision["task_id"], {"waiting_for_confirmation"})
    interaction = waiting["interaction"]
    path = decision["status_url"] + "/interactions/" + interaction["interaction_id"] + "/respond"
    decline = {"generation": interaction["generation"], "kind": "confirmation", "confirmed": False,
               "idempotency_key": run_id + ":decline"}
    foreign.expect_error("POST", path, decline, "not_found")
    assert owner.request("POST", path, decline).status == 202
    assert owner.request("POST", path, decline).status == 202
    owner.expect_error("POST", path, {**decline, "confirmed": True}, "conflict")
    assert owner.wait(decision["task_id"])["state"] == "completed"
    assert site.counters().get("submit", 0) == 0
    return {"questions_answered": 1, "declines": 1, "submit_effects": 0,
            "limitations": "canaries only checked in task results; durable artifacts and expiry remain unverified"}


def cancel_boundary(owner, profile_id, run_id):
    receipt = owner.start({"task": "Request browser assistance and wait for me without website effects.",
                           "profile_id": profile_id, "idempotency_key": run_id + ":cancel"})
    waiting = owner.wait(receipt["task_id"], {"waiting_for_browser"})
    interaction = waiting["interaction"]
    mint_path = receipt["status_url"] + "/assistance"
    grant = owner.request("POST", mint_path, {"interaction_id": interaction["interaction_id"],
                                             "generation": interaction["generation"]})
    assert grant.status == 201
    capability_path = grant.body["assistance_url"]
    assert capability_path.startswith(PREFIX + "/help/") and "?" not in capability_path
    response = owner.request("POST", receipt["status_url"] + "/cancel")
    assert response.status == 202
    view(response.body)
    assert response.body["state"] in {"cancelling", "cancelled", "interrupted"}
    terminal = owner.wait(receipt["task_id"])
    assert terminal["state"] in {"cancelled", "interrupted"}
    view(owner.get(receipt["task_id"]), terminal)
    Client(owner.base_url).expect_error("GET", capability_path + "/state", None, "assistance_expired")
    stale = {"generation": interaction["generation"], "kind": "browser", "browser_resolved": True,
             "idempotency_key": run_id + ":late"}
    late_path = receipt["status_url"] + "/interactions/" + interaction["interaction_id"] + "/respond"
    owner.expect_error("POST", late_path, stale, "stale_generation")
    return {"observed_terminal_state": terminal["state"],
            "limitations": "executor/browser stop and in-flight stream/input fencing require owned process hooks"}


def run_service(evidence, base, site, allow_tasks=False):
    readiness = Client(base).request("GET", "/health/browser-tasks")
    if readiness.status != 200:
        evidence.record("service_readiness", "blocked", "task peers disabled or unavailable",
                        http_status=readiness.status)
        return
    evidence.record("service_readiness", "passed", "local task readiness HTTP 200")
    if not allow_tasks:
        evidence.record("service_scenarios", "blocked", "explicit deterministic-local authorization required before creating accounts/tasks")
        return
    accounts = []
    def onboarding():
        for suffix in ("owner", "foreign"):
            accounts.append(onboard(base, evidence.run_id + suffix))
        return {"accounts_created": 2}
    if not evidence.check("onboarding_http", onboarding, "onboarding_http"):
        return
    owner, dashboard, key_id = accounts[0]
    foreign = accounts[1][0]
    created_profiles = []
    def create_profiles():
        created_profiles.extend(profiles(owner, evidence.run_id))
        return {"distinct_owned_profiles": 2}
    if not evidence.check("explicit_profiles", create_profiles):
        return
    evidence.check("auth_ownership", lambda: ownership(base, owner, foreign, created_profiles))
    evidence.check("http_retry_followup", lambda: retry_followup(base, owner, foreign, created_profiles[0], site, evidence.run_id),
                   "http_retry_followup")
    evidence.check("questions_decline_http", lambda: interaction_boundaries(owner, foreign, created_profiles[1], evidence.run_id, site))
    evidence.check("cancel_capability_http", lambda: cancel_boundary(owner, created_profiles[1], evidence.run_id))
    def revoke():
        response = dashboard.request("DELETE", "/api/v1/api-keys/" + key_id)
        assert response.status == 200
        owner.expect_error("GET", f"{PREFIX}/profiles", None, "unauthenticated")
    evidence.check("revocation", revoke)
    # Account data are confined to the explicitly disposable service. Never purge a DB.
    # Leased profiles cannot be deleted until the owning executor has stopped.
