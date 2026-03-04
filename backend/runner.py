"""Flow runner — agent-first with reflex tools for known, stable page interactions.

Architecture
------------
The agent always runs and retains full judgment over the task. It has access to its
complete toolkit (click, fill, navigate, screenshot, etc.) plus three reflex tools:

  search_reflexes(use_case)         — discover relevant cached Playwright snippets
  run_reflex(name)                  — fire a named reflex for the current domain
  save_reflex(name, description,    — write or update a reflex after solving something
              script, ...)

Reflexes are global, domain-scoped snippets (e.g. "uber.com/fill_booking_form") that
accumulate across runs. They're like muscle memory: the agent fires them when it
recognizes a situation it has already solved, skipping several slow LLM-driven steps.
When a reflex fails (UI changed, auth state different), the agent handles it manually
with its normal tools and can call save_reflex() to update the cached version.

Naming convention: {domain}/{name}
  uber.com/pre_auth             — navigate and fill the form before auth
  uber.com/confirm_ride         — confirm after the human logs in
  gmail.com/compose_email       — open compose window and fill fields
"""

import asyncio
import http.client
import json
import logging
import os
import re
import traceback as _traceback
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable

from playwright.async_api import async_playwright
from browser_use import Agent, Browser, ActionResult, Tools
from browser_use.llm.google import ChatGoogle
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.openai.chat import ChatOpenAI

from . import db as _db
from .models import Flow

logger = logging.getLogger(__name__)

# Minimum word-overlap fraction for search_reflexes to surface a result.
# Keeping this low (0.15) because domain scoping already does the heavy filtering;
# we just want to exclude completely unrelated reflexes.
_RELEVANCE_THRESHOLD = 0.15


class RunStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    RUNNING = "running"
    PAUSED_FOR_AUTH = "paused_for_auth"
    COMPLETED = "completed"
    FAILED = "failed"


