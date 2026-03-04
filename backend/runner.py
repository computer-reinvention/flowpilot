"""Flow runner — browser-use agent with reflex tools for cached interaction steps.

Architecture
------------
The agent uses browser-use's full toolkit (click, input, navigate, screenshot, etc.)
plus custom reflex tools:

  search_reflexes(use_case)  — find cached step-by-step instructions for this domain
  run_reflex(name)           — retrieve saved steps to follow
  save_reflex(name, ...)     — save steps after solving a repeatable interaction
  request_human_help(...)    — pause for HITL (auth walls, CAPTCHAs, etc.)

Reflexes are domain-scoped step descriptions (e.g. "uber.com/fill_booking_form") that
accumulate across runs. The agent retrieves them as guidance and follows the steps
using browser-use's built-in tools.

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

# Minimum word-overlap fraction for search_reflexes to surface a result.
# Keeping this low (0.15) because domain scoping already does the heavy filtering;
# we just want to exclude completely unrelated reflexes.
_RELEVANCE_THRESHOLD = 0.15


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
        model = os.environ.get("GEMINI_MODEL") or "gemini-3-flash-preview"
        logger.info("Using Gemini model: %s", model)
        return ChatGoogle(model=model, api_key=api_key)
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

    lines += ["", "--- Workflow ---"]
    if domain_reflexes:
        lines.append(f"Saved reflexes for {flow.domain}:")
        for r in domain_reflexes:
            pause_note = " [pauses for auth after]" if r.get("pause_after") == "auth" else ""
            lines.append(f"  • {r['name']}{pause_note} — {r['description']}")
        lines += [
            "",
            "Strategy:",
            "1. Call search_reflexes() to find cached steps for what you need to do.",
            "2. If a match exists: call run_reflex() and follow the returned steps exactly.",
            "3. If no match or the steps are outdated: handle it yourself, then save_reflex().",
        ]
    else:
        lines += [
            f"No reflexes saved yet for {flow.domain}.",
            "Strategy:",
            "1. Screenshot → plan minimal steps → execute.",
            "2. After each successful interaction, call save_reflex() to cache it for next time.",
        ]

    return "\n".join(lines)


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

            # Use browser-use's full toolkit — no action exclusions.
            # The agent can click, type, scroll, navigate, etc. directly.
            tools = Tools()

            @tools.action(
                description=(
                    "Search for saved reflexes relevant to your current step. "
                    "Pass a short description of what you're trying to do, e.g. 'fill pickup and dropoff fields'. "
                    "Returns matching reflexes with step-by-step instructions you can follow."
                )
            )
            async def search_reflexes(use_case: str) -> ActionResult:
                candidates = await _db.list_reflexes_for_domain(flow.domain)
                matches = [
                    r for r in candidates
                    if _relevance_score(use_case, r) >= _RELEVANCE_THRESHOLD
                ]
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
                lines.append("Call run_reflex(name) to see the full steps.")
                return ActionResult(extracted_content="\n".join(lines))

            @tools.action(
                description=(
                    "Retrieve a saved reflex by name. Returns step-by-step instructions "
                    "for a previously solved interaction on this domain. "
                    "Follow the returned steps using your browser tools (click, input, navigate, etc.)."
                )
            )
            async def run_reflex(name: str) -> ActionResult:
                reflex = await _db.get_reflex(flow.domain, name)
                if not reflex:
                    return ActionResult(
                        error=f"No reflex named '{name}' for domain '{flow.domain}'. "
                              f"Use search_reflexes() to see what's available."
                    )
                return ActionResult(
                    extracted_content=(
                        f"Reflex '{name}' for {flow.domain}:\n"
                        f"Description: {reflex['description']}\n"
                        f"Steps:\n{reflex['script']}\n\n"
                        "Follow these steps using your browser tools. "
                        "If the steps are outdated, adapt and call save_reflex() to update."
                    )
                )

            @tools.action(
                description=(
                    "Save or update a reflex — a named, reusable set of steps for this domain. "
                    "Call this after you've successfully completed a repeatable interaction. "
                    "Write clear step-by-step instructions that a future agent can follow. "
                    "Reference user inputs as inputs['key']. "
                    "Name should be snake_case and descriptive, e.g. 'fill_booking_form', 'confirm_ride'."
                )
            )
            async def save_reflex(
                name: str,
                description: str,
                steps: str,
                tags: list[str] | None = None,
                pause_after_auth: bool = False,
            ) -> ActionResult:
                pause_after = "auth" if pause_after_auth else None
                await _db.upsert_reflex(
                    domain=flow.domain,
                    name=name,
                    description=description,
                    script=steps,
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
                    "Request human assistance. Call this whenever you need a human to step in — "
                    "e.g. login/auth walls, CAPTCHAs, consent dialogs, ambiguous form fields, "
                    "or any situation you cannot resolve autonomously. "
                    "Pass a short 'reason' describing what you need and a 'category' tag: "
                    "'auth', 'captcha', 'consent', 'verification', 'form_fill', 'confirmation', or 'other'. "
                    "The human will interact with the browser and then resume you."
                )
            )
            async def request_human_help(reason: str, category: str = "other") -> ActionResult:
                _paused[0] = True
                await _update(RunStatus.PAUSED_FOR_HITL, f"[{category}] {reason}")
                if _agent_ref:
                    _agent_ref[0].pause()
                return ActionResult(
                    extracted_content=(
                        "Human assistance provided. The browser state has been updated. "
                        "Continue from the current page state."
                    )
                )

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
                tools=tools,
                max_actions_per_step=10,
                extend_system_message=(
                    "EFFICIENCY IS CRITICAL — you have limited steps. Do not waste them.\n"
                    "\n"
                    "STRATEGY (follow this order):\n"
                    "1. REFLEXES FIRST: Call search_reflexes() before doing anything on a page. "
                    "If a reflex exists, call run_reflex() and follow its steps exactly.\n"
                    "2. IF NO REFLEX: Take a screenshot, plan the minimum steps, execute. "
                    "Then call save_reflex() to cache what you did.\n"
                    "\n"
                    "MANDATORY RULES:\n"
                    "- AUTH/CAPTCHA/LOGIN: Any auth wall, CAPTCHA, or login page → "
                    "immediately call request_human_help(). Never try to solve auth yourself. "
                    "Never call done() with failure — the human will handle it and resume you.\n"
                    "- AUTOCOMPLETE: When you type into a location/search field and a dropdown "
                    "appears with suggestions, ACCEPT THE FIRST MATCHING SUGGESTION. "
                    "Do not open new tabs to verify. Do not search for pin codes. "
                    "Just click the suggestion — it's what a real user would do.\n"
                    "- NO OVERTHINKING: Accept reasonable defaults. Don't verify obvious things. "
                    "If a suggestion looks right, click it. If a button says what you need, click it.\n"
                    "- USER VALUES: Use inputs['key'] for user-provided values — never hardcode them.\n"
                    "- SPEED: Each step costs time and money. Finish in the fewest steps possible."
                ),
            )

            _agent_ref.append(agent)

            if agent_ready_callback:
                await agent_ready_callback(agent, browser, flow, _paused)

            await _update(RunStatus.RUNNING, f"Agent running flow '{flow.name}'...")

            # No wall-clock timeout — max_steps=30 bounds active execution and
            # HITL pauses should wait indefinitely until the human resumes.
            result = await agent.run(max_steps=30)

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
