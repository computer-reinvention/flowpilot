"""Disposable internal network + fake Docker API, never the host socket/real accounts.

Uses already-built lane005 backend/tools images. Creates only uniquely labelled
containers/network and no published ports or host mounts. Does NOT prove task crash
reconciliation; the foundation intentionally lacks executor/control implementations.
"""
import json
import subprocess
import time
import uuid


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    token = uuid.uuid4().hex[:12]
    network = "fp-lane005-" + token
    resources = []
    docker("network", "create", "--internal", "--label", "flowpilot.ops=lane005", network)
    api = """from http.server import BaseHTTPRequestHandler,HTTPServer
import json
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers()
  value = b'OK' if self.path == '/_ping' else (b'[]' if '/containers/json' in self.path else json.dumps({'ApiVersion':'1.47','Version':'27.0.0'}).encode())
  self.wfile.write(value)
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',2375),H).serve_forever()
"""
    try:
        resources.append(docker("run", "-d", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", network, "--network-alias", "docker-proxy", "--cpus", "0.25", "--memory", "128m", "--pids-limit", "64", "flowpilot-lane005-tools:verify", "python", "-c", api))
        # Give an emulated fixture server a bounded startup window.
        for attempt in range(30):
            try:
                docker("exec", resources[0], "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:2375/_ping',timeout=1)")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        else:
            raise RuntimeError("fake Docker API unavailable")
        for enabled, expected in (("false", 200), ("true", 503)):
            container = docker("run", "-d", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", network, "--cpus", "1", "--memory", "1g", "--pids-limit", "128", "--tmpfs", "/app/profiles:uid=10001,gid=10001,mode=0700,size=2g", "-e", "DOCKER_HOST=tcp://docker-proxy:2375", "-e", "GOOGLE_API_KEY=synthetic-fixture-not-a-provider-key", "-e", "JWT_SECRET=synthetic-local-fixture-secret-at-least-32-bytes", "-e", "FLOWPILOT_TASKS_ENABLED=" + enabled, "flowpilot-lane005-backend:verify")
            resources.append(container)
            path = "/health/browser-tasks" if enabled == "true" else "/health"
            probe = "import urllib.request,urllib.error;\ntry: print(urllib.request.urlopen('http://127.0.0.1:8080" + path + "',timeout=2).status)\nexcept urllib.error.HTTPError as e: print(e.code)"

            def wait():
                for attempt in range(60):
                    try:
                        status = docker("exec", container, "/app/.venv/bin/python", "-c", probe)
                        if status == str(expected):
                            return
                    except subprocess.CalledProcessError:
                        pass
                    time.sleep(0.5)
                # Logs are intentionally not emitted; they could contain app secrets.
                raise RuntimeError("disposable backend readiness did not reach expected status " + str(expected))

            wait()
            docker("restart", "--time", "65", container)
            wait()
            docker("stop", "--time", "65", container)
            code = docker("inspect", "--format", "{{.State.ExitCode}}", container)
            assert code == "0", "backend did not gracefully exit"
        print(json.dumps({"disabled_liveness_restart_graceful_stop": "PASS", "incomplete_enabled_composition_503_after_restart": "PASS", "task_interruption_semantics": "UNVERIFIED"}))
    finally:
        for container in reversed(resources):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
