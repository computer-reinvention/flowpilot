"""Benchmark: vanilla browser-use agent vs FlowPilot reflex runner.

Runs multiple scenarios of increasing complexity against public test sites.
Each scenario runs twice through FlowPilot — first to generate the reflex,
second to replay it. Vanilla browser-use runs once (it has no caching).

Scenarios:
  1. Simple form       — demoqa.com practice form (8 fields + submit)
  2. Login + action    — saucedemo.com login → add item to cart
  3. E-commerce flow   — saucedemo.com login → add items → checkout → complete
  4. Dynamic content   — the-internet.herokuapp.com dynamic controls + inputs
  5. Multi-step nav    — demoqa.com book store (login → search → interact)

Prerequisites:
  - Docker running, sandbox image built (docker compose build sandbox)
  - LLM env vars set (GOOGLE_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY)

Run:
  uv run python scripts/benchmark.py              # all scenarios
  uv run python scripts/benchmark.py --scenario 1 # single scenario
  uv run python scripts/benchmark.py --only fp    # only FlowPilot (skip vanilla)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docker
from browser_use import Agent, Browser

from backend.runner import _get_llm, _get_fast_llm, FlowRunner, _async_cdp_get
from backend.models import Flow
from backend.sandbox import CDP_CONTAINER_PORT, SANDBOX_IMAGE


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    id: int
    name: str
    domain: str
    entry_url: str
    task: str
    inputs: dict


SCENARIOS = [
    Scenario(
        id=1,
        name="Simple form fill",
        domain="demoqa.com",
        entry_url="https://demoqa.com/automation-practice-form",
        task=(
            "Go to https://demoqa.com/automation-practice-form and fill out the form:\n"
            "- First Name: John\n- Last Name: Doe\n- Email: john.doe@test.com\n"
            "- Gender: Male\n- Mobile: 1234567890\n- Subjects: Maths\n"
            "- Hobbies: Sports\n- Current Address: 123 Test Street\n"
            "Then click Submit."
        ),
        inputs={
            "first_name": "John",
            "last_name": "Doe",
            "email": "john.doe@test.com",
            "mobile": "1234567890",
            "subject": "Maths",
            "address": "123 Test Street",
        },
    ),
    Scenario(
        id=2,
        name="Login + add to cart",
        domain="saucedemo.com",
        entry_url="https://www.saucedemo.com/",
        task=(
            "Go to https://www.saucedemo.com/ and:\n"
            "1. Log in with username 'standard_user' and password 'secret_sauce'\n"
            "2. Add 'Sauce Labs Backpack' to the cart\n"
            "3. Click the cart icon to view the cart\n"
            "4. Verify the item is in the cart"
        ),
        inputs={
            "username": "standard_user",
            "password": "secret_sauce",
            "product": "Sauce Labs Backpack",
        },
    ),
    Scenario(
        id=3,
        name="Form with validation",
        domain="demoqa.com/text-box",
        entry_url="https://demoqa.com/text-box",
        task=(
            "Go to https://demoqa.com/text-box and fill out the form:\n"
            "- Full Name: Jane Smith\n"
            "- Email: jane.smith@example.com\n"
            "- Current Address: 456 Oak Avenue, Suite 200, San Francisco, CA 94102\n"
            "- Permanent Address: 789 Pine Street, Apt 3B, New York, NY 10001\n"
            "Then click Submit."
        ),
        inputs={
            "full_name": "Jane Smith",
            "email": "jane.smith@example.com",
            "current_address": "456 Oak Avenue, Suite 200, San Francisco, CA 94102",
            "permanent_address": "789 Pine Street, Apt 3B, New York, NY 10001",
        },
    ),
    Scenario(
        id=4,
        name="Checkboxes + radio",
        domain="demoqa.com/checkbox",
        entry_url="https://demoqa.com/checkbox",
        task=(
            "Go to https://demoqa.com/checkbox and:\n"
            "1. Expand the 'Home' tree by clicking the toggle arrow\n"
            "2. Expand 'Desktop' and 'Documents'\n"
            "3. Check the 'Notes' checkbox under Desktop\n"
            "4. Check the 'Office' checkbox under Documents\n"
            "5. Verify the selected items show in the result area"
        ),
        inputs={
            "select_items": ["Notes", "Office"],
        },
    ),
    Scenario(
        id=5,
        name="Sortable table",
        domain="demoqa.com/webtables",
        entry_url="https://demoqa.com/webtables",
        task=(
            "Go to https://demoqa.com/webtables and:\n"
            "1. Click the 'Add' button to add a new record\n"
            "2. Fill in the registration form:\n"
            "   - First Name: Alice\n"
            "   - Last Name: Johnson\n"
            "   - Email: alice@example.com\n"
            "   - Age: 28\n"
            "   - Salary: 85000\n"
            "   - Department: Engineering\n"
            "3. Click Submit\n"
            "4. Type 'Alice' in the search box to filter the table\n"
            "5. Verify Alice Johnson appears in the results"
        ),
        inputs={
            "first_name": "Alice",
            "last_name": "Johnson",
            "email": "alice@example.com",
            "age": "28",
            "salary": "85000",
            "department": "Engineering",
        },
    ),
]


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

async def wait_for_cdp(host: str, port: int, timeout: int = 40) -> str:
    """Poll CDP endpoint until ready, return the ws:// URL."""
    for _ in range(timeout // 2):
        info = await _async_cdp_get(host, port, "/json/version")
        if info and "webSocketDebuggerUrl" in info:
            ws_path = "/" + "/".join(info["webSocketDebuggerUrl"].split("/")[3:])
            return f"ws://{host}:{port}{ws_path}"
        await asyncio.sleep(2)
    raise RuntimeError(f"CDP not reachable at {host}:{port} after {timeout}s")


def start_sandbox(client: docker.DockerClient, name: str, host_port: int) -> str:
    """Start a sandbox container with CDP port published to localhost."""
    profiles_dir = os.path.abspath(
        os.environ.get("PROFILES_HOST_DIR", os.environ.get("PROFILES_DIR", "./profiles"))
    )
    os.makedirs(profiles_dir, exist_ok=True)

    container = client.containers.run(
        image=SANDBOX_IMAGE,
        name=name,
        detach=True,
        ports={f"{CDP_CONTAINER_PORT}/tcp": host_port},
        volumes={profiles_dir: {"bind": "/profiles", "mode": "rw"}},
        environment={"DISPLAY": ":1"},
        mem_limit="1g",
        cpu_quota=100000,
        pids_limit=256,
        shm_size="256m",
        security_opt=["seccomp=unconfined"],
    )
    return container.id


def kill_sandbox(client: docker.DockerClient, name: str):
    try:
        client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    scenario: str
    runner: str
    elapsed: float
    steps: int
    success: bool
    error: str | None = None


async def run_vanilla(scenario: Scenario, cdp_url: str) -> RunResult:
    """Run a vanilla browser-use agent — no FlowPilot."""
    llm = _get_llm()
    browser = Browser(cdp_url=cdp_url)
    await browser.start()

    agent = Agent(
        task=scenario.task,
        llm=llm,
        browser=browser,
        page_extraction_llm=_get_fast_llm(),
    )

    start = time.monotonic()
    try:
        result = await agent.run(max_steps=30)
        elapsed = time.monotonic() - start
        success = result.is_successful() if hasattr(result, "is_successful") else None
        steps = result.number_of_steps()
        return RunResult(scenario.name, "vanilla", elapsed, steps, bool(success))
    except Exception as e:
        elapsed = time.monotonic() - start
        return RunResult(scenario.name, "vanilla", elapsed, 0, False, str(e))


async def run_flowpilot(scenario: Scenario, cdp_url: str, run_id: str) -> RunResult:
    """Run via FlowPilot's FlowRunner."""
    flow = Flow(
        id=f"bench-{scenario.id}",
        name=scenario.name,
        domain=scenario.domain,
        task=scenario.task,
        entry_url=scenario.entry_url,
        inputs=json.dumps([]),
        created_by="bench",
    )

    steps_log: list[str] = []

    async def progress_cb(status, message):
        steps_log.append(message)

    runner = FlowRunner()

    start = time.monotonic()
    try:
        await runner.run_flow(
            flow=flow,
            inputs=scenario.inputs,
            user_id="bench",
            sandbox_id=f"bench-{scenario.id}",
            cdp_url=cdp_url,
            run_id=run_id,
            progress_callback=progress_cb,
        )
        elapsed = time.monotonic() - start
        return RunResult(scenario.name, "flowpilot", elapsed, len(steps_log), True)
    except Exception as e:
        elapsed = time.monotonic() - start
        return RunResult(scenario.name, "flowpilot", elapsed, len(steps_log), False, str(e))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_scenario(
    client: docker.DockerClient,
    scenario: Scenario,
    port_base: int,
    skip_vanilla: bool = False,
) -> dict:
    """Run one scenario: vanilla + FlowPilot cold + FlowPilot warm."""
    print(f"\n{'='*70}")
    print(f"  Scenario {scenario.id}: {scenario.name}")
    print(f"  {scenario.entry_url}")
    print(f"{'='*70}")

    results = {}

    # --- Vanilla ---
    if not skip_vanilla:
        name = f"bench-vanilla-{scenario.id}"
        port = port_base
        kill_sandbox(client, name)
        try:
            cid = start_sandbox(client, name, port)
            cdp = await wait_for_cdp("localhost", port)
            print(f"\n  [vanilla] Running...")
            r = await run_vanilla(scenario, cdp)
            results["vanilla"] = r
            status = "OK" if r.success else "FAIL"
            print(f"  [vanilla] {r.elapsed:6.1f}s  {r.steps:2d} steps  [{status}]")
        except Exception as e:
            print(f"  [vanilla] SETUP FAILED: {e}")
            results["vanilla"] = RunResult(scenario.name, "vanilla", 0, 0, False, str(e))
        finally:
            kill_sandbox(client, name)

    # --- FlowPilot cold (generates reflex) ---
    name = f"bench-fp-{scenario.id}"
    port = port_base + 1
    kill_sandbox(client, name)
    try:
        cid = start_sandbox(client, name, port)
        cdp = await wait_for_cdp("localhost", port)
        print(f"\n  [flowpilot cold] Running (first run, generates reflex)...")
        r = await run_flowpilot(scenario, cdp, f"bench-cold-{scenario.id}")
        results["fp_cold"] = r
        status = "OK" if r.success else "FAIL"
        print(f"  [flowpilot cold] {r.elapsed:6.1f}s  {r.steps:2d} steps  [{status}]")
    except Exception as e:
        print(f"  [flowpilot cold] SETUP FAILED: {e}")
        results["fp_cold"] = RunResult(scenario.name, "fp_cold", 0, 0, False, str(e))
    finally:
        kill_sandbox(client, name)

    # --- FlowPilot warm (replays saved reflex) ---
    name = f"bench-fp-warm-{scenario.id}"
    port = port_base + 2
    kill_sandbox(client, name)
    try:
        cid = start_sandbox(client, name, port)
        cdp = await wait_for_cdp("localhost", port)
        print(f"\n  [flowpilot warm] Running (cached reflex replay)...")
        r = await run_flowpilot(scenario, cdp, f"bench-warm-{scenario.id}")
        results["fp_warm"] = r
        status = "OK" if r.success else "FAIL"
        print(f"  [flowpilot warm] {r.elapsed:6.1f}s  {r.steps:2d} steps  [{status}]")
    except Exception as e:
        print(f"  [flowpilot warm] SETUP FAILED: {e}")
        results["fp_warm"] = RunResult(scenario.name, "fp_warm", 0, 0, False, str(e))
    finally:
        kill_sandbox(client, name)

    return results


def print_summary(all_results: list[tuple[Scenario, dict]]):
    """Print the final results table."""
    print(f"\n\n{'='*80}")
    print(f"  BENCHMARK RESULTS")
    print(f"  LLM: {os.environ.get('LLM_PROVIDER', 'gemini')}")
    print(f"{'='*80}\n")

    header = f"  {'Scenario':<28s}  {'Vanilla':>10s}  {'FP Cold':>10s}  {'FP Warm':>10s}  {'Speedup':>8s}"
    print(header)
    print(f"  {'-'*28}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}")

    total_vanilla = 0
    total_fp_warm = 0
    scenarios_with_speedup = 0

    for scenario, results in all_results:
        vanilla = results.get("vanilla")
        fp_cold = results.get("fp_cold")
        fp_warm = results.get("fp_warm")

        def fmt(r):
            if r is None:
                return "  skip  "
            if not r.success:
                return "  FAIL  "
            return f"{r.elapsed:7.1f}s"

        speedup = ""
        if vanilla and fp_warm and vanilla.success and fp_warm.success and vanilla.elapsed > 0:
            ratio = vanilla.elapsed / fp_warm.elapsed
            speedup = f"{ratio:5.1f}x"
            total_vanilla += vanilla.elapsed
            total_fp_warm += fp_warm.elapsed
            scenarios_with_speedup += 1

        print(f"  {scenario.name:<28s}  {fmt(vanilla):>10s}  {fmt(fp_cold):>10s}  {fmt(fp_warm):>10s}  {speedup:>8s}")

    print(f"  {'-'*28}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}")

    if scenarios_with_speedup > 0 and total_fp_warm > 0:
        avg_speedup = total_vanilla / total_fp_warm
        print(f"  {'TOTAL':<28s}  {total_vanilla:7.1f}s  {'':>10s}  {total_fp_warm:7.1f}s  {avg_speedup:5.1f}x")
    print()

    # Success rate
    for label, key in [("Vanilla", "vanilla"), ("FlowPilot Cold", "fp_cold"), ("FlowPilot Warm", "fp_warm")]:
        runs = [r.get(key) for _, r in all_results if r.get(key) is not None]
        if runs:
            passed = sum(1 for r in runs if r.success)
            print(f"  {label}: {passed}/{len(runs)} passed")
    print()


async def main():
    parser = argparse.ArgumentParser(description="FlowPilot Benchmark Suite")
    parser.add_argument("--scenario", type=int, help="Run a single scenario by ID (1-5)")
    parser.add_argument("--only", choices=["fp", "vanilla"], help="Only run one runner type")
    args = parser.parse_args()

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s.id == args.scenario]
        if not scenarios:
            print(f"Unknown scenario ID: {args.scenario}")
            sys.exit(1)

    skip_vanilla = args.only == "fp"

    client = docker.from_env()

    llm_provider = os.environ.get("LLM_PROVIDER", "gemini")
    print(f"FlowPilot Benchmark Suite")
    print(f"LLM: {llm_provider}  |  Scenarios: {len(scenarios)}  |  Sandbox image: {SANDBOX_IMAGE}")

    all_results: list[tuple[Scenario, dict]] = []

    for scenario in scenarios:
        # Each scenario gets its own port range to avoid conflicts
        port_base = 19220 + (scenario.id * 10)
        results = await run_scenario(client, scenario, port_base, skip_vanilla)
        all_results.append((scenario, results))

    print_summary(all_results)


if __name__ == "__main__":
    asyncio.run(main())
