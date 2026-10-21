"""Flow runner — browser-use agent with executable JavaScript reflex scripts.

Architecture
------------
The agent uses browser-use's full toolkit plus custom tools:

  run_reflex(name)           — execute a saved JS script on the browser (no LLM calls)
  save_reflex(name, ...)     — save a JS script after solving a repeatable interaction
  request_human_help(...)    — pause for HITL (auth walls, CAPTCHAs, etc.)

Reflexes are domain-scoped async JavaScript scripts executed via the Actor API's
page.evaluate(). They receive an `inputs` object and can use helpers like waitFor(),
sleep(), typeInto(). On failure, the agent gets the current URL + error context and
takes over from where the script stopped.

The agent writes reflex scripts after completing an interaction, so future runs on
the same domain skip the LLM entirely for known flows.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable

import httpx

from .trace import trace

from browser_use import Agent, Browser, ActionResult, Tools
from browser_use.llm.google import ChatGoogle
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.openai.chat import ChatOpenAI

from .models import Flow
from .llm import get_llm
from .replay import RunControl, replay_plan
from .programs import generate_program

_db = None

def _get_db():
    global _db
    if _db is None:
        from . import db as _db_mod
        _db = _db_mod
    return _db

logger = logging.getLogger(__name__)


async def _async_cdp_get(host: str, port: int, path: str) -> dict | None:
    """Non-blocking CDP HTTP request — replaces http.client calls that block the event loop."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"http://{host}:{port}{path}", headers={"Host": "localhost"})
            if resp.status_code == 200:
                return resp.json()
    except (httpx.HTTPError, Exception):
        pass
    return None


class RunStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    RUNNING = "running"
    PAUSED_FOR_HITL = "paused_for_hitl"
    COMPLETED = "completed"
    FAILED = "failed"


def _get_llm() -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    return get_llm("agent")


def _get_exploration_llm() -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    return get_llm("exploration")


def _get_fast_llm() -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    return get_llm("fast")




# JS snippet that dumps interactive elements as a compact list.
# Returns lines like: "0: input#email[name=email] "Email address""
_DUMP_ELEMENTS_JS = """
() => {
    const els = document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [role="link"], ' +
        '[role="option"], [role="tab"], [role="checkbox"], [role="menuitem"], ' +
        '[contenteditable="true"], [onclick], [tabindex]'
    );
    const lines = [];
    let i = 0;
    for (const el of els) {
        if (i >= 50) { lines.push('... (' + (els.length - 50) + ' more)'); break; }
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const tag = el.tagName.toLowerCase();
        const id = el.id ? '#' + el.id : '';
        const name = el.name ? '[name=' + el.name + ']' : '';
        const type = el.type ? '[type=' + el.type + ']' : '';
        const role = el.getAttribute('role') ? '[role=' + el.getAttribute('role') + ']' : '';
        const cls = el.className && typeof el.className === 'string'
            ? '.' + el.className.split(' ').filter(Boolean).slice(0, 2).join('.')
            : '';
        const text = (el.textContent || el.value || el.placeholder || '').trim().slice(0, 40);
        lines.push(i + ': ' + tag + id + name + type + role + cls + ' "' + text + '"');
        i++;
    }
    return lines.join('\\n');
}
"""


