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
import http.client
import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable

from browser_use import Agent, Browser, ActionResult, Tools
from browser_use.llm.google import ChatGoogle
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.openai.chat import ChatOpenAI

from . import db as _db
from .models import Flow

logger = logging.getLogger(__name__)


class RunStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    RUNNING = "running"
    PAUSED_FOR_HITL = "paused_for_hitl"
    COMPLETED = "completed"
    FAILED = "failed"


def _get_llm() -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY env var is required when LLM_PROVIDER=gemini")
        model = os.environ.get("GEMINI_MODEL") or "gemini-3-pro-preview"
        logger.info("Using Gemini model: %s", model)
        return ChatGoogle(model=model, api_key=api_key, max_output_tokens=65536)
    elif provider in ("claude", "anthropic"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY env var is required when LLM_PROVIDER=claude")
        model = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514"
        logger.info("Using Anthropic model: %s", model)
        return ChatAnthropic(model=model, api_key=api_key)
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY env var is required when LLM_PROVIDER=openai")
        model = os.environ.get("OPENAI_MODEL") or "gpt-4o"
        logger.info("Using OpenAI model: %s", model)
        return ChatOpenAI(model=model, api_key=api_key)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER='{provider}'. Valid options: gemini, claude, anthropic, openai"
        )


