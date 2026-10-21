"""Run the built browser image offline; no host mounts or published CDP ports."""
import json
import os
import subprocess
import time
import uuid

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


# Same path the backend uses: resolve the sandbox name to an IP (Chromium rejects
# non-IP Host headers), Playwright connect_over_cdp to socat 9223, render, screenshot.
E2E = """
import asyncio, socket
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://%s:9223" % socket.gethostbyname("sandbox"))
        page = await browser.contexts[0].new_page()
        await page.set_content("<h1 id=t>flowpilot smoke</h1>")
        text, png = await page.inner_text("#t"), await page.screenshot()
        assert text == "flowpilot smoke" and png[:8] == bytes.fromhex("89504e470d0a1a0a") and len(png) > 1000
        print("E2E browser=%s screenshot_bytes=%d" % (browser.version, len(png)))
asyncio.run(main())
"""


def backend_e2e(backend_image):
    """Optional: backend image drives the sandbox over a disposable internal network."""
    def docker(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    network = "fp-smoke-" + uuid.uuid4().hex[:12]
    docker("network", "create", "--internal", "--label", "flowpilot.ops=lane005", network)
    resources = []
    try:
        resources.append(docker("run", "-d", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", network, "--network-alias", "sandbox", "--cpus", "1", "--memory", "1g", "--pids-limit", "256", "--shm-size", "256m", "--security-opt", "no-new-privileges:true", IMAGE))
        for attempt in range(60):
            if subprocess.run(["docker", "exec", resources[0], "python3", "-c", PROBE], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("BLOCKER: sandbox CDP not ready for backend E2E")
        print(docker("run", "--rm", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", network, "--cpus", "1", "--memory", "1g", "--pids-limit", "128", "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--entrypoint", "/app/.venv/bin/python", backend_image, "-c", E2E))
        print("PASS: backend Playwright connect_over_cdp -> sandbox socat 9223, rendered and screenshotted")
    finally:
        for container in resources:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
    if os.environ.get("FLOWPILOT_BACKEND_IMAGE"):
        backend_e2e(os.environ["FLOWPILOT_BACKEND_IMAGE"])
