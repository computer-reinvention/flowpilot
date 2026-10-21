#!/usr/bin/env python3
"""Standard-library HTTP client. No Flowpilot imports, files, or SDK required."""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings

PREFIX = "/api/v1/browser-tasks"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def private_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise RuntimeError("Private terminal required; use a configured API key or programmatic client") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer credential through redirects.


class Client:
    def __init__(self, base_url, token):
        url = urllib.parse.urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("Use an HTTP(S) service origin without credentials/path/query")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS except for loopback mocks")
        self.base_url, self.token = base_url.rstrip("/"), token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, body=None):
        if not path.startswith("/api/") or ".." in path or ":" in path or "#" in path:
            raise ValueError("Use a relative API path")
        req = urllib.request.Request(self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Cache-Control": "no-store"})
        try:
            with self.opener.open(req, timeout=35) as response:
                data = response.read(262145)
                if len(data) > 262144:
                    raise RuntimeError("Response exceeds example client limit")
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            # Do not print raw server bodies, URLs, submitted values or credentials.
            raise RuntimeError(f"HTTP {error.code}; refresh owned status; retry only the same request/key when appropriate") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("Transport outcome unknown. Retry identical admission/key or reattach by task ID; do not replay website effects") from None

    def start(self, payload):
        if not payload.get("idempotency_key"):
            raise ValueError("Provide a stable idempotency_key before sending admission")
        return self.request("POST", PREFIX, payload)

    def status(self, task_id):
        return self.request("GET", PREFIX + "/" + identifier(task_id))

    def poll(self, task_id, attempts=20, interval=1):
        for _ in range(attempts):
            view = self.status(task_id)
            if view["state"] in TERMINAL or view.get("interaction"):
                return view
            time.sleep(interval)
        raise RuntimeError("Polling budget exhausted; task is not cancelled. Reattach by task ID")

    def respond(self, task_id, interaction, values):
        return self.request("POST", f"{PREFIX}/{identifier(task_id)}/interactions/{identifier(interaction['interaction_id'])}/respond", {
            **values, "generation": interaction["generation"], "kind": interaction["kind"],
        })

    def assistance(self, task_id, interaction):
        grant = self.request("POST", f"{PREFIX}/{identifier(task_id)}/assistance", {
            "interaction_id": interaction["interaction_id"], "generation": interaction["generation"],
        })
        token = grant.get("assistance_url", "").removeprefix(PREFIX + "/help/")
        if not grant.get("assistance_url", "").startswith(PREFIX + "/help/") or not 32 <= len(token) <= 128:
            raise ValueError("Invalid private viewer path")
        identifier(token)
        if (grant["interaction_id"], grant["generation"]) != (interaction["interaction_id"], interaction["generation"]):
            raise ValueError("Private viewer does not match the current pause")
        return grant


def identifier(value):
    if not value or len(value) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
        raise ValueError("Invalid object ID")
    return value


def safe_output(value):
    # Ordinary output must never accidentally disclose create-key or mint responses.
    if isinstance(value, dict):
        return {key: safe_output(item) for key, item in value.items() if key not in {"key", "token", "assistance_url", "values"}}
    if isinstance(value, list):
        return [safe_output(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status", "poll", "followup", "respond", "help", "cancel", "profiles", "profile-create", "profile-delete", "revoke"])
    parser.add_argument("object_id", nargs="?")
    parser.add_argument("--bootstrap", action="store_true", help="Login, create an in-memory key, perform one operation, then revoke it")
    parser.add_argument("--signup", action="store_true", help="With --bootstrap, create an account instead of logging in")
    parser.add_argument("--private-link", action="store_true", help="Explicitly reveal a help bearer link to an unrecorded terminal only")
    args = parser.parse_args()
    base = os.environ.get("FLOWPILOT_BASE_URL", "http://127.0.0.1:8080")
    temporary_key = None
    dashboard = None
    client = Client(base, os.environ.get("FLOWPILOT_API_KEY", ""))
    try:
        if args.bootstrap:
            email = private_input("Account email (hidden; terminal input): ")
            password = private_input("Account password: ")
            body = {"email": email, "password": password}
            if args.signup:
                body["name"] = private_input("Account name (hidden; terminal input): ")
            auth = client.request("POST", "/api/auth/signup" if args.signup else "/api/auth/login", body)
            password = None
            dashboard = Client(base, auth["token"])
            created = dashboard.request("POST", "/api/v1/api-keys", {"name": "Temporary standalone HTTP example"})
            temporary_key = created["id"]
            client.token = created.pop("key")
        elif not client.token:
            client.token = private_input("Flowpilot API key: ")

        command, task_id = args.command, args.object_id
        if command in {"start", "followup", "profile-create"}:
            # Read JSON from stdin: never pass secrets in process arguments.
            body = json.load(sys.stdin)
            if command == "followup":
                body["session_id"] = identifier(task_id)
            output = client.request("POST", PREFIX + "/profiles", body) if command == "profile-create" else client.start(body)
        elif command in {"status", "poll"}:
            output = client.poll(task_id) if command == "poll" else client.status(task_id)
        elif command == "profiles":
            output = client.request("GET", PREFIX + "/profiles")
        elif command == "profile-delete":
            output = client.request("DELETE", PREFIX + "/profiles/" + identifier(task_id))
        elif command == "revoke":
            output = client.request("DELETE", "/api/v1/api-keys/" + identifier(task_id))
        elif command == "cancel":
            output = client.request("POST", PREFIX + "/" + identifier(task_id) + "/cancel")
        else:
            view = client.status(task_id)
            pause = view.get("interaction")
            if not pause:
                raise ValueError("No active interaction; inspect current status")
            if command == "help":
                if pause["kind"] != "browser":
                    raise ValueError("Help can only be minted during a browser pause")
                output = client.assistance(task_id, pause)
                if args.private_link:
                    if not sys.stdout.isatty():
                        raise ValueError("Refusing to write bearer viewer link to logs/pipes")
                    print(base.rstrip("/") + output["assistance_url"])
            else:
                response = {"idempotency_key": str(uuid.uuid4())}
                print(pause["message"], file=sys.stderr)
                if pause["kind"] == "input":
                    response["values"] = {field["name"]: private_input(field["label"] + ": ") for field in pause["fields"]}
                elif pause["kind"] == "confirmation":
                    decision = input("Type approve or decline for this exact proposed action: ")
                    if decision not in {"approve", "decline"}:
                        raise ValueError("Explicit approve/decline required")
                    response["confirmed"] = decision == "approve"
                else:
                    if input("Type resolved only after resolving this browser pause: ") != "resolved":
                        raise ValueError("Explicit browser resolution required")
                    response["browser_resolved"] = True
                try:
                    output = client.respond(task_id, pause, response)
                finally:
                    response.clear()  # Best effort; Python cannot promise memory zeroization.
        print(json.dumps(safe_output(output), indent=2))
    finally:
        if temporary_key:
            dashboard.request("DELETE", "/api/v1/api-keys/" + identifier(temporary_key))
        client.token = ""


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, EOFError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