# JavaScript wrapper that provides helpers to reflex scripts.
# The user script is injected into {user_script} and runs as the body of an
# async IIFE receiving (inputs, entryUrl).
_REFLEX_JS_WRAPPER = """
async (inputs, entryUrl) => {
    // Wait for a selector to appear in the DOM
    const waitFor = (sel, timeout = 5000) => new Promise((resolve, reject) => {
        const existing = document.querySelector(sel);
        if (existing) return resolve(existing);
        const observer = new MutationObserver(() => {
            const el = document.querySelector(sel);
            if (el) { observer.disconnect(); resolve(el); }
        });
        observer.observe(document.body, {childList: true, subtree: true});
        setTimeout(() => { observer.disconnect(); reject(new Error('waitFor timeout: ' + sel)); }, timeout);
    });

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    // Type char-by-char to trigger autocomplete/React onChange
    const typeInto = async (el, text, delay = 30) => {
        el.focus();
        el.value = '';
        for (const ch of text) {
            el.value += ch;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles: true}));
            await sleep(delay);
        }
        el.dispatchEvent(new Event('change', {bubbles: true}));
    };

    // Set value instantly (for fields that don't need autocomplete)
    const fillInput = (el, value) => {
        el.focus();
        el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    };

    // Default selectors for suggestion/autocomplete dropdowns
    const SUGGESTION_SELS = '[role="option"], [role="listbox"] li, [role="listbox"] [role="presentation"], .pac-item, [class*="suggestion"], [class*="Suggestion"], [class*="dropdown"] li, [class*="autocomplete"] li, [class*="Autocomplete"] li, ul[id*="listbox"] li';

    // Wait for suggestion dropdown and click first match
    const acceptSuggestion = async (sel, timeout = 3000) => {
        const selector = sel || SUGGESTION_SELS;
        const el = await waitFor(selector, timeout);
        el.click();
    };

    // Type into a field and select from the suggestion dropdown.
    // This is the go-to for location pickers, address fields, etc. where
    // you MUST click a suggestion for the value to be accepted.
    //
    // Usage:
    //   await typeAndSelect(el, 'JFK Airport');
    //   await typeAndSelect(el, inputs.pickup, {match: 'Terminal 4'});
    //   await typeAndSelect(el, inputs.dropoff, {suggestionSel: '.uber-suggestion'});
    //
    // Options:
    //   match       — substring to match in suggestion text (picks best match instead of first)
    //   suggestionSel — custom CSS selector for suggestion items
    //   timeout     — how long to wait for suggestions (default 5000ms)
    //   typeDelay   — delay between keystrokes (default 40ms)
    //   retries     — retry typing + selecting if suggestions don't appear (default 2)
    const typeAndSelect = async (el, text, opts = {}) => {
        const {
            match = null,
            suggestionSel = null,
            timeout = 5000,
            typeDelay = 40,
            retries = 2,
        } = opts;
        const selector = suggestionSel || SUGGESTION_SELS;

        for (let attempt = 0; attempt <= retries; attempt++) {
            // Clear and type
            el.focus();
            el.value = '';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            await sleep(100);

            // Type char by char
            for (const ch of text) {
                el.value += ch;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles: true}));
                await sleep(typeDelay);
            }
            el.dispatchEvent(new Event('change', {bubbles: true}));

            // Wait for suggestions to appear
            let suggestions;
            const deadline = Date.now() + timeout;
            while (Date.now() < deadline) {
                suggestions = document.querySelectorAll(selector);
                // Filter to only visible suggestions
                suggestions = Array.from(suggestions).filter(s => {
                    const r = s.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (suggestions.length > 0) break;
                await sleep(200);
            }

            if (!suggestions || suggestions.length === 0) {
                if (attempt < retries) {
                    // Retry: clear and retype (sometimes the first attempt doesn't trigger)
                    el.value = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    await sleep(500);
                    continue;
                }
                throw new Error('typeAndSelect: no suggestions appeared for "' + text + '" (selector: ' + selector + ')');
            }

            // Pick the best matching suggestion, or first if no match specified
            let picked = suggestions[0];
            if (match) {
                const lower = match.toLowerCase();
                const found = suggestions.find(s => (s.textContent || '').toLowerCase().includes(lower));
                if (found) picked = found;
            }

            picked.click();
            await sleep(300);

            // Verify the selection stuck — the input should have changed from what we typed
            // (autocomplete fields often replace the value after clicking a suggestion)
            const newValue = el.value;
            if (newValue && newValue !== text) {
                // Selection was accepted — value was replaced by the autocomplete
                return newValue;
            }
            // Even if value didn't change, the click might have set a hidden field.
            // Check if suggestions dismissed (good sign).
            await sleep(200);
            const remaining = document.querySelectorAll(selector);
            const visible = Array.from(remaining).filter(s => {
                const r = s.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            if (visible.length === 0) {
                // Dropdown closed — selection was accepted
                return newValue || text;
            }

            // Suggestions still showing — try clicking again or retry
            if (attempt < retries) continue;
            return newValue || text;
        }
    };

    // Run the user's reflex script
    __REFLEX_SCRIPT__
}
"""


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
) -> tuple[bool, str]:
    """Execute a reflex JS script via the Actor API. No LLM calls.

    Uses page.goto() for navigation (proper CDP navigation with wait),
    then page.evaluate() for the script body. On failure, captures a
    compact DOM snapshot of interactive elements so the agent can
    immediately see what selectors are available and continue.
    """
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

    # Inject the user script into our wrapper
    wrapped = _REFLEX_JS_WRAPPER.replace("__REFLEX_SCRIPT__", script)

    try:
        await page.evaluate(wrapped, inputs, entry_url)
        return True, "Reflex executed successfully"
    except Exception as e:
        # Dump page state so the agent can pick up where the script stopped
        try:
            fail_url = await page.get_url()
            elements = await page.evaluate(_DUMP_ELEMENTS_JS)
            context = f"URL: {fail_url}\nElements on page:\n{elements}"
        except Exception:
            context = "Could not read page state"
        return False, f"Script error: {e}\n{context}"