async def _execute_reflex(
    script: str,
    inputs: dict,
    entry_url: str,
    browser_session,
    *,
    observation_policy=None,
) -> tuple[bool, str]:
    """Execute a reflex JS script via the Actor API. No LLM calls.

    Uses page.goto() for navigation (proper CDP navigation with wait),
    then page.evaluate() for the script body. On failure, captures a
    compact DOM snapshot of interactive elements so the agent can
    immediately see what selectors are available and continue.
    """
    if observation_policy is not None:
        observation = await observation_policy.observe(browser_session)
        await observation_policy.require_fresh(browser_session, observation)
    page = await browser_session.must_get_current_page()

    # Navigate to entry_url if we're on a blank page
    if entry_url:
        try:
            current_url = await page.get_url()
            if current_url in ("", "about:blank", "chrome://newtab/"):
                await page.goto(entry_url)
                await asyncio.sleep(1.5)
        except Exception:
            pass

    pre_url = ""
    try:
        pre_url = await page.get_url()
    except Exception:
        pass

    # browser-use's page.evaluate() validates that scripts start with '('.
    # LLMs generate 'async (inputs, ...) => { await ... }' which fails
    # the check. We bypass page.evaluate()'s validation and send the
    # expression directly to CDP Runtime.evaluate with awaitPromise=true.
    trace("execute_reflex.call", script=script, inputs=inputs, entry_url=entry_url, pre_url=pre_url)

    try:
        import json as _json
        arg_strs = [_json.dumps(inputs), _json.dumps(entry_url)]
        expression = f'({script})({", ".join(arg_strs)})'
        session_id = await page._ensure_session()
        timeout = float(os.environ.get("REFLEX_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("REFLEX_TIMEOUT_SECONDS must be positive")
        try:
            result = await asyncio.wait_for(page._client.send.Runtime.evaluate(
                {'expression': expression, 'returnByValue': True, 'awaitPromise': True,
                 'timeout': timeout * 1000},
                session_id=session_id,
            ), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            # Cancelling the Python await alone leaves JavaScript running.
            try:
                await asyncio.wait_for(page._client.send.Runtime.terminateExecution(
                    {}, session_id=session_id), timeout=2)
            except Exception:
                pass
            raise
        if 'exceptionDetails' in result:
            raise RuntimeError(f'JavaScript evaluation failed: {result["exceptionDetails"]}')
        value = result.get("result", {}).get("value")
        if value is False or (isinstance(value, dict) and value.get("success") is False):
            raise RuntimeError(f"Reflex reported failure: {value}")
        if isinstance(value, dict) and value.get("needs_human"):
            return False, "NEEDS_HUMAN: " + json.dumps(value)[:4000]
        trace("execute_reflex.ok", script=script[:500], pre_url=pre_url)
        return True, json.dumps(value, ensure_ascii=False)[:8000] if value is not None else "Reflex executed successfully"
    except Exception as e:
        # A changed URL does not establish that the script's remaining work ran.
        # Dump page state so the agent can pick up where the script stopped
        try:
            fail_url = await page.get_url()
            elements = await page.evaluate(_DUMP_ELEMENTS_JS)
            context = f"URL: {fail_url}\nElements on page:\n{elements}"
        except Exception:
            context = "Could not read page state"
        err_str = str(e) or type(e).__name__
        result_msg = f"SCRIPT EXECUTION FAILED\nError: {err_str}\n{context}"
        trace("execute_reflex.fail", error=err_str, context=context)
        return False, result_msg


async def _repair_reflex(
    broken_script: str,
    error_msg: str,
    inputs: dict,
    entry_url: str,
    browser_session,
    llm,
    max_attempts: int = 2,
    *,
    observation_policy=None,
) -> tuple[bool, str, str | None]:
    """Use a lightweight LLM call to fix a broken reflex script.

    Sends the broken script + DOM dump to the LLM and asks for a corrected
    version. Retries execution up to max_attempts times. Returns
    (success, message, fixed_script_or_None).

    This runs as a "subagent" — a single LLM call that doesn't pollute
    the main agent's conversation context.
    """
    trace("repair_reflex.call", broken_script=broken_script, error_msg=error_msg, inputs=inputs, entry_url=entry_url)

    for attempt in range(max_attempts):
        if observation_policy is not None:
            observation = await observation_policy.observe(browser_session)
            await observation_policy.require_fresh(browser_session, observation)
        # Get fresh DOM state for the repair prompt
        try:
            state = await browser_session.get_browser_state_summary(include_screenshot=False, cached=False)
            if not state.dom_state:
                return False, 'Cannot obtain stable browser state for repair', None
            page = await browser_session.must_get_current_page()
            current_url = await page.get_url()
            elements = await page.evaluate(_DUMP_ELEMENTS_JS)
        except Exception:
            return False, "Cannot read page state for repair", None

        repair_prompt = (
            "You are a JavaScript repair bot. A browser automation script failed. "
            "Fix the script so it works with the actual DOM elements on the page.\n\n"
            f"BROKEN SCRIPT:\n```js\n{broken_script}\n```\n\n"
            f"ERROR: {error_msg}\n\n"
            f"CURRENT PAGE: {current_url}\n"
            f"INTERACTIVE ELEMENTS ON PAGE:\n{elements}\n\n"
            "The script must be a single async arrow function:\n"
            "  async (inputs, entryUrl) => { ... }\n\n"
            "It receives inputs (object with user values) and entryUrl (string).\n"
            "Define any helpers you need (sleep, waitFor, etc.) inside the function.\n\n"
            "IMPORTANT: For autocomplete/suggestion fields, type into the field, "
            "wait for suggestion elements to appear, then click the matching one.\n\n"
            "Reply with ONLY the arrow function. No markdown fences, no explanation. "
            "Update CSS selectors to match the actual elements listed above."
        )

        try:
            fixed_script = await generate_program(llm, {
                "operation": "repair the remaining work of this failed stage",
                "broken_script": broken_script, "error": error_msg[:8000],
                "url": current_url, "elements": elements,
                "input_names": list(inputs),
            })
        except Exception as e:
            logger.warning("Repair LLM call failed: %s", e)
            return False, f"Repair LLM call failed: {e}", None

        trace("repair_reflex.llm_response", attempt=attempt + 1, prompt=repair_prompt, fixed_script=fixed_script)

        # Try executing the fixed script
        success, msg = await _execute_reflex(
            script=fixed_script,
            inputs=inputs,
            entry_url=entry_url,
            browser_session=browser_session,
            **({'observation_policy': observation_policy} if observation_policy is not None else {}),
        )

        if success:
            return True, msg, fixed_script
        if "NEEDS_HUMAN" in msg or "NEEDS_AUTH" in msg:
            return False, msg, None

        # Update error for next attempt
        broken_script = fixed_script
        error_msg = msg
        logger.info("Repair attempt %d/%d failed: %s", attempt + 1, max_attempts, msg)

    trace("repair_reflex.exhausted", max_attempts=max_attempts, last_error=error_msg[:500])
    return False, f"Repair failed after {max_attempts} attempts: {error_msg}", None


async def _generate_reflex(
    flow: Flow,
    inputs: dict,
    browser_session,
    llm,
) -> tuple[bool, str, str | None]:
    """Generate a reflex script from scratch by examining the live page.

    Navigates to the entry URL, dumps the DOM, asks the LLM to write a JS
    script that completes the task, then executes it. If it works, returns
    the script for saving. This is a single LLM call — much faster than
    letting the agent loop through 10+ steps.

    Returns (success, message, script_or_None).
    """
    entry_url = flow.entry_url or ""
    if not entry_url:
        return False, "No entry URL to navigate to", None

    page = await browser_session.must_get_current_page()

    # Navigate and wait for page to settle
    try:
        await page.goto(entry_url)
        await asyncio.sleep(2)
    except Exception as e:
        return False, f"Failed to navigate to {entry_url}: {e}", None

    # Dump interactive elements
    try:
        elements = await page.evaluate(_DUMP_ELEMENTS_JS)
        current_url = await page.get_url()
    except Exception as e:
        return False, f"Failed to read page state: {e}", None

    # Build input description
    input_desc = (
        ", ".join(f"{k}={v!r}" for k, v in inputs.items()) if inputs else "none"
    )

    generate_prompt = (
        "You are a browser automation script writer. Write a JavaScript script that "
        "completes the following task on the current page.\n\n"
        f"TASK: {flow.task}\n"
        f"USER INPUTS: {input_desc}\n"
        f"CURRENT PAGE: {current_url}\n"
        f"INTERACTIVE ELEMENTS ON PAGE:\n{elements}\n\n"
        "Output a single async arrow function:\n"
        "  async (inputs, entryUrl) => { ... }\n\n"
        "It receives inputs (object with user values) and entryUrl (string).\n"
        "Define any helpers you need (sleep, waitFor, etc.) inside the function.\n\n"
        "Use the CSS selectors from the elements list above.\n"
        "Add sleep() calls after actions that trigger navigation or XHR.\n"
        "For autocomplete/suggestion fields: type into the field, wait for "
        "suggestion elements to appear, then click the matching one.\n"
        "If the task requires login/auth, end with: throw new Error('NEEDS_AUTH');\n\n"
        "Reply with ONLY the arrow function. No markdown fences, no explanation."
    )

    try:
        script = await generate_program(llm, {
            "operation": "complete the entire task", "task": flow.task,
            "input_names": list(inputs), "url": current_url, "elements": elements,
        })
    except Exception as e:
        return False, f"LLM generation failed: {e}", None

    if not script:
        return False, "LLM returned empty script", None

    trace("generate_reflex.llm_response", prompt=generate_prompt, script=script, task=flow.task)

    # Execute the generated script
    success, msg = await _execute_reflex(
        script=script,
        inputs=inputs,
        entry_url=entry_url,
        browser_session=browser_session,
    )

    if success:
        return True, "Generated and executed successfully", script

    # Try one repair pass
    repaired, repair_msg, fixed = await _repair_reflex(
        broken_script=script,
        error_msg=msg,
        inputs=inputs,
        entry_url=entry_url,
        browser_session=browser_session,
        llm=llm,
        max_attempts=1,
    )

    if repaired and fixed:
        return True, "Generated, repaired, and executed successfully", fixed

    return False, f"Generated script failed: {repair_msg}", None


def _tree_reflex_order(tree: list[dict]) -> list[str]:
    """Return reflex names from the exploration tree in depth-first order."""
    order: list[str] = []

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node.get("reflex_name"):
                order.append(node["reflex_name"])
            _walk(node.get("children", []))

    _walk(tree)
    return order


def _build_task(flow: Flow, inputs: dict, domain_reflexes: list[dict]) -> str:
    """Build a concise agent task string."""
    lines = [f"Task: {flow.task}"]
    if flow.entry_url:
        lines.append(f"Start: {flow.entry_url}")
    if inputs:
        for k, v in inputs.items():
            lines.append(f"  {k} = {v!r}")

    if domain_reflexes:
        lines.append(f"\nReflexes for {flow.domain} — execute IN THIS ORDER:")
        for i, r in enumerate(domain_reflexes, 1):
            lines.append(f"  {i}. run_reflex('{r['name']}') — {r['description']}")
        lines.append("Call them sequentially. Do not skip or reorder.")

    return "\n".join(lines)


# System prompt extension — only FlowPilot-specific rules.
# browser-use's default prompt already handles autocomplete, batching, popups, loops.
_AGENT_RULES = """\
FLOWPILOT RULES:
- REFLEXES FIRST: If a reflex exists, call run_reflex(name). It executes JS directly — \
zero LLM calls. If it fails, you get the error + element list to take over.
- AUTH/LOGIN: If you see a login form, password field, OTP/SMS prompt, or CAPTCHA, \
call request_human_help(). NEVER type passwords or credentials yourself.
- SAVE REFLEXES: After completing a repeatable interaction, call save_reflex() with a \
JS script. Helpers available: waitFor(sel), sleep(ms), typeInto(el, text), \
fillInput(el, value), acceptSuggestion(sel), typeAndSelect(el, text, opts). \
For fields requiring suggestion selection (locations, addresses), use typeAndSelect().
- CAPTCHA: CAPTCHAs are NOT auto-solved. Call request_human_help(category="captcha") immediately.
- CLICK FALLBACK: If click_element fails, try send_keys("Tab Tab Enter") to navigate and activate.
- FORM PACING: On dynamic forms with autocomplete, fill ONE field per action batch. \
Don't batch dependent fields.\
"""


class FlowRunner:

    async def run_flow(
        self,
        flow: Flow,
        inputs: dict,
        user_id: str,
        sandbox_id: str,
        cdp_url: str,
        run_id: str,
        progress_callback: Callable[[RunStatus, str], Awaitable[None]] | None = None,
        storage_state: dict | None = None,
        agent_ready_callback: Callable | None = None,
    ) -> dict:
        """Run a flow. Reflexes execute JS directly; agent handles the rest."""

        async def _update(status: RunStatus, message: str) -> None:
            if progress_callback:
                await progress_callback(status, message)
            logger.info("Run %s: [%s] %s", run_id, status.value, message)

        # ------------------------------------------------------------------
        # CDP readiness
        # ------------------------------------------------------------------
        await _update(RunStatus.CONNECTING, "Starting browser sandbox...")
        _cdp_hp = cdp_url.split("//")[1].split("/")[0]
        _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
        _cdp_info = None
        for _attempt in range(20):
            try:
                _cdp_info = await _async_cdp_get(_cdp_h, int(_cdp_p), "/json/version")
                if _cdp_info is not None:
                    break
            except Exception:
                pass
            if _attempt < 19:
                await asyncio.sleep(2)
        else:
            raise RuntimeError(f"CDP not reachable at {cdp_url} after 40s")

        _ws_path = "/" + "/".join(_cdp_info["webSocketDebuggerUrl"].split("/")[3:])
        import socket
        address = await asyncio.to_thread(socket.gethostbyname, _cdp_h)
        cdp_url = f"ws://{address}:{_cdp_p}{_ws_path}"

        # ------------------------------------------------------------------
        # Load domain reflexes — ordered by the exploration tree if available,
        # otherwise fall back to DB creation order.
        # ------------------------------------------------------------------
        domain_reflexes, replay_complete = await replay_plan(_get_db(), flow)

        # ------------------------------------------------------------------
        # Agent run
        # ------------------------------------------------------------------
        browser: Browser | None = None
        _ss_tmp_path: str | None = None
        try:
            await _update(RunStatus.CONNECTING, "Connecting to sandbox browser via CDP")

            # browser-use's StorageStateWatchdog only loads from file paths, not dicts.
            # Write the storage_state dict to a temp file so cookies are actually injected.
            ss_arg = None
            if storage_state:
                from .session_utils import storage_state_to_file
                _ss_tmp_path = storage_state_to_file(storage_state)
                ss_arg = _ss_tmp_path

            browser = Browser(
                cdp_url=cdp_url,
                storage_state=ss_arg,
            )
            await browser.start()

            if storage_state:
                await _update(
                    RunStatus.CONNECTING,
                    f"Injected {len(storage_state.get('cookies', []))} cookies from saved session",
                )

            _paused = [False]
            control = RunControl(_paused)
            if agent_ready_callback:
                await agent_ready_callback(control, browser, flow, _paused)

            async def human_help(reason: str, category: str = "other"):
                control.pause()
                await _update(RunStatus.PAUSED_FOR_HITL, f"[{category}] {reason}")
                await control.checkpoint()
                await _update(RunStatus.RUNNING, "Human assistance complete; continuing")

            await _update(RunStatus.RUNNING, "Connected. Checking replay plan...")
            llm = None  # Successful warm replay requires neither credentials nor LLM calls.

            # ----------------------------------------------------------
            # Fast path: if a saved reflex exists, execute it directly.
            # If no reflex exists, try generating one from the DOM.
            # Only fall through to the full agent if both fail.
            # ----------------------------------------------------------
            completed_names = []
            stage_results = []
            if replay_complete:
                for reflex in domain_reflexes:
                    await control.checkpoint()
                    await _update(
                        RunStatus.RUNNING, f"Trying reflex '{reflex['name']}'..."
                    )
                    success, msg = await _execute_reflex(
                        script=reflex["script"],
                        inputs=inputs,
                        entry_url=flow.entry_url or "",
                        browser_session=browser,
                    )
                    await control.checkpoint()
                    if not success and "NEEDS_HUMAN" not in msg and "NEEDS_AUTH" not in msg:
                        from .blockers import classify_blocker
                        category = await classify_blocker(msg)
                        if category:
                            await human_help("Browser failure classified as " + category, category)
                            break
                    if not success and ("NEEDS_HUMAN" in msg or "NEEDS_AUTH" in msg):
                        await human_help(msg[:2000], "auth" if "NEEDS_AUTH" in msg else "other")
                        # A failed stage may have partially acted. Let the agent inspect
                        # the resumed state instead of automatically repeating it.
                        break
                    if not success:
                        llm = llm or _get_fast_llm()
                        success, msg, fixed_script = await _repair_reflex(
                            broken_script=reflex["script"], error_msg=msg,
                            inputs=inputs, entry_url=flow.entry_url or "",
                            browser_session=browser, llm=llm,
                        )
                        if success and fixed_script:
                            await _get_db().upsert_reflex(
                                domain=flow.domain, flow_id=flow.id, name=reflex["name"],
                                description=reflex["description"], script=fixed_script,
                                pause_after=reflex.get("pause_after"), tags=reflex.get("tags", []),
                            )
                    if not success:
                        # Dependent stages cannot run after a failed prerequisite.
                        if "NEEDS_HUMAN" in msg or "NEEDS_AUTH" in msg:
                            await human_help(msg[:2000])
                        break
                    completed_names.append(reflex["name"])
                    stage_results.append({"name": reflex["name"], "result": msg})
                    if reflex.get("pause_after"):
                        await human_help(f"Stage '{reflex['name']}' needs human assistance", reflex["pause_after"])
                else:
                    await control.checkpoint()
                    await _update(RunStatus.COMPLETED, f"Completed all {len(stage_results)} replay stages")
                    return {"raw_result": stage_results, "flow_name": flow.name,
                            "inputs": inputs, "execution_mode": "replay",
                            "completed_at": datetime.now(timezone.utc).isoformat()}

            elif not domain_reflexes and not json.loads(flow.exploration_tree or "[]") and flow.entry_url:
                # No saved reflexes — try generating one from the live page
                await _update(
                    RunStatus.RUNNING,
                    "No reflexes found. Generating script from page...",
                )
                gen_ok, gen_msg, gen_script = await _generate_reflex(
                    flow=flow,
                    inputs=inputs,
                    browser_session=browser,
                    llm=_get_fast_llm(),
                )
                if gen_ok and gen_script:
                    # Save it for next time
                    await _get_db().upsert_reflex(
                        domain=flow.domain,
                        flow_id=flow.id,
                        name="auto_generated",
                        description=f"Auto-generated for: {flow.task}",
                        script=gen_script,
                        pause_after=None,
                        tags=["auto"],
                    )
                    await _update(
                        RunStatus.COMPLETED, "Generated script completed flow"
                    )
                    return {
                        "raw_result": "Completed via auto-generated reflex",
                        "flow_name": flow.name,
                        "inputs": inputs,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                logger.info(
                    "Script generation failed (%s), falling through to agent", gen_msg
                )

            # ----------------------------------------------------------
            # Slow path: full agent loop (only if fast paths failed)
            # ----------------------------------------------------------
            await _update(RunStatus.RUNNING, "Starting agent loop...")
            await control.checkpoint()
            llm = _get_llm()
            remaining_reflexes = [r for r in domain_reflexes if r["name"] not in completed_names]
            task = _build_task(flow, inputs, remaining_reflexes)
            if completed_names:
                task += "\nAlready completed stages (do not repeat): " + json.dumps(completed_names)
            task += "\nContinue from the current page. Inspect state before retrying any partially failed stage."

            def _on_step(state_summary, agent_output, step_num):
                try:
                    state = getattr(agent_output, "current_state", None)
                    goal = (
                        getattr(state, "next_goal", None)
                        or getattr(state, "next_step", None)
                        or "processing"
                    )
                    msg = f"Step {step_num}: {goal}"
                except AttributeError:
                    msg = f"Step {step_num}: processing"

                async def _send():
                    if not _paused[0]:
                        await _update(RunStatus.RUNNING, msg)

                asyncio.create_task(_send())

            tools = Tools()

            # ----------------------------------------------------------
            # run_reflex: execute saved JS directly on the browser
            # ----------------------------------------------------------
            @tools.action(
                description=(
                    "Execute a saved reflex by name. Runs a JS script directly on the "
                    "page — fills forms, clicks buttons, handles the interaction instantly. "
                    "Call this FIRST if a reflex exists for what you need to do. "
                    "On failure you get the current URL, error, and a list of interactive "
                    "elements on the page so you can continue immediately."
                )
            )
            async def run_reflex(name: str) -> ActionResult:
                await control.checkpoint()
                if name in completed_names:
                    return ActionResult(error=f"Stage '{name}' already completed; do not repeat it.")
                reflex = await _get_db().get_reflex(flow.domain, name, flow_id=flow.id)
                if reflex is None:
                    reflex = next((r for r in remaining_reflexes if r["name"] == name), None)
                if not reflex:
                    return ActionResult(
                        error=f"No reflex '{name}' for '{flow.domain}'."
                    )

                await _update(RunStatus.RUNNING, f"Executing reflex '{name}'...")
                success, msg = await _execute_reflex(
                    script=reflex["script"],
                    inputs=inputs,
                    entry_url=flow.entry_url or "",
                    browser_session=browser,
                )

                if success:
                    completed_names.append(name)
                    if reflex.get("pause_after"):
                        await human_help(f"Stage '{name}' needs human assistance", reflex["pause_after"])
                    return ActionResult(
                        extracted_content=f"Reflex '{name}' done: {msg}. Continue from current page."
                    )

                # Reflex failed — try auto-repair with a subagent LLM call.
                if "NEEDS_HUMAN" in msg or "NEEDS_AUTH" in msg:
                    await human_help(msg[:2000])
                    return ActionResult(extracted_content="Human assisted; inspect current state before retrying the partial stage.")
                # This keeps the DOM dump + repair loop out of the main agent context.
                await _update(
                    RunStatus.RUNNING,
                    f"Reflex '{name}' failed, attempting auto-repair...",
                )
                repaired, repair_msg, fixed_script = await _repair_reflex(
                    broken_script=reflex["script"],
                    error_msg=msg,
                    inputs=inputs,
                    entry_url=flow.entry_url or "",
                    browser_session=browser,
                    llm=llm,
                )

                if repaired and fixed_script:
                    # Save the fixed script so it works next time
                    await _get_db().upsert_reflex(
                        domain=flow.domain,
                        flow_id=flow.id,
                        name=name,
                        description=reflex["description"],
                        script=fixed_script,
                        pause_after=reflex.get("pause_after"),
                        tags=(
                            json.loads(reflex.get("tags", "[]"))
                            if isinstance(reflex.get("tags"), str)
                            else reflex.get("tags", [])
                        ),
                    )
                    completed_names.append(name)
                    if reflex.get("pause_after"):
                        await human_help(f"Stage '{name}' needs human assistance", reflex["pause_after"])
                    return ActionResult(
                        extracted_content=f"Reflex '{name}' was broken but auto-repaired and executed. "
                        "Updated script saved. Continue from current page."
                    )

                # Repair also failed — hand back to the main agent with DOM context
                return ActionResult(
                    extracted_content=f"Reflex '{name}' failed and auto-repair failed:\n{repair_msg}\n"
                    "Continue manually from current page state."
                )

            # ----------------------------------------------------------
            # save_reflex: save a JS script for future instant replay
            # ----------------------------------------------------------
            @tools.action(
                description=(
                    "Save a JavaScript reflex script after completing a repeatable interaction. "
                    "The script must be an async arrow function: "
                    "async (inputs, entryUrl) => { ... }. "
                    "It receives inputs (object with user values) and entryUrl (string). "
                    "Define any helpers (sleep, waitFor, etc.) inside the function. "
                    "For autocomplete fields: type into the field, wait for suggestions, click one. "
                    "Name: snake_case, e.g. 'fill_booking_form'."
                )
            )
            async def save_reflex(
                name: str,
                description: str,
                script: str,
                tags: list[str] | None = None,
                pause_after_auth: bool = False,
            ) -> ActionResult:
                pause_after = "auth" if pause_after_auth else None
                await _get_db().upsert_reflex(
                    domain=flow.domain,
                    flow_id=flow.id,
                    name=name,
                    description=description,
                    script=script,
                    pause_after=pause_after,
                    tags=tags or [],
                )
                return ActionResult(
                    extracted_content=f"Reflex '{flow.domain}/{name}' saved."
                )

            # ----------------------------------------------------------
            # request_human_help: HITL pause
            # ----------------------------------------------------------
            @tools.action(
                description=(
                    "Request human help. ONLY for: login forms, CAPTCHAs, OTP/SMS, "
                    "consent dialogs requiring credentials. NEVER for: loading pages, "
                    "dropdowns, cookie banners, forms you can fill."
                )
            )
            async def request_human_help(
                reason: str, category: str = "other"
            ) -> ActionResult:
                await human_help(reason, category)
                return ActionResult(
                    extracted_content="Human assisted. Continue from current page state."
                )

            if os.getenv("AGENT_DRIVER", "browser-use") == "pi":
                from .pi_browser import drive_browser
                result = await drive_browser(browser, task, inputs, flow.entry_url or "",
                                             control, human_help, run_reflex, save_reflex)
                await _update(RunStatus.COMPLETED, "Pi completed and verified the flow")
                return {"raw_result": result, "flow_name": flow.name, "inputs": inputs,
                        "execution_mode": "pi", "completed_at": datetime.now(timezone.utc).isoformat()}

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
                tools=tools,
                page_extraction_llm=_get_fast_llm(),
                extend_system_message=_AGENT_RULES,
            )

            control.agent = agent
            await control.checkpoint()

            await _update(RunStatus.RUNNING, f"Agent running flow '{flow.name}'...")

            result = await agent.run(max_steps=30)

            if hasattr(result, "is_successful") and not result.is_successful():
                errors = result.errors() if hasattr(result, "errors") else []
                error_msg = (
                    "; ".join(str(e) for e in errors)
                    if errors
                    else "Agent failed to complete the task"
                )
                await _update(RunStatus.FAILED, f"Flow failed: {error_msg}")
                raise RuntimeError(error_msg)

            await _update(RunStatus.COMPLETED, "Flow completed successfully")

            return {
                "raw_result": str(result),
                "flow_name": flow.name,
                "inputs": inputs,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            error_msg = f"Flow failed: {e}"
            logger.error("Run %s failed: %s", run_id, e, exc_info=True)
            await _update(RunStatus.FAILED, error_msg)
            raise

        finally:
            if browser is not None:
                try:
                    await browser.stop()
                except Exception:
                    pass
            if _ss_tmp_path and os.path.exists(_ss_tmp_path):
                try:
                    os.unlink(_ss_tmp_path)
                except Exception:
                    pass
