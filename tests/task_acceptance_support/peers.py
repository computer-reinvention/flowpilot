"""Scripted HTTP peer tests the consumer, never proves service behavior."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def scripted_peer(exchanges):
    remaining = list(exchanges)
    failures = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                with lock:
                    assert remaining, "unexpected peer request"
                    case = remaining.pop(0)
                assert (self.command, self.path) == (case["method"], case["path"])
                for name, value in case.get('expected_headers', {}).items():
                    assert self.headers.get(name) == value
                if "request" in case and self.command != "GET":
                    assert body == case["request"]
                if case.get("drop_response"):
                    self.close_connection = True
                    return
                data = b"" if case.get("response") is None else json.dumps(case["response"]).encode()
                self.send_response(case["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in case.get("headers", {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                failures.append(type(exc).__name__)
                self.send_error(500)

        do_GET = do_POST = do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
        assert not failures and not remaining, "scripted peer not consumed correctly"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