async def _repair_reflex(
    broken_script: str,
    error_msg: str,
    inputs: dict,
    entry_url: str,
    browser_session,
    llm,
    max_attempts: int = 2,
) -> tuple[bool, str, str | None]:
    """Use a lightweight LLM call to fix a broken reflex script.

    Sends the broken script + DOM dump to the LLM and asks for a corrected
    version. Retries execution up to max_attempts times. Returns
    (success, message, fixed_script_or_None).

    This runs as a "subagent" — a single LLM call that doesn't pollute
    the main agent's conversation context.
    """
    for attempt in range(max_attempts):
        # Get fresh DOM state for the repair prompt
        try:
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
            "The script runs as an async function body receiving (inputs, entryUrl) "
            "and helpers: waitFor(sel, timeout), sleep(ms), typeInto(el, text, delay), "
            "fillInput(el, value), acceptSuggestion(sel, timeout), "
            "typeAndSelect(el, text, opts?) — for fields requiring suggestion selection.\n\n"
            "IMPORTANT: For location/address/search fields where you MUST pick from a "
            "suggestion dropdown, use typeAndSelect() instead of typeInto()+acceptSuggestion().\n\n"
            "Reply with ONLY the fixed JavaScript code, no markdown fences, no explanation. "
            "Update the CSS selectors to match the actual elements listed above."
        )

        try:
            from browser_use.llm.messages import UserMessage
            response = await llm.ainvoke([UserMessage(content=repair_prompt)])
            fixed_script = response.completion.strip()
            # Strip markdown fences if the model included them anyway
            if fixed_script.startswith("```"):
                fixed_script = "\n".join(fixed_script.split("\n")[1:])
            if fixed_script.endswith("```"):
                fixed_script = "\n".join(fixed_script.split("\n")[:-1])
            fixed_script = fixed_script.strip()
        except Exception as e:
            logger.warning("Repair LLM call failed: %s", e)
            return False, f"Repair LLM call failed: {e}", None

        # Try executing the fixed script
        success, msg = await _execute_reflex(
            script=fixed_script,
            inputs=inputs,
            entry_url=entry_url,
            browser_session=browser_session,
        )

        if success:
            return True, "Repaired and executed successfully", fixed_script

        # Update error for next attempt
        broken_script = fixed_script
        error_msg = msg
        logger.info("Repair attempt %d/%d failed: %s", attempt + 1, max_attempts, msg)

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
    input_desc = ", ".join(f"{k}={v!r}" for k, v in inputs.items()) if inputs else "none"

    generate_prompt = (
        "You are a browser automation script writer. Write a JavaScript script that "
        "completes the following task on the current page.\n\n"
        f"TASK: {flow.task}\n"
        f"USER INPUTS: {input_desc}\n"
        f"CURRENT PAGE: {current_url}\n"
        f"INTERACTIVE ELEMENTS ON PAGE:\n{elements}\n\n"
        "The script runs as an async function body receiving (inputs, entryUrl) "
        "and helpers:\n"
        "  waitFor(sel, timeout=5000) — wait for CSS selector to appear, returns element\n"
        "  sleep(ms) — wait\n"
        "  typeInto(el, text, delay=30) — type char-by-char (triggers autocomplete)\n"
        "  fillInput(el, value) — set value instantly\n"
        "  acceptSuggestion(sel?, timeout=3000) — wait for dropdown, click first match\n"
        "  typeAndSelect(el, text, opts?) — TYPE AND PICK FROM SUGGESTIONS in one call.\n"
        "    For ANY field that requires selecting from a dropdown (locations, addresses,\n"
        "    search fields), you MUST use typeAndSelect. Typing alone won't work — the app\n"
        "    requires clicking a suggestion. opts: {match, suggestionSel, timeout, typeDelay, retries}\n\n"
        "Access user inputs via `inputs.key_name`. The page is already loaded at entryUrl.\n"
        "Use the CSS selectors from the elements list above (id, name, class attributes).\n"
        "Add sleep() calls after actions that trigger navigation or XHR.\n"
        "If the task requires login/auth, end with: throw new Error('NEEDS_AUTH');\n\n"
        "Reply with ONLY the JavaScript code. No markdown fences, no explanation."
    )

    try:
        from browser_use.llm.messages import UserMessage
        response = await llm.ainvoke([UserMessage(content=generate_prompt)])
        script = response.completion.strip()
        # Strip markdown fences
        if script.startswith("```"):
            script = "\n".join(script.split("\n")[1:])
        if script.endswith("```"):
            script = "\n".join(script.split("\n")[:-1])
        script = script.strip()
    except Exception as e:
        return False, f"LLM generation failed: {e}", None

    if not script:
        return False, "LLM returned empty script", None

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
For fields requiring suggestion selection (locations, addresses), use typeAndSelect().\
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
                _conn = http.client.HTTPConnection(_cdp_h, int(_cdp_p), timeout=3)
                _conn.request("GET", "/json/version", headers={"Host": "localhost"})
                _resp = _conn.getresponse()
                if _resp.status == 200:
                    _cdp_info = json.loads(_resp.read())
                    break
            except (OSError, http.client.HTTPException):
                pass
            if _attempt < 19:
                await asyncio.sleep(2)
        else:
            raise RuntimeError(f"CDP not reachable at {cdp_url} after 40s")

        _ws_path = "/" + "/".join(_cdp_info["webSocketDebuggerUrl"].split("/")[3:])
        cdp_url = f"ws://{_cdp_h}:{_cdp_p}{_ws_path}"

        # ------------------------------------------------------------------
        # Load domain reflexes — ordered by the exploration tree if available,
        # otherwise fall back to DB creation order.
        # ------------------------------------------------------------------
        all_reflexes = await _db.list_reflexes_for_domain(flow.domain)
        tree = json.loads(flow.exploration_tree) if flow.exploration_tree else []
        if tree:
            tree_order = _tree_reflex_order(tree)
            by_name = {r["name"]: r for r in all_reflexes}
            # Tree-ordered first, then any reflexes not in the tree appended at the end
            domain_reflexes = [by_name[n] for n in tree_order if n in by_name]
            domain_reflexes += [r for r in all_reflexes if r["name"] not in {n for n in tree_order}]
        else:
            domain_reflexes = all_reflexes

        # ------------------------------------------------------------------
        # Agent run
        # ------------------------------------------------------------------
        browser: Browser | None = None
        try:
            await _update(RunStatus.CONNECTING, "Connecting to sandbox browser via CDP")
            browser = Browser(
                cdp_url=cdp_url,
                storage_state=storage_state,
                headers={"Host": "localhost"},
                # Give autocomplete dropdowns time to appear after typing.
                # Default 0.5s is too fast — most location/address pickers
                # need a network round-trip before rendering suggestions.
                wait_between_actions=2.0,
                wait_for_network_idle_page_load_time=1.0,
            )

            if storage_state:
                await _update(
                    RunStatus.CONNECTING,
                    f"Injected {len(storage_state.get('cookies', []))} cookies from saved session",
                )

            await _update(RunStatus.RUNNING, "Connected. Launching agent...")

            llm = _get_llm()

            # ----------------------------------------------------------
            # Fast path: if a saved reflex exists, execute it directly.
            # If no reflex exists, try generating one from the DOM.
            # Only fall through to the full agent if both fail.
            # ----------------------------------------------------------
            if domain_reflexes:
                # Try the first matching reflex
                for reflex in domain_reflexes:
                    await _update(RunStatus.RUNNING, f"Trying reflex '{reflex['name']}'...")
                    success, msg = await _execute_reflex(
                        script=reflex["script"],
                        inputs=inputs,
                        entry_url=flow.entry_url or "",
                        browser_session=browser,
                    )
                    if success:
                        await _update(RunStatus.COMPLETED, f"Reflex '{reflex['name']}' completed flow")
                        return {
                            "raw_result": f"Completed via reflex '{reflex['name']}'",
                            "flow_name": flow.name,
                            "inputs": inputs,
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    # Try auto-repair
                    await _update(RunStatus.RUNNING, f"Reflex '{reflex['name']}' failed, repairing...")
                    repaired, _, fixed_script = await _repair_reflex(
                        broken_script=reflex["script"],
                        error_msg=msg,
                        inputs=inputs,
                        entry_url=flow.entry_url or "",
                        browser_session=browser,
                        llm=llm,
                    )
                    if repaired and fixed_script:
                        await _db.upsert_reflex(
                            domain=flow.domain,
                            name=reflex["name"],
                            description=reflex["description"],
                            script=fixed_script,
                            pause_after=reflex.get("pause_after"),
                            tags=json.loads(reflex.get("tags", "[]")) if isinstance(reflex.get("tags"), str) else reflex.get("tags", []),
                        )
                        await _update(RunStatus.COMPLETED, f"Reflex '{reflex['name']}' repaired and completed flow")
                        return {
                            "raw_result": f"Completed via repaired reflex '{reflex['name']}'",
                            "flow_name": flow.name,
                            "inputs": inputs,
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    logger.info("Reflex '%s' repair failed, trying next or falling through", reflex["name"])

            elif flow.entry_url:
                # No saved reflexes — try generating one from the live page
                await _update(RunStatus.RUNNING, "No reflexes found. Generating script from page...")
                gen_ok, gen_msg, gen_script = await _generate_reflex(
                    flow=flow,
                    inputs=inputs,
                    browser_session=browser,
                    llm=llm,
                )
                if gen_ok and gen_script:
                    # Save it for next time
                    await _db.upsert_reflex(
                        domain=flow.domain,
                        name="auto_generated",
                        description=f"Auto-generated for: {flow.task}",
                        script=gen_script,
                        pause_after=None,
                        tags=["auto"],
                    )
                    await _update(RunStatus.COMPLETED, "Generated script completed flow")
                    return {
                        "raw_result": "Completed via auto-generated reflex",
                        "flow_name": flow.name,
                        "inputs": inputs,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                logger.info("Script generation failed (%s), falling through to agent", gen_msg)

            # ----------------------------------------------------------
            # Slow path: full agent loop (only if fast paths failed)
            # ----------------------------------------------------------
            await _update(RunStatus.RUNNING, "Starting agent loop...")
            task = _build_task(flow, inputs, domain_reflexes)

            _paused = [False]
            _agent_ref: list = []

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
                reflex = await _db.get_reflex(flow.domain, name)
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
                    return ActionResult(
                        extracted_content=f"Reflex '{name}' done. Continue from current page."
                    )

                # Reflex failed — try auto-repair with a subagent LLM call.
                # This keeps the DOM dump + repair loop out of the main agent context.
                await _update(RunStatus.RUNNING, f"Reflex '{name}' failed, attempting auto-repair...")
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
                    await _db.upsert_reflex(
                        domain=flow.domain,
                        name=name,
                        description=reflex["description"],
                        script=fixed_script,
                        pause_after=reflex.get("pause_after"),
                        tags=json.loads(reflex.get("tags", "[]")) if isinstance(reflex.get("tags"), str) else reflex.get("tags", []),
                    )
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
                    "The script runs as an async function body receiving (inputs, entryUrl) "
                    "plus helpers: waitFor(sel, timeout), sleep(ms), typeInto(el, text, delay), "
                    "fillInput(el, value), acceptSuggestion(sel, timeout), "
                    "typeAndSelect(el, text, opts?). "
                    "For fields requiring suggestion selection (locations, addresses), "
                    "ALWAYS use typeAndSelect — typing alone won't work. Example:\n"
                    "  const el = await waitFor('input[name=\"pickup\"]');\n"
                    "  await typeAndSelect(el, inputs.pickup);\n"
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
                await _db.upsert_reflex(
                    domain=flow.domain,
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
            async def request_human_help(reason: str, category: str = "other") -> ActionResult:
                _paused[0] = True
                await _update(RunStatus.PAUSED_FOR_HITL, f"[{category}] {reason}")
                if _agent_ref:
                    _agent_ref[0].pause()
                return ActionResult(
                    extracted_content="Human assisted. Continue from current page state."
                )

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
                tools=tools,
                max_actions_per_step=10,
                use_vision="auto",
                llm_screenshot_size=(1024, 768),
                extend_system_message=_AGENT_RULES,
            )

            _agent_ref.append(agent)

            if agent_ready_callback:
                await agent_ready_callback(agent, browser, flow, _paused)

            await _update(RunStatus.RUNNING, f"Agent running flow '{flow.name}'...")

            result = await agent.run(max_steps=20)

            if hasattr(result, "is_successful") and not result.is_successful():
                errors = result.errors() if hasattr(result, "errors") else []
                error_msg = "; ".join(str(e) for e in errors) if errors else "Agent failed to complete the task"
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
                    await browser.close()
                except Exception:
                    pass
