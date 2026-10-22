"""Standard-library HTTP client, usable without a running caller process or SDK."""

import ipaddress
import json
import http.client
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
    def __init__(self, base_url, token="", timeout=30, ssl_context=None):
        self.base_url = local_url(base_url)
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl_context
        handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
        if ssl_context is not None:
            # Explicit trust root for the run's local TLS proxy; never disables verification.
            assert ssl_context.verify_mode != 0 and ssl_context.check_hostname
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, body=None, headers=None, raw=False):
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
            return Reply(response.status, data if raw else json.loads(data) if data else None, dict(response.headers))

    def start(self, body):
        reply = self.request("POST", PREFIX, body)
        assert reply.status == 202, f"admission HTTP {reply.status}"
        admission(reply.body)
        ordinary_safe(reply.body)
        return reply.body

    def start_retry(self, body, attempts=3):
        """Retry HTTP admission, never issue a new website task/key after uncertainty."""
        assert 1 <= attempts <= 5 and body.get("idempotency_key")
        for attempt in range(attempts):
            try:
                reply = self.request("POST", PREFIX, body)
            except (urllib.error.URLError, http.client.RemoteDisconnected, TimeoutError, ConnectionError):
                if attempt + 1 == attempts:
                    raise
                continue
            if reply.status == 202:
                admission(reply.body)
                ordinary_safe(reply.body)
                return reply.body
            error(reply.status, reply.body, reply.headers)
            if not reply.body["error"]["retryable"] or attempt + 1 == attempts:
                raise AssertionError(f"admission HTTP {reply.status}")
            delay = reply.body["error"].get("retry_after_seconds", 1)
            if delay > 5:
                raise TimeoutError("server backoff exceeds bounded admission retry budget")
            time.sleep(delay)

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
