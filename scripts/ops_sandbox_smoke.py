"""Run the built browser image offline; no host mounts or published CDP ports."""
import json
import subprocess
import time


def main():
    def docker(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()

    container = docker("run", "-d", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--network", "none", "--cpus", "1", "--memory", "1g", "--pids-limit", "256", "--shm-size", "256m", "--security-opt", "no-new-privileges:true", "flowpilot-lane005-sandbox:verify")
    try:
        for attempt in range(60):
            result = subprocess.run(["docker", "exec", container, "curl", "-fsS", "--max-time", "2", "http://127.0.0.1:9222/json/version"], capture_output=True, text=True)
            if result.returncode == 0:
                version = json.loads(result.stdout)
                assert "Browser" in version
                assert docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", container) in {"{}", "null"}
                print("PASS: offline sandbox CDP readiness; no host publication; Linux/amd64 image")
                return
            time.sleep(0.5)
        raise SystemExit("BLOCKER: offline amd64 Chromium did not become ready; inspect only this labelled disposable fixture on a target-compatible kernel")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


if __name__ == "__main__":
    main()
