"""Standard-library HTTP client, usable without a running caller process or SDK."""

import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .wire import PREFIX, TERMINAL, admission, error, ordinary_safe, view


def local_url(url):
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
    assert not parsed.query and not parsed.fragment
    assert parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    return url.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected host.


@dataclass
class Reply:
    status: int
    body: object
    headers: dict


class Client:
    def __init__(self, base_url, token="", timeout=30):
        self.base_url = local_url(base_url)
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, headers=None):
        assert path.startswith("/") and not path.startswith("//") and "://" not in path
        fields = {"Accept": "application/json"}
        if self.token:
            fields["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        req = urllib.request.Request(self.base_url + path, method=method, headers=fields,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read(262145)
            assert len(data) <= 262144, "response exceeds harness bound"
            return Reply(response.status, json.loads(data) if data else None, dict(response.headers))

    def start(self, body):
        reply = self.request("POST", PREFIX, body)
        assert reply.status == 202, f"admission HTTP {reply.status}"
        admission(reply.body)
        ordinary_safe(reply.body)
        return reply.body

    def get(self, task_id):
        reply = self.request("GET", f"{PREFIX}/{task_id}")
        assert reply.status == 200, f"poll HTTP {reply.status}"
        view(reply.body)
        ordinary_safe(reply.body)
        return reply.body

    def wait(self, task_id, states=TERMINAL, timeout=15):
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            current = self.get(task_id)
            view(current, previous)
            if current["state"] in states:
                return current
            previous = current
            time.sleep(.05)
        raise TimeoutError("task did not reach requested state before bounded deadline")

    def expect_error(self, method, path, body, code, headers=None):
        reply = self.request(method, path, body, headers)
        error(reply.status, reply.body, reply.headers)
        assert reply.body["error"]["code"] == code
        return reply
