"""Drive real browser tasks through the public HTTP API and record timings.

Uses only the documented account, API-key and browser-task endpoints, so it
works the same against a local Compose stack or a hosted deployment.

    python scripts/e2e_browser_tasks.py --base-url http://localhost:8080 \
        --scenarios scripts/e2e_scenarios.json --out reports/e2e

Confirmations are always declined: nothing is booked or purchased.
Input questions are answered only from a scenario's `answers` map;
anything else cancels the task so a run never hangs on a human.
"""
import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"completed", "failed", "interrupted", "cancelled"}


class Api:
    def __init__(self, base, token=""):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"raw": raw[:200].decode(errors="replace")}
        except (TimeoutError, urllib.error.URLError, ConnectionError) as err:
            # A slow or restarting server is retried by the caller's poll loop.
            return 0, {"transport_error": type(err).__name__}


def account_key(base, email, password):
    anon = Api(base)
    status, body = anon.call("POST", "/api/auth/signup", {"email": email, "password": password, "name": "e2e"})
    if status >= 400:
        status, body = anon.call("POST", "/api/auth/login", {"email": email, "password": password})
    if status >= 400:
        raise SystemExit(f"login failed: {status}")
    status, created = Api(base, body["token"]).call("POST", "/api/v1/api-keys", {"name": "e2e"})
    if status >= 400:
        raise SystemExit(f"api key creation failed: {status}")
    return created["key"]


def respond(api, view, scenario, log):
    inter = view["interaction"]
    kind = inter["kind"]
    key = "resp-" + secrets.token_hex(8)
    log.append({"t": time.monotonic(), "interaction": kind, "message": inter.get("message"),
                "fields": [f.get("name") for f in inter.get("fields") or []]})
    path = f"/api/v1/browser-tasks/{view['task_id']}/interactions/{inter['interaction_id']}/respond"
    if kind == "confirmation":
        return api.call("POST", path, {"kind": "confirmation", "generation": inter["generation"],
                                       "idempotency_key": key, "confirmed": False})
    if kind == "input":
        answers = scenario.get("answers", {})
        names = [f["name"] for f in inter.get("fields") or []]
        if names and all(n in answers for n in names):
            return api.call("POST", path, {"kind": "input", "generation": inter["generation"],
                                           "idempotency_key": key, "values": {n: answers[n] for n in names}})
    # Unanswerable input or a browser handoff: stop rather than wait on a human.
    return api.call("POST", f"/api/v1/browser-tasks/{view['task_id']}/cancel")


def run(api, scenario, profile_id=None, session_id=None):
    body = {"task": scenario["task"], "idempotency_key": "e2e-" + secrets.token_hex(8)}
    if scenario.get("inputs"):
        body["inputs"] = scenario["inputs"]
    if session_id:
        body["session_id"] = session_id
    elif profile_id:
        body["profile_id"] = profile_id
    started = time.monotonic()
    status, adm = api.call("POST", "/api/v1/browser-tasks", body)
    if status != 202:
        return {"name": scenario["name"], "admission_status": status, "error": adm}
    task_id, revision, log, states = adm["task_id"], adm["revision"], [], []
    first_running = None
    deadline = started + scenario.get("timeout_seconds", 900)
    view = None
    while time.monotonic() < deadline:
        status, view = api.call("GET", f"/api/v1/browser-tasks/{task_id}?after_revision={revision}&wait_seconds=25")
        if status != 200:
            time.sleep(2)
            continue
        revision = view["revision"]
        if not states or states[-1][0] != view["state"]:
            states.append((view["state"], round(time.monotonic() - started, 2)))
            print(f"  [{states[-1][1]:7.1f}s] {view['state']} {json.dumps(view.get('progress') or {})[:160]}", flush=True)
        if view["state"] == "running" and first_running is None:
            first_running = time.monotonic() - started
        if view["state"] in TERMINAL:
            break
        if view.get("interaction") and view["state"].startswith("waiting_for"):
            respond(api, view, scenario, log)
    if view is None or view.get("state") not in TERMINAL:
        # Over budget: cancel and wait, so the next run gets the profile and browser slot.
        api.call("POST", f"/api/v1/browser-tasks/{task_id}/cancel")
        for _ in range(30):
            status, view = api.call("GET", f"/api/v1/browser-tasks/{task_id}")
            if status == 200 and view["state"] in TERMINAL:
                break
            time.sleep(2)
    elapsed = time.monotonic() - started
    for entry in log:
        entry["t"] = round(entry["t"] - started, 2)
    return {
        "name": scenario["name"], "task_id": task_id, "session_id": adm["session_id"],
        "profile_id": adm["profile_id"], "state": view and view["state"],
        "seconds_total": round(elapsed, 2),
        "seconds_to_running": first_running and round(first_running, 2),
        # Time the agent took to produce what it asks the owner to approve (or any pause).
        "seconds_to_first_pause": next((t for state, t in states if state.startswith("waiting_for_")), None),
        "states": states, "interactions": log,
        "result": view and view.get("result"), "error": view and view.get("error"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("FLOWPILOT_BASE_URL", "http://localhost:8080"))
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--out", default="reports/e2e")
    parser.add_argument("--email", default=os.environ.get("FLOWPILOT_E2E_EMAIL", "e2e@flowpilot.test"))
    args = parser.parse_args()
    password = os.environ.get("FLOWPILOT_E2E_PASSWORD") or "e2e-" + secrets.token_hex(12)
    api = Api(args.base_url, account_key(args.base_url, args.email, password))
    scenarios = json.loads(Path(args.scenarios).read_text())
    results = []
    profiles = {}  # scenario name -> profile its runs used
    for scenario in scenarios:
        if args.only and scenario["name"] not in args.only:
            continue
        runs = scenario.get("runs", ["cold"])
        # "profile_of" runs a different request in another scenario's profile, so its
        # saved stages must generalize to new values rather than replay old ones.
        profile_id = scenario.get("profile_id") or profiles.get(scenario.get("profile_of"))
        for label in runs:
            print(f"== {scenario['name']} [{label}]", flush=True)
            # Warm runs reuse the cold run's profile so saved reflexes and cookies apply.
            result = run(api, scenario, profile_id=profile_id if label != "cold" else None)
            result["run"] = label
            profile_id = result.get("profile_id") or profile_id
            profiles[scenario["name"]] = profile_id
            results.append(result)
            answer = ((result.get("result") or {}).get("answer") or "")[:600]
            pause = result.get('seconds_to_first_pause')
            print(f"   -> {result.get('state')} in {result.get('seconds_total')}s"
                  + (f" (first pause at {pause}s)" if pause is not None else "") + f"\n   {answer}\n", flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"e2e-{stamp}.json"
    path.write_text(json.dumps({"base_url": args.base_url, "results": results}, indent=2))
    print(f"wrote {path}")
    return 0 if all(r.get("state") == "completed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