def _get_llm() -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if provider == "gemini":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY required when LLM_PROVIDER=gemini")
        return ChatGoogle(model=os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview"), api_key=api_key)
    elif provider in ("claude", "anthropic"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY required when LLM_PROVIDER=claude")
        return ChatAnthropic(model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"), api_key=api_key)
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY required when LLM_PROVIDER=openai")
        return ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), api_key=api_key)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def _relevance_score(use_case: str, reflex: dict) -> float:
    """Word-recall fraction: what proportion of use_case words appear in the reflex text.

    Intentionally recall-oriented so we don't miss relevant reflexes. The domain
    scoping already restricts the candidate set; this just removes clearly irrelevant ones.
    """
    def tokens(text: str) -> set[str]:
        return set(re.findall(r"\b\w+\b", text.lower()))

    query = tokens(use_case)
    if not query:
        return 0.0
    candidate = tokens(
        f"{reflex['name']} {reflex['description']} {' '.join(reflex.get('tags', []))}"
    )
    return len(query & candidate) / len(query)


def _build_task(flow: Flow, inputs: dict, domain_reflexes: list[dict]) -> str:
    """Build the agent task string, including a summary of available reflexes."""
    try:
        input_defs = json.loads(flow.inputs) if flow.inputs else []
    except (json.JSONDecodeError, TypeError):
        input_defs = []
    input_keys = [d.get("name") or d.get("key", "") for d in input_defs if d.get("name") or d.get("key")]

    lines = [f"Task: {flow.description}"]
    if flow.entry_url:
        lines.append(f"Starting URL: {flow.entry_url}")
    if inputs:
        lines.append("User inputs:")
        for k, v in inputs.items():
            lines.append(f"  {k} = {v!r}")
    if input_keys:
        lines.append(f"Input keys available in reflexes: {input_keys}")

    if domain_reflexes:
        lines += [
            "",
            f"Reflexes available for {flow.domain}:",
        ]
        for r in domain_reflexes:
            pause_note = "  [expects auth after]" if r.get("pause_after") == "auth" else ""
            lines.append(f"  • {r['name']}{pause_note} — {r['description']}")
        lines += [
            "",
            "Use search_reflexes(use_case) to find relevant ones for your current step.",
            "Use run_reflex(name) to fire one. If it fails, handle manually and update it with save_reflex().",
        ]
    else:
        lines += [
            "",
            f"No reflexes saved yet for {flow.domain}.",
            "As you solve parts of this task, use save_reflex() to cache them for future runs.",
        ]

    return "\n".join(lines)


class FlowRunner:

    @staticmethod
    async def _exec_playwright(script: str, inputs: dict, cdp_url: str) -> str:
        """Execute a Playwright script body against the existing Chromium via CDP.

        Wraps the body in `async def _fn(page, inputs):` before exec()-ing so
        top-level `await` works. Returns the stringified return value or "Completed".
        """
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                indented = "\n".join(f"    {line}" for line in script.splitlines()) or "    pass"
                wrapped = f"async def _fn(page, inputs):\n{indented}\n"

                ns: dict = {}
                exec(compile(wrapped, "<reflex>", "exec"), ns)  # noqa: S102
                result = await ns["_fn"](page, inputs)
                return str(result) if result is not None else "Completed"
            finally:
                await browser.disconnect()

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
        """Run a flow. The agent always drives; reflex tools give it cached fast paths."""

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
        # Load domain reflexes (metadata only — scripts fetched on demand)
        # ------------------------------------------------------------------
        domain_reflexes = await _db.list_reflexes_for_domain(flow.domain)

        # ------------------------------------------------------------------
        # Agent run
        # ------------------------------------------------------------------
        browser: Browser | None = None
        try:
            await _update(RunStatus.CONNECTING, "Connecting to sandbox browser via CDP")
            browser = Browser(cdp_url=cdp_url, storage_state=storage_state, headers={"Host": "localhost"})

            if storage_state:
                await _update(
                    RunStatus.CONNECTING,
                    f"Injected {len(storage_state.get('cookies', []))} cookies from saved session",
                )

            await _update(RunStatus.RUNNING, "Connected. Launching agent...")

            llm = _get_llm()
            task = _build_task(flow, inputs, domain_reflexes)

            _paused = [False]
            _agent_ref: list = []

            step_count = 0

            def _on_step(state_summary, agent_output, step_num):
                nonlocal step_count
                step_count += 1
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

            # All default browser tools remain available — reflexes are additive.
            tools = Tools()

            @tools.action(
                description=(
                    "Search for saved reflexes (cached Playwright snippets) relevant to your current step. "
                    "Pass a short description of what you're trying to do, e.g. 'fill pickup and dropoff fields'. "
                    "Returns only high-confidence matches — if nothing comes back, there are no useful reflexes "
                    "for this step and you should handle it manually (and consider saving a new reflex)."
                )
            )
            async def search_reflexes(use_case: str) -> ActionResult:
                candidates = await _db.list_reflexes_for_domain(flow.domain)
                matches = [
                    r for r in candidates
                    if _relevance_score(use_case, r) >= _RELEVANCE_THRESHOLD
                ]
                # Sort by score descending
                matches.sort(key=lambda r: _relevance_score(use_case, r), reverse=True)
                if not matches:
                    return ActionResult(
                        extracted_content=(
                            f"No reflexes found for '{use_case}' on {flow.domain}. "
                            "Handle this step manually, then save a reflex if it's repeatable."
                        )
                    )
                lines = [f"Relevant reflexes for '{use_case}' on {flow.domain}:"]
                for r in matches:
                    pause_note = "  [expects auth after]" if r.get("pause_after") == "auth" else ""
                    lines.append(f"  • {r['name']}{pause_note} — {r['description']}  tags: {r['tags']}")
                lines.append("Call run_reflex(name) to execute one.")
                return ActionResult(extracted_content="\n".join(lines))

            @tools.action(
                description=(
                    "Execute a saved reflex by name. The reflex fires a pre-written Playwright script "
                    "against the current browser — fast, no per-step LLM calls. "
                    "If it fails, the error is returned so you can handle the step manually "
                    "and update the reflex with save_reflex()."
                )
            )
            async def run_reflex(name: str) -> ActionResult:
                reflex = await _db.get_reflex(flow.domain, name)
                if not reflex:
                    return ActionResult(
                        error=f"No reflex named '{name}' for domain '{flow.domain}'. "
                              f"Use search_reflexes() to see what's available."
                    )
                try:
                    result_str = await asyncio.wait_for(
                        FlowRunner._exec_playwright(reflex["script"], inputs, cdp_url),
                        timeout=120,
                    )
                    return ActionResult(
                        extracted_content=f"Reflex '{name}' executed successfully. Result: {result_str}"
                    )
                except Exception:
                    tb = _traceback.format_exc()
                    return ActionResult(
                        error=(
                            f"Reflex '{name}' failed:\n{tb}\n"
                            "Handle this step manually, then call save_reflex() to update it."
                        )
                    )

            @tools.action(
                description=(
                    "Save or update a reflex — a named, reusable Playwright Python snippet for this domain. "
                    "Call this after you've successfully handled a repeatable browser interaction manually, "
                    "or to update a reflex that broke because the UI changed. "
                    "The script body receives `page` (Playwright Page) and `inputs` (dict of user values). "
                    "Always start with: await page.goto('<url>'). "
                    "Use inputs['key'] to reference user-provided values — never hardcode them. "
                    "Name should be snake_case and descriptive, e.g. 'fill_booking_form', 'confirm_ride'."
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
                    extracted_content=(
                        f"Reflex '{flow.domain}/{name}' saved. "
                        "It will be available to future agents running flows on this domain."
                    )
                )

            @tools.action(
                description=(
                    "Request human help to log in. Call this when you encounter a login page, "
                    "auth wall, or captcha you cannot bypass. The human will log in via the "
                    "browser and then resume you."
                )
            )
            async def request_human_login(reason: str) -> ActionResult:
                _paused[0] = True
                await _update(RunStatus.PAUSED_FOR_AUTH, f"Login needed: {reason}")
                if _agent_ref:
                    _agent_ref[0].pause()
                return ActionResult(
                    extracted_content=(
                        "Human has logged in. The browser now has an authenticated session. "
                        "Continue from the current page state."
                    )
                )

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
                tools=tools,
                extend_system_message=(
                    "You have reflex tools (search_reflexes, run_reflex, save_reflex) for fast, "
                    "cached browser interactions. Use search_reflexes() at the start of each major "
                    "phase to check if a reflex exists. Fire it with run_reflex() if relevant. "
                    "If a reflex fails or doesn't exist, use your normal browser tools and then "
                    "call save_reflex() to cache the solution for next time. "
                    "Your judgment always takes precedence — reflexes are an optimization, not a constraint."
                ),
            )

            _agent_ref.append(agent)

            if agent_ready_callback:
                await agent_ready_callback(agent, browser, flow, _paused)

            await _update(RunStatus.RUNNING, f"Agent running flow '{flow.name}'...")

            result = await asyncio.wait_for(agent.run(max_steps=30), timeout=600)

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
