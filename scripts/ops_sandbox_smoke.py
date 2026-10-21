"""Run the built browser image offline; no host mounts or published CDP ports."""
import json
import os
import subprocess
import time

IMAGE = os.environ.get("FLOWPILOT_SANDBOX_IMAGE", "flowpilot-lane005-sandbox:verify")
PROBE = """
import json, urllib.request
version = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2))
forwarded = json.load(urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=2))
assert forwarded["Browser"] == version["Browser"]
target = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9223/json/new?about:blank", method="PUT"), timeout=5))
assert target["webSocketDebuggerUrl"].startswith("ws://")
version["new_target"] = target["type"]
print(json.dumps(version))
"""


def main():
    def docker(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    container = docker("run", "-d", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", "none", "--cpus", "1", "--memory", "1g", "--pids-limit", "256", "--shm-size", "256m", "--security-opt", "no-new-privileges:true", IMAGE)
    try:
        for attempt in range(60):
            result = subprocess.run(["docker", "exec", container, "python3", "-c", PROBE], capture_output=True, text=True)
            if result.returncode == 0:
                version = json.loads(result.stdout)
                assert "Browser" in version
                expected = os.environ.get("FLOWPILOT_EXPECT_BROWSER")
                if expected:
                    assert version["Browser"].endswith("/" + expected), version["Browser"]
                print("Browser: " + version["Browser"])
                assert docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", container) in {"{}", "null"}
                print("PASS: offline sandbox CDP readiness (9222 + socat 9223, new page target); no host publication; Linux/amd64 image")
                return
            time.sleep(0.5)
        raise SystemExit("BLOCKER: offline amd64 Chromium did not become ready; inspect only this labelled disposable fixture on a target-compatible kernel")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


if __name__ == "__main__":
    main()
