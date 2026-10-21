"""Disposable HTTP/WS proxy + error-log redaction rehearsal (NOT TLS issuance)."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main():
    seen = []
    sentinel = "LANE005_CAPABILITY_CANARY"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b"fixture")
                except BrokenPipeError:
                    pass  # Expected when Caddy aborts an oversized request body.

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "proxy.log"
        with log.open("wb") as output:
            proc = subprocess.Popen(["caddy", "run", "--config", str(Path(__file__).with_name("ops_Caddyfile")), "--adapter", "caddyfile"], env={**os.environ, "FLOWPILOT_DOMAIN": "http://localhost:18080", "FLOWPILOT_EXTERNAL_HOST": "localhost:18080"}, stdout=output, stderr=output)
            try:
                for attempt in range(50):
                    try:
                        urllib.request.urlopen("http://localhost:18080/health", timeout=1).close()
                        break
                    except Exception:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("local proxy unavailable")
                url = "http://localhost:18080/api/v1/browser-tasks/help/" + sentinel + "?token=" + sentinel
                request = urllib.request.Request(url, headers={"Forwarded": "host=evil.invalid", "X-Forwarded-Proto": "http"})
                with urllib.request.urlopen(request) as response:
                    assert response.headers["Referrer-Policy"] == "no-referrer"
                assert seen[-1].get("X-Forwarded-Proto") == "https"
                assert seen[-1].get("Host") == "localhost:18080"
                assert seen[-1].get("X-Forwarded-Host") == "localhost:18080"
                assert "Forwarded" not in seen[-1]
                before = len(seen)
                urllib.request.urlopen(urllib.request.Request(url, headers={"Host": "foreign.invalid"})).close()
                assert len(seen) == before, "foreign Host routed to backend"
                with socket.create_connection(("127.0.0.1", 18080), timeout=3) as sock:
                    sock.sendall((f"GET /api/v1/browser-tasks/help/{sentinel}/stream HTTP/1.1\r\nHost: localhost:18080\r\nOrigin: http://localhost:18080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
                    assert b"101" in sock.recv(4096)
                try:
                    urllib.request.urlopen(urllib.request.Request(url, data=b"x" * 140000), timeout=3)
                    raise AssertionError("unbounded request accepted")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 413
                server.shutdown()
                server.server_close()
                thread.join()
                try:
                    urllib.request.urlopen(url, timeout=3)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 502
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                if thread.is_alive():
                    server.shutdown()
                    server.server_close()
                    thread.join()
        assert sentinel.encode() not in log.read_bytes(), "capability leaked into proxy logs"
    print("PASS: local HTTP/WS upgrade, body bound, forwarded headers, capability-free normal/error logs; TLS UNVERIFIED")


if __name__ == "__main__":
    main()
