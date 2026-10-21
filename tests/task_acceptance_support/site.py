"""Run-labelled loopback website. Durable counters are independent effect oracles.

This is a fixture website, NOT a task executor. Driving it directly only validates
the fixture and the actual browser; it cannot satisfy integrated service gates.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Site:
    def __init__(self, directory):
        self.run_id = "task-acceptance-" + uuid.uuid4().hex
        self.directory = Path(directory) / self.run_id
        self.directory.mkdir(parents=True)
        self.database = self.directory / "effects.sqlite3"
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE effects (sequence INTEGER PRIMARY KEY, action TEXT NOT NULL)")
        self.selector = "add-original"
        self.after_effect = threading.Event()
        self.release_response = threading.Event()
        self.hold_response = False
        self.frame_url = ""

    def counters(self):
        with sqlite3.connect(self.database) as db:
            return dict(db.execute("SELECT action, COUNT(*) FROM effects GROUP BY action"))

    def effect(self, action):
        assert action in {"login", "prerequisite", "cart", "submit", "drag", "frame", "input"}
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO effects(action) VALUES (?)", (action,))
        self.after_effect.set()

    def page(self):
        return f"""<!doctype html><title>Synthetic acceptance site</title>
<label>Name<input id='name'></label><button id='login'>Sign in</button>
<button id='prerequisite'>Complete prerequisite</button>
<button id='{self.selector}' data-action='cart'>Add synthetic item</button>
<button id='submit'>Confirm synthetic submission</button>
<button id='delay'>Delayed update</button><div id='state'>initial</div>
<div id='drag' draggable='true'>Drag me</div><div id='drop'>Drop here</div>
<a id='navigate' href='/next'>Next page</a><iframe src='{self.frame_url}/frame'></iframe>
<script>
const effect = async action => {{ await fetch('/effects', {{method:'POST',body:JSON.stringify({{action}})}}); }};
document.querySelector('#login').onclick = async () => {{
  document.cookie = 'synthetic_session=owned; SameSite=Strict; path=/';
  localStorage.setItem('synthetic_user', document.querySelector('#name').value);
  await effect('login');
}};
document.querySelector('#prerequisite').onclick = () => effect('prerequisite');
document.querySelector('[data-action=cart]').onclick = () => effect('cart');
document.querySelector('#submit').onclick = () => effect('submit');
document.querySelector('#delay').onclick = () => setTimeout(() => {{
  document.querySelector('#state').textContent = 'settled';
  const b = document.createElement('button'); b.id='late'; b.textContent='Late action';
  b.onclick=()=>effect('input'); document.body.appendChild(b);
}}, 150);
document.querySelector('#name').onchange = () => effect('input');
document.querySelector('#drag').ondragstart = e => e.dataTransfer.setData('text/plain','fixture');
document.querySelector('#drop').ondragover = e => e.preventDefault();
document.querySelector('#drop').ondrop = e => {{e.preventDefault(); effect('drag');}};
setInterval(() => document.body.dataset.volatile = String(Date.now()), 30);
</script>"""


@contextmanager
def synthetic_site(directory, browser_host='127.0.0.1'):
    assert browser_host in {'127.0.0.1', 'localhost', 'host.docker.internal'}
    site = Site(directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request paths, cookies or bodies in telemetry.

        def send(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected at the effect/result fault boundary.

        def do_GET(self):
            if self.path in {"/", "/next"}:
                self.send(200, site.page().encode(), "text/html")
            elif self.path == "/frame":
                self.send(200, b"<button id='frame' onclick=\"fetch('/effects',{method:'POST',body:JSON.stringify({action:'frame'})})\">Frame effect</button>", "text/html")
            elif self.path == "/counters":
                self.send(200, json.dumps(site.counters()).encode(), "application/json")
            else:
                self.send(404, b"", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if self.path != "/effects" or not 0 < length <= 1024:
                return self.send(422, b"{}", "application/json")
            try:
                site.effect(json.loads(self.rfile.read(length))["action"])
            except (KeyError, ValueError, AssertionError):
                return self.send(422, b"{}", "application/json")
            if site.hold_response:
                site.release_response.wait(5)
            self.send(200, b'{"accepted":true}', "application/json")

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    site.url = f"http://127.0.0.1:{servers[0].server_port}"
    site.browser_url = f"http://{browser_host}:{servers[0].server_port}"
    site.frame_url = f"http://{browser_host}:{servers[1].server_port}"
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield site
    finally:
        site.release_response.set()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
        # Caller owns its unique directory. Never delete profiles or global state.
